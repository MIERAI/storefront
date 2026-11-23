#!/usr/bin/env python3
"""Pass / fail per promise of the v2 design §0, with the measured numbers (docs/m6-verification.md).

  check.py deploy      --before B.json --after A.json --target payment [--k6-summary S.json]
  check.py isolation   --timeline T.json --k6-csv C.csv.gz --k6-summary S.json [--max-outage-gap 60]
  check.py consistency --timeline T.json --k6-summary S.json [--outage 10m] [--budget 5m]
  check.py stock       --tag TAG --k6-summary S.json [--wait 3m]           (the oversell rush, flash-oversell.js)
  check.py scaling     --baseline BASE-capacity.json --scaled SCALED-capacity.json [--factor 2]
  common: [--namespace v2-lab] [--pg-cluster NAME] [--out out/<name>]   (writes <name>.md and <name>.json)

Everything is read only: SQL runs in READ ONLY sessions on the stack's CNPG primary (kubectl exec psql), Prometheus
through the API server's service proxy. Exit 0 = PASS, 1 = FAIL, 2 = INCONCLUSIVE (something could not be measured).
"""
import argparse
import csv
import gzip
import io
import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m6lib as m6

BROWSE = 'procedure=~"/catalog.catalog.v1.CatalogService/(ListProducts|GetProduct)"'
SERVER_ERRORS = 'code=~"internal|unavailable|unknown|deadline_exceeded|data_loss"'
JOURNEY = 'machine="OrderSaga"'


def dur(s):
    s = str(s)
    return int(s[:-1]) * {"s": 1, "m": 60, "h": 3600}[s[-1]] if s[-1] in "smh" else int(s)


# --- pure judgements (unit-tested in test_m6.py) ---------------------------------------------------------------

def window_ratio(rows, metric, start, end):
    """(ok, total) of a k6 Rate metric's samples with start <= timestamp <= end. rows: dicts of the k6 CSV."""
    ok = total = 0
    for r in rows:
        if r.get("metric_name") != metric:
            continue
        ts = float(r["timestamp"])
        if start <= ts <= end:
            total += 1
            ok += 1 if float(r["metric_value"]) >= 1 else 0
    return ok, total


def read_k6_csv(path, metrics=("m6_browse_ok", "m6_order_ok")):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("metric_name") in metrics]


def deploy_diff(before, after, target):
    """What changed between two snapshots: (target_changed, other_workloads_changed, other_pods_replaced,
    other_containers_restarted). target: the Deployment's name (payment). Pods of Jobs are left out: the topology
    job is an Argo CD Sync hook (BeforeHookCreation), re-created by every sync of the application by design."""
    tkey = f"Deployment/{target}"
    keys = lambda w: (w.get("revision"), w.get("generation"), tuple(sorted(w.get("images", {}).items())))
    bw, aw = before["workloads"], after["workloads"]
    changed = sorted(k for k in set(bw) | set(aw) if keys(bw.get(k, {})) != keys(aw.get(k, {})))
    by_owner = lambda pods, o: {p["uid"] for p in pods.values() if p["owner"] == o}
    target_changed = tkey in changed or by_owner(before["pods"], tkey) != by_owner(after["pods"], tkey)
    after_uids = {p["uid"]: (n, p) for n, p in after["pods"].items()}
    replaced, restarted = [], []
    for name, p in before["pods"].items():
        if p["owner"] == tkey or p["owner"].startswith("Job/"):
            continue
        if p["uid"] not in after_uids:
            replaced.append(name)
            continue
        q = after_uids[p["uid"]][1]
        for c, s in p["containers"].items():
            t = q["containers"].get(c, {})
            if (s["restarts"], s["startedAt"], s["imageID"]) != (t.get("restarts"), t.get("startedAt"), t.get("imageID")):
                restarted.append(f"{name}/{c}: restarts {s['restarts']}→{t.get('restarts')}")
    new = [n for n, p in after["pods"].items() if p["owner"] != tkey and not p["owner"].startswith("Job/") and p["uid"] not in {x["uid"] for x in before["pods"].values()}]
    return target_changed, [k for k in changed if k != tkey], replaced + [f"new {n}" for n in new], restarted


def payments_check(orders, payments):
    """orders: {id: (status, total Decimal)}, payments: {order_id: [(status, amount Decimal)]} → problems by kind."""
    out = {"double": [], "confirmed_unpaid": [], "amount_mismatch": [], "charged_cancelled": []}
    for oid, (status, total) in orders.items():
        ps = payments.get(oid, [])
        if len(ps) > 1:
            out["double"].append(oid)
        ok = [a for s, a in ps if s == 1]
        if status == 6:
            if len(ok) != 1:
                out["confirmed_unpaid"].append(oid)
            elif ok[0] != total:
                out["amount_mismatch"].append(oid)
        if status == 7 and ok:
            out["charged_cancelled"].append(oid)
    return out


def reservations_check(orders, items, reservations):
    """CONFIRMED: every line COMMITTED with its qty; CANCELLED: nothing COMMITTED and nothing still HELD.
    items: {order_id: {sku: qty}}, reservations: {order_id: {sku: (qty, status)}}."""
    out = {"confirmed_not_committed": [], "cancelled_committed": [], "cancelled_held": []}
    for oid, (status, _) in orders.items():
        res = reservations.get(oid, {})
        if status == 6:
            if any(res.get(sku) != (qty, 2) for sku, qty in items.get(oid, {}).items()):
                out["confirmed_not_committed"].append(oid)
        elif status == 7:
            if any(st == 2 for _, st in res.values()):
                out["cancelled_committed"].append(oid)
            if any(st == 1 for _, st in res.values()):
                out["cancelled_held"].append(oid)
    return out


def stock_books(rows):
    """rows: (sku, available, reserved, adjusted, held, committed) ints → problems: negative stock, reserved ≠ held,
    available + reserved + committed ≠ everything ever received (the books do not balance = units appeared or vanished)."""
    out = []
    for sku, avail, reserved, adjusted, held, committed in rows:
        if avail < 0 or reserved < 0:
            out.append(f"{sku}: negative (available {avail}, reserved {reserved})")
        if reserved != held:
            out.append(f"{sku}: reserved {reserved} ≠ held reservations {held}")
        if avail + reserved + committed != adjusted:
            out.append(f"{sku}: available {avail} + reserved {reserved} + committed {committed} ≠ received {adjusted}")
    return out


def few(xs, n=5):
    xs = list(xs)
    return "" if not xs else ", ".join(map(str, xs[:n])) + (f" … (+{len(xs) - n})" if len(xs) > n else "")


# --- data ------------------------------------------------------------------------------------------------------

class Stack:
    def __init__(self, ns, cluster=None):
        self.ns = ns
        self.pod = m6.primary_pod(ns, cluster)
        self.db = m6.databases(ns, self.pod)

    def q(self, service, query):
        return m6.sql(self.ns, self.db[service], query, pod=self.pod)

    def orders(self, tag):
        lo, hi = tag * 1000000, tag * 1000000 + 999999
        rows = self.q("order", f"SELECT id, status, total FROM orders WHERE customer_id BETWEEN {lo} AND {hi}")
        orders = {int(i): (int(s), Decimal(t)) for i, s, t in rows}
        items = {}
        if orders:
            for oid, sku, qty in self.q("order", f"""SELECT i.order_id, i.sku, i.qty FROM order_items i JOIN orders o ON o.id = i.order_id
WHERE o.customer_id BETWEEN {lo} AND {hi}"""):
                items.setdefault(int(oid), {})[sku] = int(qty)
        return orders, items

    def payments(self, lo, hi):
        out = {}
        for oid, s, amount in self.q("payment", f"SELECT order_id, status, amount FROM payments WHERE order_id BETWEEN {lo} AND {hi}"):
            out.setdefault(int(oid), []).append((int(s), Decimal(amount)))
        return out

    def reservations(self, lo, hi):
        out = {}
        for oid, sku, qty, s in self.q("inventory", f"SELECT order_id, sku, qty, status FROM reservations WHERE order_id BETWEEN {lo} AND {hi}"):
            out.setdefault(int(oid), {})[sku] = (int(qty), int(s))
        return out

    def stock(self, like="M6-%"):
        rows = self.q("inventory", f"""SELECT s.sku, s.available, s.reserved, coalesce(a.adj, 0), coalesce(r.held, 0), coalesce(r.committed, 0)
FROM stock s
LEFT JOIN (SELECT sku, sum(delta) AS adj FROM stock_adjustments GROUP BY sku) a USING (sku)
LEFT JOIN (SELECT sku, sum(qty) FILTER (WHERE status = 1) AS held, sum(qty) FILTER (WHERE status = 2) AS committed
           FROM reservations GROUP BY sku) r USING (sku)
WHERE s.sku LIKE '{like}' ORDER BY s.sku""")
        return [(r[0],) + tuple(int(x) for x in r[1:]) for r in rows]

    def outbox_pending(self):
        out = {}
        for s in m6.ALL_SERVICES:
            try:
                out[s] = int(self.q(s, "SELECT count(*) FROM factory_outbox WHERE published_at IS NULL")[0][0])
            except RuntimeError as e:
                print(f"warning: outbox of {s}: {e}", file=sys.stderr)
                out[s] = None
        return out


def k6_accepted(summary):
    m = summary.get("metrics", {}).get("m6_orders_accepted") or {}
    return int(m.get("count", 0)) if m else None


def window_increase(ns, metric_sel, start, end, by=""):
    w = max(int(end - start), 60)
    q = f"sum{f' by ({by})' if by else ''}(increase({metric_sel}[{w}s]))"
    return m6.prom(q, at=end)


def server_ratio(ns, matchers, errors, start, end):
    total = m6.prom_scalar(f'sum(increase(rpc_requests_total{{namespace="{ns}", {matchers}}}[{max(int(end - start), 60)}s]))', end)
    bad = m6.prom_scalar(f'sum(increase(rpc_requests_total{{namespace="{ns}", {matchers}, {errors}}}[{max(int(end - start), 60)}s]))', end) or 0.0
    return (None, None) if not total else (1 - bad / total, total)


# --- the checks --------------------------------------------------------------------------------------------------

def common_consistency_rows(st, tag, summary, now_open_ok=True, since=None):
    """Rows every experiment shares: nothing lost, nothing double-charged, no oversell, reservations agree, outboxes drained."""
    rows = []
    orders, items = st.orders(tag)
    acc = k6_accepted(summary) if summary else None
    rows.append(m6.row("no accepted order lost", "orders in order's DB ≥ orders k6 saw accepted",
                       f"{len(orders)} in DB / {acc if acc is not None else 'n/a'} accepted",
                       None if acc is None else len(orders) >= acc,
                       "more in the DB = a response lost on the way (timeout after commit), not a lost order"))
    open_ = [o for o, (s, _) in orders.items() if s not in m6.TERMINAL]
    rows.append(m6.row("every order final now", "0 orders of the run outside CONFIRMED / CANCELLED", f"{len(open_)} open of {len(orders)}",
                       len(open_) == 0 if now_open_ok else None, few(open_)))
    if orders:
        lo, hi = min(orders), max(orders)
        pays = st.payments(lo, hi)
        pc = payments_check(orders, pays)
        conf = sum(1 for s, _ in orders.values() if s == 6)
        canc = sum(1 for s, _ in orders.values() if s == 7)
        rows.append(m6.row("no double charge", "≤ 1 payment per order; each CONFIRMED order exactly 1 succeeded payment = its total",
                           f"{len(pc['double'])} doubles, {len(pc['confirmed_unpaid'])} unpaid, {len(pc['amount_mismatch'])} wrong amount ({conf} confirmed)",
                           not (pc["double"] or pc["confirmed_unpaid"] or pc["amount_mismatch"]),
                           few(pc["double"] + pc["confirmed_unpaid"] + pc["amount_mismatch"])))
        rows.append(m6.row("no charge kept for a cancelled order", "0 CANCELLED orders with a succeeded payment",
                           f"{len(pc['charged_cancelled'])} of {canc} cancelled", not pc["charged_cancelled"], few(pc["charged_cancelled"])))
        rc = reservations_check(orders, items, st.reservations(lo, hi))
        bad = rc["confirmed_not_committed"] + rc["cancelled_committed"] + rc["cancelled_held"]
        rows.append(m6.row("stock follows the orders", "CONFIRMED: every line committed; CANCELLED: nothing committed or held",
                           f"{len(rc['confirmed_not_committed'])} / {len(rc['cancelled_committed'])} / {len(rc['cancelled_held'])}",
                           not bad, few(bad)))
    sb = stock_books(st.stock())
    rows.append(m6.row("no oversell", "every M6 SKU: available ≥ 0, reserved = held, available + reserved + committed = received",
                       f"{len(sb)} problems", not sb, few(sb, 3)))
    ob = st.outbox_pending()
    pending = {k: v for k, v in ob.items() if v}
    rows.append(m6.row("outboxes drained", "0 unpublished rows in every service's outbox",
                       ", ".join(f"{k} {v if v is not None else 'n/a'}" for k, v in ob.items()),
                       None if None in ob.values() else not pending))
    q = f'sum(factory_dlq_messages{{namespace="{st.ns}"}})'
    dlq, dlq0 = m6.prom_scalar(q), (m6.prom_scalar(q, since) if since else 0.0)
    grew = None if dlq is None or dlq0 is None else dlq - dlq0
    rows.append(m6.row("nothing dead-lettered", "no new message in any dead letter queue during the run (factory_dlq_messages)",
                       "n/a" if grew is None else f"{dlq0:.0f} → {dlq:.0f}", None if grew is None else grew <= 0,
                       "messages already there before the run (e.g. the e2e driver's poison) do not count" if dlq0 else ""))
    return rows


def info_rows(ns, start, end):
    """Not judged: how hard the machinery worked (resends, duplicates, late answers ignored)."""
    w = f"{max(int(end - start), 60)}s"
    def val(q):
        v = m6.prom_scalar(q, end)
        return "n/a" if v is None else f"{v:.0f}"
    return [m6.row("info: saga resends", "-", val(f'sum(increase(factory_sm_transitions_total{{namespace="{ns}", {JOURNEY}, trigger="timeout"}}[{w}]))'), "info", "commands re-sent after 30s / 60s"),
            m6.row("info: duplicates dropped", "-", val(f'sum(increase(factory_messages_total{{namespace="{ns}", outcome="duplicate"}}[{w}]))'), "info", "redeliveries with a seen message id"),
            m6.row("info: late answers ignored", "-", val(f'sum(increase(factory_sm_messages_ignored_total{{namespace="{ns}", {JOURNEY}}}[{w}]))'), "info", "e.g. payment answering a re-sent charge")]


def outage_rows(t, want_s, tolerance_s=60):
    down, up = t.get("down_at"), t.get("recovered_at")
    length = (up - down) if down and up else None
    return [m6.row("the fault happened", f"{t['target']} had no ready pod for ≈ {want_s // 60} min",
                   "n/a" if length is None else f"{length / 60:.1f} min",
                   None if length is None else length >= want_s - tolerance_s,
                   "" if length is not None else "watch.py saw no complete outage")], length


def cmd_isolation(a):
    t, summary = m6.load_json(a.timeline), m6.load_json(a.k6_summary)
    st = Stack(a.namespace, a.pg_cluster)
    rows, _ = outage_rows(t, dur(a.outage))
    down, up = t.get("down_at"), t.get("recovered_at")
    k6rows = read_k6_csv(a.k6_csv) if a.k6_csv else []
    if down and up:
        ok, n = window_ratio(k6rows, "m6_browse_ok", down, up)
        rows.append(m6.row("browse during the outage (users)", "≥ 99.5% of browse calls 2xx (k6, inside the outage)",
                           f"{m6.fmt_pct(ok / n if n else None, 3)} of {n}", None if not n else ok / n >= 0.995))
        r, n2 = server_ratio(a.namespace, BROWSE, SERVER_ERRORS, down, up)
        rows.append(m6.row("browse during the outage (catalog)", "≥ 99.5% without a server error (rpc_requests_total)",
                           f"{m6.fmt_pct(r, 3)} of {n2 and int(n2)}", None if r is None else r >= 0.995))
        ok, n = window_ratio(k6rows, "m6_order_ok", down, up)
        rows.append(m6.row("orders accepted during the outage", "≥ 99.5% of PlaceOrder accepted (design §6.3: only order's own DB)",
                           f"{m6.fmt_pct(ok / n if n else None, 3)} of {n}", None if not n else ok / n >= 0.995))
        dl = m6.prom_scalar(f'sum(increase(factory_sm_transitions_total{{namespace="{a.namespace}", {JOURNEY}, trigger="deadline"}}[{max(int(time.time() - down), 60)}s]))')
        rows.append(m6.row("no order given up", "0 orders cancelled by the RESERVING deadline (15 min)",
                           "n/a" if dl is None else f"{dl:.0f}", None if dl is None else dl == 0))
    if up:
        fin = t.get("all_terminal_at")
        rows.append(m6.row("orders resume after recovery", f"every order created before the recovery final within {dur(a.budget) // 60} min of it",
                           "never" if not fin else f"{fin - up:.0f}s", bool(fin) and fin - up <= dur(a.budget)))
    rows += common_consistency_rows(st, t["tag"], summary, since=t["started"])
    rows += info_rows(a.namespace, down or t["started"], t.get("ended") or time.time())
    return m6.write_result(a.out, f"Fault isolation ({t['target']} down)", rows, {"timeline": a.timeline})


def cmd_consistency(a):
    t, summary = m6.load_json(a.timeline), m6.load_json(a.k6_summary)
    st = Stack(a.namespace, a.pg_cluster)
    rows, _ = outage_rows(t, dur(a.outage))
    up, fin = t.get("recovered_at"), t.get("all_terminal_at")
    rows.append(m6.row("final within 5 min of recovery", f"every order created before the recovery CONFIRMED / CANCELLED ≤ {dur(a.budget)}s after it",
                       "never" if not (up and fin) else f"{fin - up:.0f}s", None if not up else bool(fin) and fin - up <= dur(a.budget),
                       f"polled every {a.poll_note}" if a.poll_note else ""))
    peak = max((s["by_status"].get("CHARGING", 0) for s in t.get("samples", [])), default=0)
    rows.append(m6.row("the fault bit", "orders piled up in CHARGING while payment was down", f"peak {peak}", peak > 0 if t["target"] == "payment" else True))
    rows += common_consistency_rows(st, t["tag"], summary, since=t["started"])
    rows += info_rows(a.namespace, t.get("down_at") or t["started"], t.get("ended") or time.time())
    return m6.write_result(a.out, f"Eventual consistency ({t['target']} down)", rows, {"timeline": a.timeline})


def cmd_stock(a):
    summary = m6.load_json(a.k6_summary)
    st = Stack(a.namespace, a.pg_cluster)
    deadline = time.time() + dur(a.wait)
    while True:
        orders, items = st.orders(a.tag)
        open_ = [o for o, (s, _) in orders.items() if s not in m6.TERMINAL]
        if not open_ or time.time() > deadline:
            break
        print(f"{len(open_)} orders still open, waiting…", flush=True)
        time.sleep(10)
    stock = summary.get("scarce_stock") or a.stock
    lo, hi = (min(orders), max(orders)) if orders else (0, 0)
    res = st.reservations(lo, hi) if orders else {}
    committed = sum(q for r in res.values() for sku, (q, s) in r.items() if sku == "M6-SCARCE" and s == 2)
    confirmed = sum(1 for s, _ in orders.values() if s == 6)
    acc = k6_accepted(summary)
    rows = [m6.row("demand above stock", "accepted orders > stock (else it proves nothing)", f"{acc} accepted for {stock}",
                   None if acc is None else acc > stock),
            m6.row("no oversell (this run)", "units committed to the run's orders ≤ stock", f"{committed} ≤ {stock}", committed <= stock),
            m6.row("sold out exactly", "committed = stock when demand exceeds it (no units lost either)", f"{committed} / {stock}", committed == stock),
            m6.row("orders agree", "CONFIRMED orders = committed units (1 each)", f"{confirmed} / {committed}", confirmed == committed)]
    sb = [p for p in stock_books(st.stock("M6-SCARCE"))]
    rows.append(m6.row("M6-SCARCE books balance", "available ≥ 0, reserved = held, available + reserved + committed = received", f"{len(sb)} problems", not sb, few(sb, 3)))
    started = float(summary["started"]) if summary.get("started") else None
    rows += common_consistency_rows(st, a.tag, summary, since=started)
    return m6.write_result(a.out, "No oversell (flash sale on M6-SCARCE)", rows)


def cmd_deploy(a):
    before, after = m6.load_json(a.before), m6.load_json(a.after)
    tc, others, replaced, restarted = deploy_diff(before, after, a.target)
    start, end = before["taken"], after["taken"]
    rows = [m6.row("the release happened", f"{a.target}'s revision / image / pods changed", "yes" if tc else "no", tc),
            m6.row("no other workload changed", "same revision, generation and images", f"{len(others)} changed", not others, few(others)),
            m6.row("no other pod replaced", "same pod uids outside the target", f"{len(replaced)}", not replaced, few(replaced)),
            m6.row("no other container restarted", "same restart count, start time and image digest", f"{len(restarted)}", not restarted, few(restarted, 3))]
    w = max(int(end - start), 60)
    num = m6.prom_scalar(f'sum(increase(factory_sm_completion_seconds_bucket{{namespace="{a.namespace}", {JOURNEY}, le="60"}}[{w}s]))', end)
    den = m6.prom_scalar(f'sum(increase(factory_sm_completion_seconds_count{{namespace="{a.namespace}", {JOURNEY}}}[{w}s]))', end)
    j = (num or 0) / den if den else None
    rows.append(m6.row("journey SLO unaffected", "≥ 99% of orders final within 60s during the window", f"{m6.fmt_pct(j)} of {den and int(den)}", None if j is None else j >= 0.99))
    for s in m6.ALL_SERVICES:
        if s == a.target:
            continue
        r, n = server_ratio(a.namespace, f'app="{s}"', SERVER_ERRORS, start, end)
        mt = m6.prom_scalar(f'sum(increase(factory_messages_total{{namespace="{a.namespace}", app="{s}"}}[{w}s]))', end)
        mb = m6.prom_scalar(f'sum(increase(factory_messages_total{{namespace="{a.namespace}", app="{s}", outcome=~"dlq|poison"}}[{w}s]))', end) or 0.0
        m = (1 - mb / mt) if mt else None
        parts = ([f"RPCs {m6.fmt_pct(r, 3)} of {int(n)}"] if r is not None else []) + ([f"messages {m6.fmt_pct(m, 3)} of {int(mt)}"] if m is not None else [])
        ok = "info" if r is None and m is None else (r is None or r >= 0.995) and (m is None or m >= 0.999)
        rows.append(m6.row(f"{s} healthy", "RPCs ≥ 99.5% without a server error; messages ≥ 99.9% not dead-lettered (its SLOs)",
                           "; ".join(parts) or "no traffic in the window", ok))
    if a.k6_summary:
        s = m6.load_json(a.k6_summary)
        for k, label in (("m6_browse_ok", "browse"), ("m6_order_ok", "orders")):
            v = (s.get("metrics", {}).get(k) or {}).get("rate")
            rows.append(m6.row(f"users: {label}", "≥ 99.5% succeeded (k6, the whole load run)", m6.fmt_pct(v, 3), None if v is None else v >= 0.995))
    pages = m6.prom(f'max_over_time(ALERTS{{namespace="{a.namespace}", alertstate="firing", severity="page"}}[{w}s])', end)
    rows.append(m6.row("no page", "no page-severity alert fired in the namespace during the window",
                       f"{len(pages)}", not pages, few(p[0].get("slo") or p[0].get("alertname") for p in pages)))
    return m6.write_result(a.out, f"Independent deploy (release {a.target})", rows,
                           {"window": [start, end], "changed": others, "replaced": replaced, "restarted": restarted})


def cmd_scaling(a):
    b, s = m6.load_json(a.baseline), m6.load_json(a.scaled)
    sb, ss = b.get("summary", b), s.get("summary", s)
    kb, ks = sb.get("knee_rps"), ss.get("knee_rps")
    rows = [m6.row("baseline knee", "a knee found (an SLO broke above it; not generator-limited)",
                   f"{kb}/s (first break {sb.get('first_breaking_rps')}/s, {sb.get('pods')} {sb.get('workload')} pods)",
                   bool(kb and sb.get("first_breaking_rps"))),
            m6.row("scaled knee", "a knee found, or ≥ the factor × baseline before the generator ran out",
                   f"{ks}/s (first break {ss.get('first_breaking_rps')}/s, {ss.get('pods')} {ss.get('workload')} pods)"
                   + (" generator-limited" if ss.get("generator_limited") else ""),
                   bool(ks and (ss.get("first_breaking_rps") or (kb and ks >= a.factor * kb))))]
    ratio = (ks / kb) if kb and ks else None
    rows.append(m6.row("knee raised", f"scaled / baseline ≥ {a.factor}", "n/a" if ratio is None else f"{ratio:.2f}×",
                       None if ratio is None else ratio >= a.factor,
                       "; ".join(x for x in (f"baseline broke on: {sb.get('broke_on')}" if sb.get("broke_on") else "",
                                             f"bottleneck: {sb.get('bottleneck')}" if sb.get("bottleneck") else "") if x)))
    for name, x in (("baseline", sb), ("scaled", ss)):
        if x.get("unstable"):
            rows.append(m6.row(f"{name} stable", "repeated steps agree", f"unstable at {x['unstable']}", None, "re-run before concluding"))
    return m6.write_result(a.out, "Independent scaling (flash sale)", rows)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    def common(sp, cluster=True):
        sp.add_argument("--namespace", default=os.environ.get("M6_NAMESPACE", "v2-lab"))
        sp.add_argument("--pg-cluster")
        sp.add_argument("--out", help="write <out>.md and <out>.json")
    d = sub.add_parser("deploy"); common(d)
    d.add_argument("--before", required=True); d.add_argument("--after", required=True)
    d.add_argument("--target", required=True); d.add_argument("--k6-summary")
    i = sub.add_parser("isolation"); common(i)
    i.add_argument("--timeline", required=True); i.add_argument("--k6-csv"); i.add_argument("--k6-summary", required=True)
    i.add_argument("--outage", default="10m"); i.add_argument("--budget", default="5m")
    c = sub.add_parser("consistency"); common(c)
    c.add_argument("--timeline", required=True); c.add_argument("--k6-summary", required=True)
    c.add_argument("--outage", default="10m"); c.add_argument("--budget", default="5m"); c.add_argument("--poll-note", default="15s")
    s = sub.add_parser("stock"); common(s)
    s.add_argument("--tag", type=int, required=True); s.add_argument("--k6-summary", required=True)
    s.add_argument("--stock", type=int, default=200); s.add_argument("--wait", default="3m")
    k = sub.add_parser("scaling"); common(k)
    k.add_argument("--baseline", required=True); k.add_argument("--scaled", required=True); k.add_argument("--factor", type=float, default=2.0)
    a = p.parse_args()
    if a.cmd != "scaling":
        m6.guard_or_exit(a.namespace)
    v = {"deploy": cmd_deploy, "isolation": cmd_isolation, "consistency": cmd_consistency, "stock": cmd_stock, "scaling": cmd_scaling}[a.cmd](a)
    sys.exit({"PASS": 0, "FAIL": 1}.get(v, 2))


if __name__ == "__main__":
    main()
