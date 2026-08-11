#!/usr/bin/env python3
"""sysbench benchmark runner + HTTP results server.

PID 1 in the container is this server, not sysbench. That is deliberate:
a pod whose main process is the benchmark itself runs exactly once and then
dies (CrashLoopBackOff under a Deployment). Here the benchmark is a child
process that can be triggered on startup and re-triggered any number of times
over HTTP, while the results stay served from the same pod.

Only the Python standard library is used - nothing to pip install.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import templates

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def env(name, default=""):
    value = os.environ.get(name, "").strip()
    return value if value else default


PORT = int(env("PORT", "8080"))
RESULTS_DIR = env("RESULTS_DIR", "/var/results")
HISTORY_MAX = int(env("HISTORY_MAX", "20"))
RUN_TIMEOUT = int(env("RUN_TIMEOUT", "3600"))
AUTORUN = env("AUTORUN", "true").lower() in ("1", "true", "yes")
NODE_NAME = env("NODE_NAME", "unknown")
POD_NAME = env("POD_NAME", "unknown")

DEFAULTS = {
    "cpu": {
        "cpu_max_prime": env("CPU_MAX_PRIME", "20000"),
        "threads": env("CPU_THREADS", "1"),
        # empty -> do not pass --time, sysbench applies its own default (10s)
        "time": env("CPU_TIME", ""),
    },
    "memory": {
        "block_size": env("MEMORY_BLOCK_SIZE", "1M"),
        "total_size": env("MEMORY_TOTAL_SIZE", "100G"),
        "threads": env("MEMORY_THREADS", "1"),
        # 0 = no time limit, so the full total-size actually gets transferred.
        # sysbench defaults to --time=10 and would otherwise cut the run short.
        "time": env("MEMORY_TIME", "0"),
    },
}

KINDS = ("cpu", "memory")


# --------------------------------------------------------------------------
# host / cgroup context captured around every run
# --------------------------------------------------------------------------


def _read(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""


def cgroup_cpu_stat():
    """nr_throttled / throttled_usec from cgroup v2, falling back to v1.

    Sampled before and after each run: a non-zero delta means the CFS quota
    throttled the benchmark, which invalidates the numbers as a measure of
    hardware speed.
    """
    raw = _read("/sys/fs/cgroup/cpu.stat") or _read("/sys/fs/cgroup/cpu/cpu.stat")
    stat = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            stat[parts[0]] = int(parts[1])
    return stat


def cgroup_cpu_quota():
    """Effective CPU limit as seen from inside the container, in cores."""
    v2 = _read("/sys/fs/cgroup/cpu.max").split()
    if len(v2) == 2 and v2[0].isdigit() and v2[1].isdigit():
        return round(int(v2[0]) / int(v2[1]), 3)
    quota = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").strip()
    period = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us").strip()
    if quota.isdigit() and period.isdigit():
        return round(int(quota) / int(period), 3)
    return None


def cpu_mhz():
    """Per-CPU MHz from /proc/cpuinfo.

    Inside a KVM guest this is usually a frozen nominal value (APERF/MPERF are
    not exposed), so it is recorded as context rather than trusted as a
    measurement.
    """
    values = re.findall(r"^cpu MHz\s*:\s*([0-9.]+)", _read("/proc/cpuinfo"), re.M)
    return [float(v) for v in values]


def physical_package_ids():
    """physical_package_id per CPU - verifies the sockets/cores topology fix."""
    ids = []
    base = "/sys/devices/system/cpu"
    try:
        cpus = sorted(
            entry for entry in os.listdir(base) if re.fullmatch(r"cpu[0-9]+", entry)
        )
    except OSError:
        return ids
    for cpu in cpus:
        value = _read(f"{base}/{cpu}/topology/physical_package_id").strip()
        if value:
            ids.append(int(value))
    return ids


def collect_context():
    mhz = cpu_mhz()
    packages = physical_package_ids()
    return {
        "node_name": NODE_NAME,
        "pod_name": POD_NAME,
        "nproc": os.cpu_count(),
        "cpu_quota_cores": cgroup_cpu_quota(),
        "cpu_mhz_min": min(mhz) if mhz else None,
        "cpu_mhz_max": max(mhz) if mhz else None,
        "physical_package_ids": sorted(set(packages)),
        "physical_package_count": len(set(packages)),
    }


def static_snapshot():
    """One-off snapshot of what the guest thinks the CPU is."""
    try:
        lscpu = subprocess.run(
            ["lscpu"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        lscpu = "(lscpu unavailable)"
    return {
        "lscpu": lscpu,
        "sysbench_version": sysbench_version(),
        "collected_at": now(),
        **collect_context(),
    }


def sysbench_version():
    try:
        out = subprocess.run(
            ["sysbench", "--version"], capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# --------------------------------------------------------------------------
# sysbench invocation and output parsing
# --------------------------------------------------------------------------


def build_command(kind, params):
    if kind == "cpu":
        cmd = [
            "sysbench",
            "cpu",
            f"--cpu-max-prime={params['cpu_max_prime']}",
            f"--threads={params['threads']}",
        ]
    else:
        cmd = [
            "sysbench",
            "memory",
            f"--memory-block-size={params['block_size']}",
            f"--memory-total-size={params['total_size']}",
            f"--threads={params['threads']}",
        ]
    if params.get("time") != "":
        cmd.append(f"--time={params['time']}")
    cmd.append("run")
    return cmd


NUM = r"([0-9]+(?:\.[0-9]+)?)"


def _grab(pattern, text, cast=float):
    match = re.search(pattern, text, re.M)
    return cast(match.group(1)) if match else None


def parse_output(kind, out):
    metrics = {
        "total_time_s": _grab(r"^\s*total time:\s+" + NUM + r"s", out),
        "total_events": _grab(r"^\s*total number of events:\s+([0-9]+)", out, int),
        "latency_min_ms": _grab(r"^\s*min:\s+" + NUM, out),
        "latency_avg_ms": _grab(r"^\s*avg:\s+" + NUM, out),
        "latency_max_ms": _grab(r"^\s*max:\s+" + NUM, out),
        "latency_p95_ms": _grab(r"^\s*95th percentile:\s+" + NUM, out),
    }
    if kind == "cpu":
        metrics["events_per_second"] = _grab(r"events per second:\s+" + NUM, out)
    else:
        match = re.search(r"Total operations:\s+[0-9]+\s*\(\s*" + NUM + r" per second", out)
        metrics["events_per_second"] = float(match.group(1)) if match else None
        transfer = re.search(NUM + r"\s+MiB transferred\s+\(\s*" + NUM + r"\s+MiB/sec", out)
        if transfer:
            metrics["transferred_mib"] = float(transfer.group(1))
            metrics["throughput_mib_s"] = float(transfer.group(2))
    if metrics["events_per_second"] is None and metrics["total_events"] and metrics["total_time_s"]:
        metrics["events_per_second"] = round(
            metrics["total_events"] / metrics["total_time_s"], 2
        )
    return metrics


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


class Runner:
    """Serialises benchmark runs and keeps their results.

    The lock matters: two sysbench processes competing for the same cores
    produce numbers that measure the contention, not the machine.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.current = None
        self.history = deque(maxlen=HISTORY_MAX)
        self.latest = {}
        self.snapshot = static_snapshot()
        self._load_persisted()

    def _load_persisted(self):
        if not os.path.isdir(RESULTS_DIR):
            return
        files = sorted(
            entry for entry in os.listdir(RESULTS_DIR) if entry.endswith(".json")
        )
        for name in files[-HISTORY_MAX:]:
            try:
                with open(os.path.join(RESULTS_DIR, name)) as handle:
                    result = json.load(handle)
            except (OSError, ValueError):
                continue
            self.history.append(result)
            self.latest[result.get("kind")] = result

    def busy(self):
        return self.current is not None

    def run(self, kind, params):
        """Blocking; callers that must not block should use run_async."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            started = time.monotonic()
            record = {
                "id": uuid.uuid4().hex[:12],
                "kind": kind,
                "params": params,
                "command": " ".join(build_command(kind, params)),
                "started_at": now(),
            }
            with self._state_lock:
                self.current = dict(record, status="running")

            before = cgroup_cpu_stat()
            context_before = collect_context()
            try:
                proc = subprocess.run(
                    build_command(kind, params),
                    capture_output=True,
                    text=True,
                    timeout=RUN_TIMEOUT,
                )
                stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired:
                stdout, stderr, code = "", f"timed out after {RUN_TIMEOUT}s", 124
            except OSError as exc:
                stdout, stderr, code = "", f"failed to execute sysbench: {exc}", 127
            after = cgroup_cpu_stat()
            context_after = collect_context()

            record.update(
                {
                    "finished_at": now(),
                    "duration_s": round(time.monotonic() - started, 2),
                    "exit_code": code,
                    "status": "ok" if code == 0 else "failed",
                    "stdout": stdout,
                    "stderr": stderr,
                    "metrics": parse_output(kind, stdout) if code == 0 else {},
                    "context_before": context_before,
                    "context_after": context_after,
                    "throttling": {
                        key: after.get(key, 0) - before.get(key, 0)
                        for key in ("nr_throttled", "throttled_usec", "nr_periods")
                        if key in after or key in before
                    },
                }
            )

            with self._state_lock:
                self.current = None
                self.history.append(record)
                self.latest[kind] = record
            self._persist(record)
            return record
        finally:
            self._lock.release()

    def _persist(self, record):
        if not RESULTS_DIR:
            return
        try:
            os.makedirs(RESULTS_DIR, exist_ok=True)
            path = os.path.join(
                RESULTS_DIR, f"{record['started_at'].replace(':', '')}-{record['kind']}-{record['id']}.json"
            )
            with open(path, "w") as handle:
                json.dump(record, handle)
        except OSError as exc:
            log(f"could not persist result ({exc}); keeping it in memory only")

    def run_async(self, kinds, params_by_kind):
        if self.busy() or self._lock.locked():
            return False

        def worker():
            for kind in kinds:
                self.run(kind, params_by_kind[kind])

        threading.Thread(target=worker, daemon=True, name="benchmark").start()
        return True

    def state(self):
        with self._state_lock:
            return {
                "running": self.current,
                "latest": dict(self.latest),
                "history": list(self.history),
                "defaults": DEFAULTS,
                "snapshot": self.snapshot,
            }


def resolve_params(kind, query):
    params = dict(DEFAULTS[kind])
    for key in params:
        values = query.get(key)
        if values and values[0].strip():
            params[key] = values[0].strip()
    return params


def log(message):
    print(f"[{now()}] {message}", flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

RUNNER = Runner()


class Handler(BaseHTTPRequestHandler):
    server_version = "sysbench-perf"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, indent=2), "application/json; charset=utf-8")

    def do_GET(self):
        route = urlparse(self.path)
        path = route.path.rstrip("/") or "/"

        if path in ("/healthz", "/readyz"):
            # Deliberately independent of benchmark state: a run in progress
            # must never look like an unhealthy pod.
            return self._json(200, {"status": "ok", "busy": RUNNER.busy()})
        if path == "/":
            return self._send(200, templates.PAGE, "text/html; charset=utf-8")
        if path == "/api/results":
            return self._json(200, RUNNER.state())
        if path == "/api/status":
            return self._json(200, {"running": RUNNER.current, "busy": RUNNER.busy()})
        if path in ("/cpu", "/memory"):
            kind = path.lstrip("/")
            result = RUNNER.latest.get(kind)
            if not result:
                return self._send(404, f"no {kind} result yet - POST /run/{kind}\n")
            return self._send(200, plain_report(result))
        if path == "/snapshot":
            return self._send(200, RUNNER.snapshot["lscpu"])
        return self._send(404, "not found\n")

    do_HEAD = do_GET

    def do_POST(self):
        route = urlparse(self.path)
        path = route.path.rstrip("/") or "/"
        query = parse_qs(route.query)

        if not path.startswith("/run/"):
            return self._send(404, "not found\n")
        target = path[len("/run/"):]
        kinds = list(KINDS) if target == "all" else [target]
        if any(kind not in KINDS for kind in kinds):
            return self._send(400, f"unknown test - use one of: {', '.join(KINDS)}, all\n")

        params = {kind: resolve_params(kind, query) for kind in kinds}
        if not RUNNER.run_async(kinds, params):
            return self._json(409, {"error": "a benchmark is already running"})
        return self._json(202, {"accepted": kinds, "params": params})


def plain_report(result):
    metrics = result.get("metrics", {})
    lines = [
        f"kind:        {result['kind']}",
        f"command:     {result['command']}",
        f"started:     {result['started_at']}",
        f"finished:    {result.get('finished_at')}",
        f"status:      {result['status']} (exit {result.get('exit_code')})",
        f"node:        {result['context_before']['node_name']}",
        f"pod:         {result['context_before']['pod_name']}",
        f"cpu quota:   {result['context_before']['cpu_quota_cores']} cores",
        f"packages:    {result['context_before']['physical_package_ids']}",
        f"throttling:  {result.get('throttling')}",
        "",
        "--- parsed metrics ---",
    ]
    lines += [f"{key}: {value}" for key, value in metrics.items()]
    lines += ["", "--- raw sysbench output ---", result.get("stdout", "")]
    if result.get("stderr"):
        lines += ["--- stderr ---", result["stderr"]]
    return "\n".join(lines) + "\n"


def main():
    log(f"sysbench: {RUNNER.snapshot['sysbench_version']}")
    log(f"node={NODE_NAME} pod={POD_NAME} nproc={RUNNER.snapshot['nproc']} "
        f"quota={RUNNER.snapshot['cpu_quota_cores']} cores")
    log(f"defaults: {json.dumps(DEFAULTS)}")

    server = ThreadingHTTPServer(("", PORT), Handler)
    server.daemon_threads = True

    def shutdown(signum, _frame):
        log(f"signal {signum} received, shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if AUTORUN:
        log("AUTORUN enabled - queueing cpu + memory")
        RUNNER.run_async(list(KINDS), {kind: dict(DEFAULTS[kind]) for kind in KINDS})

    log(f"listening on :{PORT}")
    server.serve_forever()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
