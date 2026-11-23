#!/usr/bin/env python3
"""Independent deploy, the evidence: what every workload and pod of the v2 stack looks like, so a snapshot before and
after releasing ONE service shows that nothing else changed or restarted (check.py deploy compares two).

  snapshot.py [--namespace v2-lab] --out out/deploy-before.json
  snapshot.py [--namespace v2-lab] --wait-rollout payment --baseline out/deploy-before.json [--soak 5m] --out out/deploy-after.json
      the second form waits until payment's revision differs from the baseline's and every pod is ready, then
      --soak longer (the window in which the neighbours must stay untouched and the SLOs hold), then snapshots

Per workload (Deployment, StatefulSet; CNPG and RabbitMQ pods are covered through their owners): generation,
observed generation, revision, the pod template's images. Per pod: uid, owner workload, start time, and per
container the restart count, the running container's start time and the image digest it runs (imageID).
Read only.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m6lib as m6


def owner_workload(pod):
    """'Deployment/payment', 'StatefulSet/rabbitmq', 'Cluster/postgres', … from the pod's controller reference."""
    for o in pod["metadata"].get("ownerReferences", []):
        if not o.get("controller"):
            continue
        if o["kind"] == "ReplicaSet":
            return "Deployment/" + re.sub(r"-[a-z0-9]{5,10}$", "", o["name"])
        return f"{o['kind']}/{o['name']}"
    return "none/" + pod["metadata"]["name"]


def workloads(items, kind):
    out = {}
    for w in items:
        md, spec, st = w["metadata"], w.get("spec", {}), w.get("status", {})
        out[f"{kind}/{md['name']}"] = {
            "generation": md.get("generation"), "observedGeneration": st.get("observedGeneration"),
            "revision": md.get("annotations", {}).get("deployment.kubernetes.io/revision") or st.get("currentRevision"),
            "replicas": spec.get("replicas"), "ready": st.get("readyReplicas", 0),
            "images": {c["name"]: c["image"] for c in spec.get("template", {}).get("spec", {}).get("containers", [])},
        }
    return out


def pods(items):
    out = {}
    for p in items:
        st = p.get("status", {})
        cs = {}
        for c in st.get("containerStatuses", []) or []:
            cs[c["name"]] = {"restarts": c.get("restartCount", 0), "imageID": c.get("imageID", ""), "image": c.get("image", ""),
                             "startedAt": (c.get("state", {}).get("running") or {}).get("startedAt"),
                             "ready": c.get("ready", False)}
        out[p["metadata"]["name"]] = {"uid": p["metadata"]["uid"], "owner": owner_workload(p), "phase": st.get("phase"),
                                      "startTime": st.get("startTime"), "containers": cs}
    return out


def snapshot(ns):
    get = lambda kind: json.loads(m6.kubectl("get", kind, "-n", ns, "-o", "json"))["items"]
    return {"namespace": ns, "taken": time.time(), "workloads": {**workloads(get("deployments"), "Deployment"),
                                                                 **workloads(get("statefulsets"), "StatefulSet")},
            "pods": pods(get("pods"))}


def secs(s):
    return int(s[:-1]) * {"s": 1, "m": 60, "h": 3600}[s[-1]] if s[-1] in "smh" else int(s)


def wait_rollout(ns, deploy, baseline, timeout):
    key = f"Deployment/{deploy}"
    was = baseline["workloads"][key]["revision"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = json.loads(m6.kubectl("get", "deployment", deploy, "-n", ns, "-o", "json"))
        rev = d["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
        st, want = d.get("status", {}), d["spec"].get("replicas", 1)
        done = (rev != was and st.get("updatedReplicas", 0) == want and st.get("readyReplicas", 0) == want
                and st.get("replicas", 0) == want and st.get("observedGeneration") == d["metadata"]["generation"])
        print(f"{time.strftime('%H:%M:%S')} {deploy}: revision {rev} (was {was}), ready {st.get('readyReplicas', 0)}/{want}", flush=True)
        if done:
            print(f"{deploy} rolled out: {', '.join(c['image'] for c in d['spec']['template']['spec']['containers'])}")
            return
        time.sleep(15)
    sys.exit(f"{deploy} did not roll out within {timeout}s (did the release workflow and its promote commit run?)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--namespace", default=os.environ.get("M6_NAMESPACE", "v2-lab"))
    p.add_argument("--out", required=True)
    p.add_argument("--wait-rollout", metavar="DEPLOYMENT")
    p.add_argument("--baseline", help="with --wait-rollout: the snapshot taken before the release")
    p.add_argument("--soak", default="5m")
    p.add_argument("--timeout", default="45m", help="with --wait-rollout: give up after this")
    a = p.parse_args()
    m6.guard_or_exit(a.namespace)
    if a.wait_rollout:
        wait_rollout(a.namespace, a.wait_rollout, m6.load_json(a.baseline), secs(a.timeout))
        print(f"soaking {a.soak}…", flush=True)
        time.sleep(secs(a.soak))
    s = snapshot(a.namespace)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    print(f"{len(s['workloads'])} workloads, {len(s['pods'])} pods → {a.out}")
    for name, w in sorted(s["workloads"].items()):
        print(f"  {name:<32} rev {w['revision']}  ready {w['ready']}/{w['replicas']}  {', '.join(w['images'].values())}")


if __name__ == "__main__":
    main()
