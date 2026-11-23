"""Unit tests of the M6 tools' pure parts (no cluster): python3 -m unittest discover -s scripts/m6 -p 'test_*.py'"""
import gzip
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402
import flash_app  # noqa: E402
import m6lib as m6  # noqa: E402


class Guard(unittest.TestCase):
    def test_namespace_names(self):
        for ok in ("v2-lab", "storefront-v2-lab", "v2"):
            m6.check_namespace_name(ok)
        for bad in ("", "catalog-dev", "storefront-prod", "monitoring", "kube-system", "sample-app-dev", "lab", "V2-lab"):
            with self.assertRaises(m6.GuardError, msg=bad):
                m6.check_namespace_name(bad)

    def test_kubeconfig_paths(self):
        with self.assertRaises(m6.GuardError):
            m6.check_kubeconfig_path(os.path.expanduser("~/.kube/config"))
        with tempfile.TemporaryDirectory() as d:
            laptop = os.path.join(d, "laptop.yaml")
            open(laptop, "w").close()
            with self.assertRaises(m6.GuardError):
                m6.check_kubeconfig_path(laptop)
            lab = os.path.join(d, "xiaoxin.yaml")
            open(lab, "w").close()
            m6.check_kubeconfig_path(lab)
            link = os.path.join(d, "innocent.yaml")
            os.symlink(laptop, link)
            with self.assertRaises(m6.GuardError):   # a symlink to the laptop's file is the laptop's file
                m6.check_kubeconfig_path(link)

    def test_verdict(self):
        self.assertEqual(m6.verdict([m6.row("a", "", "", True)]), "PASS")
        self.assertEqual(m6.verdict([m6.row("a", "", "", True), m6.row("b", "", "", None)]), "INCONCLUSIVE")
        self.assertEqual(m6.verdict([m6.row("a", "", "", False), m6.row("b", "", "", None)]), "FAIL")
        self.assertEqual(m6.verdict([]), "INCONCLUSIVE")
        self.assertEqual(m6.verdict([m6.row("a", "", "", True), m6.row("i", "", "", "info")]), "PASS")


class K6Window(unittest.TestCase):
    def test_window_ratio_and_csv(self):
        rows = "metric_name,timestamp,metric_value,name\n" + "\n".join([
            "m6_browse_ok,100,1,x", "m6_browse_ok,150,0,x", "m6_browse_ok,160,1,x", "m6_browse_ok,300,0,x",
            "http_reqs,150,1,x", "m6_order_ok,150,1,y"])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.csv.gz")
            with gzip.open(p, "wt") as f:
                f.write(rows + "\n")
            got = check.read_k6_csv(p)
        self.assertEqual(len(got), 5)                                     # http_reqs dropped
        self.assertEqual(check.window_ratio(got, "m6_browse_ok", 140, 200), (1, 2))
        self.assertEqual(check.window_ratio(got, "m6_browse_ok", 0, 1000), (2, 4))
        self.assertEqual(check.window_ratio(got, "m6_order_ok", 140, 200), (1, 1))


def pod(uid, owner, restarts=0, started="t0", image="sha256:a"):
    return {"uid": uid, "owner": owner, "containers": {"c": {"restarts": restarts, "startedAt": started, "imageID": image}}}


def snap(workloads, pods):
    return {"workloads": workloads, "pods": pods}


class Deploy(unittest.TestCase):
    W = {"Deployment/payment": {"revision": "3", "generation": 3, "images": {"c": "p:1"}},
         "Deployment/order": {"revision": "1", "generation": 1, "images": {"c": "o:1"}}}

    def test_only_target(self):
        before = snap(self.W, {"payment-a-1": pod("u1", "Deployment/payment"), "order-a-1": pod("u2", "Deployment/order")})
        w2 = dict(self.W, **{"Deployment/payment": {"revision": "4", "generation": 4, "images": {"c": "p:2"}}})
        after = snap(w2, {"payment-b-1": pod("u3", "Deployment/payment", image="sha256:b"), "order-a-1": pod("u2", "Deployment/order")})
        self.assertEqual(check.deploy_diff(before, after, "payment"), (True, [], [], []))

    def test_neighbour_restarted_and_replaced(self):
        before = snap(self.W, {"order-a-1": pod("u2", "Deployment/order"), "rabbit-0": pod("u5", "StatefulSet/rabbitmq")})
        after = snap(self.W, {"order-a-1": pod("u2", "Deployment/order", restarts=1, started="t1"), "rabbit-0": pod("u6", "StatefulSet/rabbitmq")})
        tc, others, replaced, restarted = check.deploy_diff(before, after, "payment")
        self.assertFalse(tc)                       # nothing was released
        self.assertEqual(others, [])
        self.assertEqual(replaced, ["rabbit-0", "new rabbit-0"])
        self.assertEqual(len(restarted), 1)

    def test_hook_job_pods_ignored(self):
        before = snap(self.W, {"topology-a": pod("j1", "Job/topology")})
        after = snap(self.W, {"topology-b": pod("j2", "Job/topology")})
        self.assertEqual(check.deploy_diff(before, after, "payment")[2:], ([], []))

    def test_other_workload_changed(self):
        w2 = dict(self.W, **{"Deployment/order": {"revision": "2", "generation": 2, "images": {"c": "o:1"}}})
        _, others, _, _ = check.deploy_diff(snap(self.W, {}), snap(w2, {}), "payment")
        self.assertEqual(others, ["Deployment/order"])


class Consistency(unittest.TestCase):
    def test_payments(self):
        orders = {1: (6, Decimal("10.00")), 2: (6, Decimal("5")), 3: (7, Decimal("1")), 4: (6, Decimal("2")), 5: (7, Decimal("3"))}
        pays = {1: [(1, Decimal("10.0"))], 2: [(1, Decimal("6"))], 3: [(1, Decimal("1"))], 5: [(2, Decimal("3"))]}
        got = check.payments_check(orders, pays)
        self.assertEqual(got["amount_mismatch"], [2])
        self.assertEqual(got["confirmed_unpaid"], [4])
        self.assertEqual(got["charged_cancelled"], [3])   # 5: a failed payment for a cancelled order is fine
        self.assertEqual(got["double"], [])
        self.assertEqual(check.payments_check({9: (6, Decimal(1))}, {9: [(1, Decimal(1)), (1, Decimal(1))]})["double"], [9])

    def test_reservations(self):
        orders = {1: (6, 0), 2: (7, 0), 3: (7, 0), 4: (6, 0)}
        items = {1: {"A": 2, "B": 1}, 4: {"A": 1}}
        res = {1: {"A": (2, 2), "B": (1, 2)}, 2: {"A": (1, 3)}, 3: {"A": (1, 1)}, 4: {"A": (1, 1)}}
        got = check.reservations_check(orders, items, res)
        self.assertEqual(got, {"confirmed_not_committed": [4], "cancelled_committed": [], "cancelled_held": [3]})

    def test_stock_books(self):
        self.assertEqual(check.stock_books([("A", 5, 2, 10, 2, 3)]), [])
        probs = check.stock_books([("B", 5, 2, 10, 1, 3), ("C", 4, 0, 10, 0, 3)])
        self.assertEqual(len(probs), 2)          # B: reserved ≠ held (and books: 5+2+3=10 ok); C: 7 ≠ 10
        self.assertIn("reserved 2", probs[0])
        self.assertIn("received 10", probs[1])


class FlashApp(unittest.TestCase):
    def test_render(self):
        doc = flash_app.render("v2-lab", 8080, "postgres", "rabbitmq")
        self.assertEqual((doc["name"], doc["environments"][0]["name"]), ("v2", "lab"))
        self.assertEqual(len(doc["capacity"]["requests"]), 3)
        self.assertEqual(doc["capacity"]["workloads"][0], "order")
        self.assertIn("{rand}", doc["capacity"]["requests"][0]["body"])
        names = {s["name"] for s in doc["slos"]}
        self.assertEqual(set(doc["capacity"]["slos"]), names)
        doc = flash_app.render("storefront-v2-lab", 80, "", "")
        self.assertEqual((doc["name"], doc["environments"][0]["name"]), ("storefront-v2", "lab"))
        self.assertEqual(doc["capacity"]["workloads"], ["order", "inventory", "payment", "notification"])


if __name__ == "__main__":
    unittest.main()
