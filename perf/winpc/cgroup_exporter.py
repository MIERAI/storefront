#!/usr/bin/env python3
"""CPU and memory of every container of this host, from cgroup v2, as Prometheus metrics on :9338.

cAdvisor (v0.49–v0.53) cannot follow Docker 29's containerd image store ("failed to identify the read-write layer
ID"), so the perf env reads the cgroups itself: /sys/fs/cgroup/system.slice/docker-<id>.scope/{cpu.stat,memory.current}
(systemd cgroup driver), names and compose labels from /var/lib/docker/containers/<id>/config.v2.json, and the host's
CPU from /proc/stat (not namespaced). Stdlib only.

  perf_container_cpu_seconds_total{service,container}          usage_usec / 1e6
  perf_container_cpu_throttled_seconds_total{service,container} throttled_usec / 1e6 (hit its CPU limit)
  perf_container_cpu_limit_cores{service,container}            cpu.max quota / period (absent: unlimited)
  perf_container_memory_bytes{service,container}               memory.current
  perf_host_cpu_seconds_total{mode="busy|idle|iowait|steal"}   /proc/stat, all CPUs
  perf_host_cpus                                               number of CPUs
"""
import glob
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CG = os.environ.get("CGROUP_ROOT", "/sys/fs/cgroup")
CONTAINERS = os.environ.get("DOCKER_CONTAINERS", "/var/lib/docker/containers")
_names = {}


def meta(cid):
    if cid not in _names:
        try:
            with open(os.path.join(CONTAINERS, cid, "config.v2.json")) as f:
                c = json.load(f)
            labels = c.get("Config", {}).get("Labels", {}) or {}
            _names[cid] = (labels.get("com.docker.compose.service", ""), c.get("Name", "").lstrip("/") or cid[:12])
        except OSError:
            return None   # not (yet) a container we can name: skip this round, do not cache
    return _names[cid]


def read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.partition(" ")
                out[k] = v.strip()
    except OSError:
        pass
    return out


def read1(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


def render():
    lines = []
    add = lines.append
    for d in sorted(glob.glob(os.path.join(CG, "system.slice", "docker-*.scope"))):
        cid = os.path.basename(d)[len("docker-"):-len(".scope")]
        m = meta(cid)
        if m is None:
            continue
        lab = f'service="{esc(m[0])}",container="{esc(m[1])}"'
        st = read_kv(os.path.join(d, "cpu.stat"))
        if "usage_usec" in st:
            add(f"perf_container_cpu_seconds_total{{{lab}}} {int(st['usage_usec']) / 1e6}")
            add(f"perf_container_cpu_throttled_seconds_total{{{lab}}} {int(st.get('throttled_usec', 0)) / 1e6}")
        mx = read1(os.path.join(d, "cpu.max"))
        if mx and not mx.startswith("max"):
            q, p = mx.split()
            add(f"perf_container_cpu_limit_cores{{{lab}}} {int(q) / int(p)}")
        mem = read1(os.path.join(d, "memory.current"))
        if mem:
            add(f"perf_container_memory_bytes{{{lab}}} {mem}")
    with open("/proc/stat") as f:
        first = f.readline().split()
        ncpu = sum(1 for line in f if line.startswith("cpu"))
    v = [int(x) for x in first[1:]]
    hz = os.sysconf("SC_CLK_TCK")
    user, nice, system, idle, iowait, irq, softirq, steal = (v + [0] * 8)[:8]
    for mode, val in (("busy", user + nice + system + irq + softirq), ("idle", idle), ("iowait", iowait), ("steal", steal)):
        add(f'perf_host_cpu_seconds_total{{mode="{mode}"}} {val / hz}')
    add(f"perf_host_cpus {ncpu}")
    return "\n".join(lines) + "\n"


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "9338"))), H).serve_forever()
