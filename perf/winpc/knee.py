#!/usr/bin/env python3
"""Find the place-order knee of the perf env (winpc) under a flash sale: runs on the load generator (xiaoxin), steps
k6 (flash.js) through a list of arrival rates, judges every step from the perf env's Prometheus, repeats the points
around the knee. Stdlib only.

  knee.py --label baseline [--rates 10,15,20,...] [--warmup 30] [--measure 60] [--out results/]

A step HOLDS when, over its measurement window (the last --measure seconds of the step):
  server errors       PlaceOrder answers with a server error code            < 0.5%      (rpc_requests_total)
  placement latency   PlaceOrder answered within 0.5 s (server side)        >= 99%       (rpc_request_duration_seconds)
  saga keeps up       orders final within 10 s (OrderSaga)                  >= 99%       (factory_sm_completion_seconds)
  saga throughput     orders final / orders placed in the window            >= 97%
  client              k6: non-accepted answers (incl. network) < 0.5%; dropped iterations < 1% of the planned
The last row guards the measurement itself (a load generator that cannot keep up is not a result).

Search: ascend through --rates until a step fails; then repeat the failing rate and the last holding one; if the
latter fails its repeat, repeat the next lower one, and so on. KNEE = the highest rate whose two runs both held (and
every lower rate held its first run). Between steps the system drains: no order in flight, no outbox row pending, no
message queued (at most --drain-max seconds; the time is recorded, a step that does not drain is noted).

Output: <out>/<label>-<YYYYmmdd-HHMM>.json (every step: verdict, rows, CPU per service, Postgres / RabbitMQ / HAProxy,
host) and a table on stdout.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLACE = "/order.order.v1.OrderService/PlaceOrder"
SERVER_ERRORS = 'code=~"internal|unavailable|unknown|deadline_exceeded|data_loss|resource_exhausted"'
RATES = [10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 100, 120, 140, 160, 200, 240, 280, 320, 400]
OBJ = {"server_err_max": 0.005, "latency_min": 0.99, "saga_min": 0.99, "throughput_min": 0.97,
       "client_fail_max": 0.005, "dropped_max": 0.01}


class Prom:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def q(self, query, at):
        u = self.url + "/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": f"{at:.3f}"})
        with urllib.request.urlopen(u, timeout=20) as r:
            d = json.load(r)
        if d.get("status") != "success":
            raise RuntimeError(f"{query}: {d}")
        return d["data"]["result"]

    def scalar(self, query, at):
        res = self.q(query, at)
        if not res:
            return None
        v = float(res[0]["value"][1])
        return None if v != v else v   # NaN → None

    def by(self, query, label, at):
        return {r["metric"].get(label, ""): float(r["value"][1]) for r in self.q(query, at)}


def backlog(prom, at=None):
    at = at or time.time()
    flight = prom.scalar('sum(max by (state) (factory_sm_instances{machine="OrderSaga"}))', at) or 0
    outbox = prom.scalar("sum(max by (app) (factory_outbox_pending))", at) or 0
    queued = prom.scalar('sum(rabbitmq_queue_messages{queue!~".*(dlq|DLQ|dead).*"})', at) or 0
    return flight, outbox, queued


def drain(prom, max_s):
    t0 = time.time()
    while True:
        f, o, q = backlog(prom)
        if f == 0 and o == 0 and q == 0:
            return round(time.time() - t0, 1), True
        if time.time() - t0 > max_s:
            return round(time.time() - t0, 1), False
        time.sleep(5)


def measure(prom, t_end, w):
    W = f"{int(w)}s"
    P = f'procedure="{PLACE}"'
    inc = lambda m: f"sum(increase({m}[{W}]))"
    placed = prom.scalar(inc(f"rpc_requests_total{{{P}}}"), t_end) or 0
    errs = prom.scalar(inc(f"rpc_requests_total{{{P},{SERVER_ERRORS}}}"), t_end) or 0
    lat_ok = prom.scalar(inc(f'rpc_request_duration_seconds_bucket{{{P},le=~"0.5"}}'), t_end) or 0
    lat_n = prom.scalar(inc(f"rpc_request_duration_seconds_count{{{P}}}"), t_end) or 0
    saga_ok = prom.scalar(inc('factory_sm_completion_seconds_bucket{machine="OrderSaga",le=~"10(\\\\.0)?"}'), t_end) or 0
    saga_n = prom.scalar(inc('factory_sm_completion_seconds_count{machine="OrderSaga"}'), t_end) or 0
    q = lambda expr: prom.scalar(expr, t_end)
    hq = lambda phi, m, sel: q(f"histogram_quantile({phi}, sum by (le) (rate({m}_bucket{{{sel}}}[{W}])))")
    cpu = prom.by(f"sum by (service) (rate(perf_container_cpu_seconds_total[{W}]))", "service", t_end)
    thr = prom.by(f"sum by (service) (rate(perf_container_cpu_throttled_seconds_total[{W}]))", "service", t_end)
    lim = prom.by("sum by (service) (perf_container_cpu_limit_cores)", "service", t_end)
    reps = prom.by("count by (service) (perf_container_cpu_limit_cores)", "service", t_end)
    host_busy = q(f'sum(rate(perf_host_cpu_seconds_total{{mode="busy"}}[{W}]))')
    ncpu = q("max(perf_host_cpus)") or 0
    return {
        "placed": placed, "server_errors": errs,
        "server_err_ratio": errs / placed if placed else None,
        "latency_ok_ratio": lat_ok / lat_n if lat_n else None,
        "saga_ok_ratio": saga_ok / saga_n if saga_n else None,
        "saga_completed": saga_n,
        "throughput_ratio": saga_n / placed if placed else None,
        "place_p95_s": hq(0.95, "rpc_request_duration_seconds", P),
        "place_p99_s": hq(0.99, "rpc_request_duration_seconds", P),
        "saga_p95_s": hq(0.95, "factory_sm_completion_seconds", 'machine="OrderSaga"'),
        "saga_p99_s": hq(0.99, "factory_sm_completion_seconds", 'machine="OrderSaga"'),
        "accepted_per_s": placed / w,
        "completed_per_s": saga_n / w,
        "cpu": {k: round(v, 3) for k, v in sorted(cpu.items())},
        "cpu_limit": {k: round(v, 2) for k, v in sorted(lim.items())},
        "replicas": {k: int(v) for k, v in sorted(reps.items())},
        # share of the window the service's containers spent throttled at their CPU limit (sum over replicas / replicas)
        "throttled": {k: round(v / max(1, reps.get(k, 1)), 3) for k, v in sorted(thr.items()) if v > 0.001},
        "host_busy_cores": host_busy, "host_cpus": ncpu,
        "outbox_pending_max": q(f"max_over_time((sum(max by (app) (factory_outbox_pending)))[{W}:5s])"),
        "outbox_age_max_s": q(f"max_over_time((max(factory_outbox_oldest_pending_age_seconds))[{W}:5s])"),
        "saga_in_flight_max": q(f'max_over_time((sum(max by (state) (factory_sm_instances{{machine="OrderSaga"}})))[{W}:5s])'),
        "queued_max": q(f'max_over_time((sum(rabbitmq_queue_messages{{queue!~".*(dlq|DLQ|dead).*"}}))[{W}:5s])'),
        "dlq": q('sum(rabbitmq_queue_messages{queue=~".*(dlq|DLQ|dead).*"})'),
        "handler_p95_s": prom.by(f"histogram_quantile(0.95, sum by (le, app) (rate(factory_handler_duration_seconds_bucket[{W}])))", "app", t_end),
    }


def judge(m, k6):
    rows = []
    def row(name, val, ok):
        rows.append({"row": name, "value": val, "ok": bool(ok)})
    row("server errors < 0.5%", m["server_err_ratio"], m["server_err_ratio"] is not None and m["server_err_ratio"] < OBJ["server_err_max"])
    row("placement <= 0.5s >= 99%", m["latency_ok_ratio"], m["latency_ok_ratio"] is not None and m["latency_ok_ratio"] >= OBJ["latency_min"])
    row("saga final <= 10s >= 99%", m["saga_ok_ratio"], m["saga_ok_ratio"] is not None and m["saga_ok_ratio"] >= OBJ["saga_min"])
    row("saga throughput >= 97%", m["throughput_ratio"], m["throughput_ratio"] is not None and m["throughput_ratio"] >= OBJ["throughput_min"])
    fail = None if k6 is None else 1 - (k6.get("metrics", {}).get("m6_order_ok", {}).get("rate", 0))
    row("client not accepted < 0.5%", fail, fail is not None and fail < OBJ["client_fail_max"])
    planned = (k6 or {}).get("planned") or 0
    dropped = ((k6 or {}).get("dropped_iterations") or 0) / planned if planned else None
    row("k6 dropped < 1%", dropped, dropped is not None and dropped < OBJ["dropped_max"])
    return all(r["ok"] for r in rows), rows


def run_step(a, prom, rate, tag):
    dur = a.warmup + a.measure
    summary = os.path.join(a.out, "k6", f"{a.label}-{tag}-{rate}.json")
    os.makedirs(os.path.dirname(summary), exist_ok=True)
    cmd = [a.k6, "run", "-q", "--no-color", "-e", f"ORDER_URL={a.order_url}", "-e", f"RATE={rate}",
           "-e", f"DURATION={dur}s", "-e", f"TAG={tag}", "-e", f"SUMMARY={summary}", os.path.join(a.k6_dir, "flash.js")]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True)
    t1 = time.time()
    k6 = None
    try:
        with open(summary) as f:
            k6 = json.load(f)
        k6["planned"] = rate * dur
    except (OSError, ValueError):
        sys.stderr.write(p.stdout[-2000:] + p.stderr[-2000:])
    # the measurement window: the last `measure` seconds of the load (k6 starts ~1 s after t0, ends at t1), minus 2 s;
    # evaluated once the scrapes after it exist (5 s interval)
    t_end = min(t1, t0 + 1 + dur) - 2
    time.sleep(max(0, t_end + 11 - time.time()))
    m = measure(prom, t_end, a.measure)
    ok, rows = judge(m, k6)
    drained_s, drained = drain(prom, a.drain_max)
    k6s = None if k6 is None else {k: k6.get(k) for k in ("http_reqs", "http_failed_rate", "dropped_iterations", "p95_ms", "p99_ms", "vus_max")}
    return {"rate": rate, "tag": tag, "start": t0, "end": t1, "holds": ok, "rows": rows, "metrics": m, "k6": k6s,
            "drain_s": drained_s, "drained": drained}


def fmt(v, pct=False):
    if v is None:
        return "-"
    return f"{v * 100:.2f}%" if pct else f"{v:.3f}"


def line(s):
    m = s["metrics"]
    cpu = m["cpu"]
    top = ", ".join(f"{k} {v:.2f}" for k, v in sorted(cpu.items(), key=lambda kv: -kv[1])[:5])
    return (f"{s['rate']:>5}/s  {'HOLDS' if s['holds'] else 'FAILS':5}  acc {m['accepted_per_s']:.1f}/s fin {m['completed_per_s']:.1f}/s"
            f"  place p95 {fmt(m['place_p95_s'])}s le0.5 {fmt(m['latency_ok_ratio'], True)}"
            f"  saga p95 {fmt(m['saga_p95_s'])}s le10 {fmt(m['saga_ok_ratio'], True)}"
            f"  err {fmt(m['server_err_ratio'], True)} k6p95 {(s['k6'] or {}).get('p95_ms') or 0:.0f}ms"
            f"  host {fmt(m['host_busy_cores'])}/{m['host_cpus']:.0f}  drain {s['drain_s']}s  | {top}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--rates", default=",".join(map(str, RATES)))
    ap.add_argument("--start", type=float, default=0, help="skip rates below this")
    ap.add_argument("--warmup", type=int, default=30)
    ap.add_argument("--measure", type=int, default=60)
    ap.add_argument("--drain-max", type=int, default=300)
    ap.add_argument("--prom", default="http://100.106.140.35:19090")
    ap.add_argument("--order-url", default="http://100.106.140.35:18080")
    ap.add_argument("--k6", default=os.path.expanduser("~/sf2perf/bin/k6"))
    ap.add_argument("--k6-dir", default=os.path.expanduser("~/sf2perf/k6"))
    ap.add_argument("--out", default=os.path.expanduser("~/sf2perf/results"))
    ap.add_argument("--no-repeat", action="store_true", help="ascend only (exploration)")
    a = ap.parse_args()
    prom = Prom(a.prom)
    rates = [float(x) if "." in x else int(x) for x in a.rates.split(",") if float(x) >= a.start]
    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    path = os.path.join(a.out, f"{a.label}-{stamp}.json")
    doc = {"label": a.label, "started": stamp, "objectives": OBJ, "warmup_s": a.warmup, "measure_s": a.measure, "steps": []}
    seq = [0]

    def step(rate):
        seq[0] += 1
        tag = int(time.strftime("%m%d%H%M")) * 10 + seq[0] % 10
        d, ok = drain(prom, a.drain_max)
        if not ok:
            print(f"   (not drained before {rate}/s after {d}s: in flight / outbox / queued = {backlog(prom)})", flush=True)
        s = run_step(a, prom, rate, tag)
        doc["steps"].append(s)
        with open(path, "w") as f:
            json.dump(doc, f, indent=1)
        print(line(s), flush=True)
        return s["holds"]

    print(f"knee {a.label}: rates {rates}, warmup {a.warmup}s, measure {a.measure}s → {path}", flush=True)
    first = {}
    for r in rates:
        first[r] = step(r)
        if not first[r]:
            break
    held = [r for r in rates if first.get(r)]
    failed = [r for r in rates if r in first and not first[r]]
    knee = None
    if not a.no_repeat and held:
        if failed:
            step(failed[0])                       # the failing rate, a second time
        for r in reversed(held):                  # the highest that holds twice
            if step(r):
                knee = r
                break
    doc["first_fail"] = failed[0] if failed else None
    doc["knee"] = knee
    doc["unstable"] = [r for r in {s["rate"] for s in doc["steps"]}
                       if len({s["holds"] for s in doc["steps"] if s["rate"] == r}) > 1]
    with open(path, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"KNEE {a.label}: {knee}/s (first failing rate {doc['first_fail']}; unstable {sorted(doc['unstable'])}) → {path}", flush=True)


if __name__ == "__main__":
    main()
