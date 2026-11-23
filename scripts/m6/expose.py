#!/usr/bin/env python3
"""How winpc (the load generator, on Tailscale, not on the lab's LAN) reaches the v2 stack: a temporary NodePort
Service <svc>-m6 per service the k6 scripts call (catalog, order, inventory), selecting the same pods as the stack's
own Service, reached at the lab node's Tailscale address. The stack's NetworkPolicies admit its HTTP port only from
its own namespace and from monitoring (default-deny-ingress + services), so `up` also adds a NetworkPolicy m6-loadgen
that admits the API port of those three services from anywhere (NetworkPolicies add up; the lab only). Everything is
labelled storefront.m6/expose=true and deleted by `down`. Neither object is in Git, so Argo CD leaves them alone.

  expose.py up   [--namespace v2-lab] [--address 100.76.236.114]   create them, write out/urls.env
  expose.py urls --catalog URL --order URL --inventory URL         the stack already has NodePorts: just write urls.env
  expose.py down [--namespace v2-lab]                               delete them

The capacity runner (slo-platform capacity.py) makes its own <svc>-capacity Services for its run and deletes them;
these are separate so the two never delete each other's. urls.env is what winpc.sh passes to k6.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m6lib as m6

LABEL = "storefront.m6/expose"
TARGETS = ("catalog", "order", "inventory")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URLS = os.path.join(HERE, "out", "urls.env")


def api_port(svc):
    """The API port of a Service: the one named http / api / connect, else 8080, else the first that is not metrics."""
    ports = svc["spec"]["ports"]
    for p in ports:
        if p.get("name") in ("http", "api", "connect", "grpc"):
            return p
    for p in ports:
        if p["port"] == 8080:
            return p
    rest = [p for p in ports if "metric" not in (p.get("name") or "") and p["port"] != 9090]
    if not rest:
        raise RuntimeError(f"Service {svc['metadata']['name']}: no API port among {ports}")
    return rest[0]


def manifest(ns, svc):
    p = api_port(svc)
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": f"{svc['metadata']['name']}-m6", "namespace": ns, "labels": {LABEL: "true"}},
            "spec": {"type": "NodePort", "selector": svc["spec"]["selector"],
                     "ports": [{"name": "api", "port": p["port"], "targetPort": p.get("targetPort", p["port"])}]}}


def netpol(ns):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "m6-loadgen", "namespace": ns, "labels": {LABEL: "true"}},
            "spec": {"podSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": list(TARGETS)}]},
                     "policyTypes": ["Ingress"],
                     "ingress": [{"ports": [{"port": "http", "protocol": "TCP"}]}]}}


def write_urls(path, urls):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        for k in ("catalog", "order", "inventory"):
            f.write(f"{k.upper()}_URL={urls[k]}\n")
    print(open(path).read().strip())
    print(f"→ {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["up", "urls", "down"])
    p.add_argument("--namespace", default=os.environ.get("M6_NAMESPACE", "v2-lab"))
    p.add_argument("--address", default=os.environ.get("M6_ADDRESS", "100.76.236.114"),
                   help="the lab node as winpc reaches it (Tailscale)")
    p.add_argument("--out", default=DEFAULT_URLS)
    for t in TARGETS:
        p.add_argument(f"--{t}", help=f"urls: base URL of {t}")
    a = p.parse_args()
    if a.cmd == "urls":
        if not all(getattr(a, t) for t in TARGETS):
            sys.exit("urls: give --catalog, --order and --inventory")
        write_urls(a.out, {t: getattr(a, t).rstrip("/") for t in TARGETS})
        return
    m6.guard_or_exit(a.namespace)
    if a.cmd == "down":
        print(m6.kubectl("delete", "service,networkpolicy", "-n", a.namespace, "-l", f"{LABEL}=true", "--ignore-not-found").strip())
        return
    m6.kubectl("apply", "-f", "-", stdin=json.dumps(netpol(a.namespace)))
    urls = {}
    for t in TARGETS:
        svc = json.loads(m6.kubectl("get", "service", t, "-n", a.namespace, "-o", "json"))
        m6.kubectl("apply", "-f", "-", stdin=json.dumps(manifest(a.namespace, svc)))
        got = json.loads(m6.kubectl("get", "service", f"{t}-m6", "-n", a.namespace, "-o", "json"))
        urls[t] = f"http://{a.address}:{got['spec']['ports'][0]['nodePort']}"
    write_urls(a.out, urls)


if __name__ == "__main__":
    main()
