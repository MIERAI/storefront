#!/usr/bin/env python3
"""The timeline of a fault experiment, recorded while it runs (start it just before the fault, next to experiment.py):

  - the target Deployment's readiness every 5s: when it went fully down (no ready pod) and when it came back
    (first ready pod = "recovered"; every pod ready = "all ready");
  - every --poll seconds, the run's orders by status (orders of customers TAG*1e6 … TAG*1e6+999999, read-only SQL in
    order's database) and how many of the orders created BEFORE the recovery are still not in a final state;
  - the moment that number reaches 0 ("all terminal"), then it stops (or --max-wait after the recovery).

  watch.py --target payment --tag 11231430 [--namespace v2-lab] [--poll 15] [--max-wait 20m] --out out/payment-down.timeline.json

The file is rewritten after every poll (a crash keeps what was seen). check.py consistency / isolation read it.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m6lib as m6


def ready(ns, deploy):
    d = json.loads(m6.kubectl("get", "deployment", deploy, "-n", ns, "-o", "json"))
    return d["status"].get("readyReplicas", 0) or 0, d["spec"].get("replicas", 0) or 0


def order_counts(ns, db, tag, before, pod):
    lo, hi = tag * 1000000, tag * 1000000 + 999999
    rows = m6.sql(ns, db, f"""SELECT status, count(*), count(*) FILTER (WHERE created_at < to_timestamp({before:.3f}))
FROM orders WHERE customer_id BETWEEN {lo} AND {hi} GROUP BY status ORDER BY status""", pod=pod)
    by = {m6.STATUS.get(int(s), s): int(n) for s, n, _ in rows}
    open_before = sum(int(b) for s, _, b in rows if int(s) not in m6.TERMINAL)
    return by, open_before


def dur(s):
    s = str(s)
    return int(s[:-1]) * {"s": 1, "m": 60, "h": 3600}[s[-1]] if s[-1] in "smh" else int(s)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--namespace", default=os.environ.get("M6_NAMESPACE", "v2-lab"))
    p.add_argument("--target", required=True, help="the Deployment the fault takes down (inventory, payment)")
    p.add_argument("--tag", type=int, required=True, help="the run's TAG (the k6 run's -e TAG)")
    p.add_argument("--poll", type=int, default=15)
    p.add_argument("--down-timeout", default="10m", help="give up if the target never goes down within this")
    p.add_argument("--max-wait", default="20m", help="after the recovery, stop after this even if orders are still open")
    p.add_argument("--pg-cluster")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    m6.guard_or_exit(a.namespace)
    pod = m6.primary_pod(a.namespace, a.pg_cluster)
    db = m6.databases(a.namespace, pod)["order"]
    t = {"namespace": a.namespace, "target": a.target, "tag": a.tag, "started": time.time(), "down_at": None,
         "recovered_at": None, "all_ready_at": None, "all_terminal_at": None, "samples": [], "readiness": []}

    def save():
        os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(t, f, indent=2)

    last_poll, last_ready = 0.0, None
    while True:
        now = time.time()
        try:
            r, want = ready(a.namespace, a.target)
        except RuntimeError as e:
            print(f"warning: {e}", file=sys.stderr)
            r, want = None, None
        if r is not None and (r, want) != last_ready:
            t["readiness"].append({"t": now, "ready": r, "replicas": want})
            print(f"{time.strftime('%H:%M:%S')} {a.target} ready {r}/{want}", flush=True)
            last_ready = (r, want)
        if r is not None:
            if t["down_at"] is None and r == 0:
                t["down_at"] = now
            elif t["down_at"] and t["recovered_at"] is None and r >= 1:
                t["recovered_at"] = now
            if t["recovered_at"] and t["all_ready_at"] is None and r >= want > 0:
                t["all_ready_at"] = now
        if t["down_at"] is None and now - t["started"] > dur(a.down_timeout):
            t["error"] = f"{a.target} never went down within {a.down_timeout}"
            save()
            sys.exit(f"watch: {t['error']}")
        if now - last_poll >= a.poll:
            last_poll = now
            try:
                by, open_before = order_counts(a.namespace, db, a.tag, t["recovered_at"] or now, pod)
                t["samples"].append({"t": now, "by_status": by, "open_created_before_recovery": open_before if t["recovered_at"] else None})
                print(f"{time.strftime('%H:%M:%S')} orders {by}" + (f"  open (created before recovery): {open_before}" if t["recovered_at"] else ""), flush=True)
                if t["recovered_at"] and open_before == 0 and t["all_terminal_at"] is None:
                    t["all_terminal_at"] = now
                    print(f"all orders created before the recovery are final, {now - t['recovered_at']:.0f}s after it", flush=True)
            except RuntimeError as e:
                print(f"warning: {e}", file=sys.stderr)
            save()
        if t["all_terminal_at"] or (t["recovered_at"] and now - t["recovered_at"] > dur(a.max_wait)):
            break
        time.sleep(5)
    t["ended"] = time.time()
    save()
    print(f"→ {a.out}")


if __name__ == "__main__":
    main()
