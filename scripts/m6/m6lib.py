"""Shared pieces of the M6 verification tools (docs/m6-verification.md): the lab guard, kubectl, read-only SQL in the
stack's Postgres, Prometheus through the API server's service proxy, and the pass/fail table.

Every tool that talks to a cluster calls guard() first. It refuses to run unless ALL of these hold:
  - the kubeconfig is an explicit file (M6_KUBECONFIG, default ~/.kube/xiaoxin.yaml), never ~/.kube/config;
  - its current context is exactly LAB_CONTEXT ("xiaoxin"), and the API server agrees that every node carries
    slo-platform/lab=true (the label the platform's own capacity and regression runners require);
  - the namespace is the v2 stack's: it matches NAMESPACE_RE, is not one of the other applications' namespaces, exists,
    and runs the four Deployments the promises are about (catalog, order, inventory, payment).
The production cluster (laptop) fails the context check and the node-label check, so a mistyped flag cannot reach it.

Standard library only (Python >= 3.9: winpc's Anaconda is 3.9).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

LAB_CONTEXT = "xiaoxin"
LAB_LABEL = "slo-platform/lab"
NAMESPACE_RE = re.compile(r"^[a-z0-9-]*v2[a-z0-9-]*$")        # v2-lab, storefront-v2-lab, …
OTHER_APPS_RE = re.compile(r"^(catalog|api-factory|sample-app|storefront)-(dev|staging|prod)$|^kube-|^(monitoring|argocd|"
                           r"chaos-mesh|cnpg-system|slo-experiments|traffic|default)$")
SERVICES = ("catalog", "order", "inventory", "payment")       # the Deployments the four promises are about
ALL_SERVICES = SERVICES + ("notification",)
PROMETHEUS = "monitoring/kps-prometheus:9090"
TERMINAL = {6: "CONFIRMED", 7: "CANCELLED"}
STATUS = {1: "PENDING", 2: "RESERVING", 3: "CHARGING", 4: "CONFIRMING", 5: "RELEASING", 6: "CONFIRMED", 7: "CANCELLED"}


class GuardError(RuntimeError):
    pass


def kubeconfig():
    path = os.path.expanduser(os.environ.get("M6_KUBECONFIG", "~/.kube/xiaoxin.yaml"))
    check_kubeconfig_path(path)
    return path


def check_kubeconfig_path(path):
    real = os.path.realpath(path)
    if real == os.path.realpath(os.path.expanduser("~/.kube/config")):
        raise GuardError("refusing ~/.kube/config: give the lab's own kubeconfig (M6_KUBECONFIG)")
    if "laptop" in os.path.basename(real):
        raise GuardError(f"refusing {path}: that is the production cluster's kubeconfig")
    if not os.path.isfile(real):
        raise GuardError(f"no kubeconfig at {path}")


def kube_base():
    return ["kubectl", "--kubeconfig", kubeconfig(), "--context", LAB_CONTEXT]


def kubectl(*args, stdin=None, check=True, timeout=120):
    r = subprocess.run(kube_base() + list(args), input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:4])}…: {r.stderr.strip()[-400:]}")
    return r.stdout


def check_namespace_name(ns):
    if not ns or not NAMESPACE_RE.match(ns):
        raise GuardError(f"namespace {ns!r} is not the v2 stack's (must match {NAMESPACE_RE.pattern})")
    if OTHER_APPS_RE.match(ns):
        raise GuardError(f"namespace {ns!r} belongs to something else")


def guard(ns, need=SERVICES):
    """Raise GuardError unless this is the lab and ns is the v2 stack's namespace (see the module docstring)."""
    check_namespace_name(ns)
    r = subprocess.run(["kubectl", "--kubeconfig", kubeconfig(), "config", "current-context"],
                       capture_output=True, text=True)
    current = r.stdout.strip()
    if current != LAB_CONTEXT:
        raise GuardError(f"kubeconfig's current context is {current!r}, not {LAB_CONTEXT!r}")
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    missing = [n["metadata"]["name"] for n in nodes if n["metadata"].get("labels", {}).get(LAB_LABEL) != "true"]
    if not nodes or missing:
        raise GuardError(f"not the lab: nodes without {LAB_LABEL}=true: {', '.join(missing) or '(no nodes)'}")
    if subprocess.run(kube_base() + ["get", "namespace", ns], capture_output=True).returncode != 0:
        raise GuardError(f"namespace {ns} does not exist on {LAB_CONTEXT}")
    have = set(kubectl("get", "deployments", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}").split())
    lacking = [s for s in need if s not in have]
    if lacking:
        raise GuardError(f"{ns} has no Deployment {', '.join(lacking)}: not the v2 stack")
    return True


def guard_or_exit(ns, need=SERVICES):
    try:
        guard(ns, need)
    except GuardError as e:
        sys.exit(f"guard: {e}")


# --- Postgres (read only) --------------------------------------------------------------------------------------

def primary_pod(ns, cluster=None):
    """The CNPG primary pod of the stack (cluster: its name when the namespace has more than one)."""
    sel = "cnpg.io/instanceRole=primary" + (f",cnpg.io/cluster={cluster}" if cluster else "")
    pods = kubectl("get", "pods", "-n", ns, "-l", sel, "-o", "jsonpath={.items[*].metadata.name}").split()
    if len(pods) != 1:
        raise RuntimeError(f"expected one CNPG primary in {ns} ({sel}), found {pods or 'none'}: give --pg-cluster")
    return pods[0]


def sql(ns, db, query, pod=None, cluster=None):
    """Rows (lists of strings) of a query run in a READ ONLY session (default_transaction_read_only=on) on the
    primary, through kubectl exec + psql over the pod's local socket. No credentials leave the cluster."""
    pod = pod or primary_pod(ns, cluster)
    conninfo = f"dbname={db} options='-c default_transaction_read_only=on -c statement_timeout=60000'"
    out = kubectl("exec", "-n", ns, pod, "-c", "postgres", "--", "psql", conninfo, "-X", "-q", "-At", "-F", "\t",
                  "-v", "ON_ERROR_STOP=1", "-c", query)
    return [line.split("\t") for line in out.splitlines() if line != ""]


def databases(ns=None, pod=None):
    """Database name per service: M6_DB_<SERVICE> overrides; else the service's name, or its plural when only that
    exists on the server (the lab stack names order's database "orders": ORDER is a reserved word)."""
    have = set()
    if ns:
        have = {r[0] for r in sql(ns, "postgres", "SELECT datname FROM pg_database", pod=pod)}
    out = {}
    for s in ALL_SERVICES:
        env = os.environ.get(f"M6_DB_{s.upper()}")
        out[s] = env or (s + "s" if s not in have and s + "s" in have else s)
    return out


# --- Prometheus ------------------------------------------------------------------------------------------------

def prom_path(spec=PROMETHEUS):
    m = re.fullmatch(r"([a-z0-9-]+)/([a-z0-9-]+):(\d+)", spec)
    if not m:
        raise ValueError(f"not <namespace>/<service>:<port>: {spec!r}")
    return f"/api/v1/namespaces/{m.group(1)}/services/{m.group(2)}:{m.group(3)}/proxy/api/v1/"


def prom(q, at=None, spec=PROMETHEUS):
    """Instant query → [(labels, float)]; [] when Prometheus has nothing (or cannot be reached: the caller reports n/a)."""
    params = {"query": q}
    if at:
        params["time"] = f"{at:.3f}"
    try:
        out = json.loads(kubectl("get", "--raw", prom_path(spec) + "query?" + urllib.parse.urlencode(params)))
    except (RuntimeError, ValueError) as e:
        print(f"warning: Prometheus query failed: {e}", file=sys.stderr)
        return []
    return [(r["metric"], float(r["value"][1])) for r in out.get("data", {}).get("result", [])]


def prom_scalar(q, at=None):
    """Sum of an instant query's values, None when it returns nothing."""
    res = prom(q, at)
    return sum(v for _, v in res) if res else None


# --- results ---------------------------------------------------------------------------------------------------

def row(check, criterion, measured, ok, note=""):
    """One line of a promise's table. ok: True (PASS), False (FAIL), None (N/A: could not be measured) or "info"
    (shown, not judged)."""
    v = "INFO" if ok == "info" else "PASS" if ok is True else "FAIL" if ok is False else "N/A"
    return {"check": check, "criterion": criterion, "measured": measured, "verdict": v, "note": note}


def verdict(rows):
    """A promise holds when every row passes; any FAIL fails it; otherwise (some N/A) it is INCONCLUSIVE."""
    vs = [r["verdict"] for r in rows if r["verdict"] != "INFO"]
    if "FAIL" in vs:
        return "FAIL"
    return "PASS" if vs and all(v == "PASS" for v in vs) else "INCONCLUSIVE"


def table(title, rows):
    out = [f"### {title}: **{verdict(rows)}**", "", "| Check | Criterion | Measured | Verdict | Note |", "|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['check']} | {r['criterion']} | {r['measured']} | {r['verdict']} | {r['note']} |")
    return "\n".join(out) + "\n"


def write_result(path, title, rows, extra=None):
    """Markdown table to stdout and <path>.md, rows (+extra) to <path>.json."""
    md = table(title, rows)
    print(md)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path + ".md", "w", encoding="utf-8") as f:
            f.write(f"<!-- {time.strftime('%Y-%m-%d %H:%M:%S %z')} -->\n" + md)
        with open(path + ".json", "w", encoding="utf-8") as f:
            json.dump({"title": title, "verdict": verdict(rows), "rows": rows, **(extra or {})}, f, indent=2)
    return verdict(rows)


def fmt_pct(x, digits=2):
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
