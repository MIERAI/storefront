#!/usr/bin/env python3
"""Markdown tables from knee.py results (perf.sh fetch → results/results/*.json).

  report.py results/results/durable-base-*.json results/results/durable-x3-*.json
"""
import json
import sys


def pct(v):
    return "–" if v is None else f"{v * 100:.1f}%"


def num(v, f="{:.2f}"):
    return "–" if v is None else f.format(v)


def main(paths):
    print("| run | knee | first fail | unstable |")
    print("|---|---|---|---|")
    docs = []
    for p in paths:
        with open(p) as f:
            d = json.load(f)
        docs.append(d)
        print(f"| {d['label']} | **{d.get('knee')}/s** | {d.get('first_fail')}/s | {', '.join(map(str, sorted(d.get('unstable', [])))) or '–'} |")
    for d in docs:
        print(f"\n**{d['label']}** (warm-up {d['warmup_s']} s, measured {d['measure_s']} s per step)\n")
        print("| rate | verdict | accepted/s | final/s | place p95 | ≤0.5 s | saga p95 | ≤10 s | order | inventory | pg-order | pg-inventory | rabbitmq | host busy | drain |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for s in d["steps"]:
            m = s["metrics"]
            c = m["cpu"]
            print(f"| {s['rate']} | {'holds' if s['holds'] else '**fails**'} | {m['accepted_per_s']:.1f} | {m['completed_per_s']:.1f}"
                  f" | {num(m['place_p95_s'], '{:.3f}')} s | {pct(m['latency_ok_ratio'])} | {num(m['saga_p95_s'], '{:.2f}')} s | {pct(m['saga_ok_ratio'])}"
                  f" | {c.get('order', 0):.2f} | {c.get('inventory', 0):.2f} | {c.get('pg-order', 0):.2f} | {c.get('pg-inventory', 0):.2f}"
                  f" | {c.get('rabbitmq', 0):.2f} | {num(m['host_busy_cores'], '{:.1f}')}/{m['host_cpus']:.0f} | {s['drain_s']} s |")


if __name__ == "__main__":
    main(sys.argv[1:])
