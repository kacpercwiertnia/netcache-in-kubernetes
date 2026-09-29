#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shlex
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from environment import remote_home, ssh_cmd, metrics_mode


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_ROOT = ROOT / ".generated" / "experiments"
RUN_TOPOLOGY = ROOT / "scripts" / "run_topology.sh"

TERMINAL_PHASES = {"Succeeded", "Failed"}
DEFAULT_CAPACITY_ENTRIES = 65536
DEFAULT_METRICS_POLL_SECONDS = 5
NETCACHE_HIT_CPU_COST = 1.0
NETCACHE_MISS_CPU_COST = 8.0
NETCACHE_CPU_BUDGET_PPS = 100000.0
NETCACHE_RATE_EWMA_ALPHA = 0.5


@dataclass
class DeviceMetricState:
    previous: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    peaks: dict[str, float] | None = None
    smoothed: dict[str, float] | None = None


def load_cases(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text())
    cases = raw["cases"] if isinstance(raw, dict) else raw
    return {case["name"]: case for case in cases}


def wait_for_metrics(mode: str, timeout: float, bridge=None) -> None:
    deadline = time.monotonic() + timeout
    detail = "No metrics received"
    while time.monotonic() < deadline:
        def query(args):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Metrics readiness deadline exceeded")
            return json.loads(inner_kubectl(args, timeout=min(30, remaining)).stdout)
        try:
            switches = query("get ncsw -o json").get("items", [])
            expected = {s["spec"].get("deviceRef", s["metadata"]["name"]) for s in switches}
            deployments = query("-n default get deployments -o json").get("items", [])
            collectors = {d["metadata"]["name"]: d for d in deployments
                          if d["metadata"]["name"].startswith("in-network-device-metrics-")}
            pods = query("-n default get pods -l app=in-network-device-metrics -o json").get("items", [])
            active_pods = [p for p in pods if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
            if mode == "ssh":
                if active_pods or any(d.get("spec", {}).get("replicas", 1) > 0 for d in collectors.values()):
                    raise RuntimeError("Cluster collectors are active; redeploy with NETCACHE_METRICS_MODE=ssh")
                bridge.poll_once(deadline=deadline)
            ready = set()
            if mode == "cluster":
                for name in expected:
                    d = collectors.get("in-network-device-metrics-" + name, {})
                    status = d.get("status", {})
                    replicas = d.get("spec", {}).get("replicas", 1)
                    if (replicas > 0 and status.get("readyReplicas", 0) >= replicas
                            and status.get("updatedReplicas", 0) >= replicas
                            and status.get("observedGeneration", 0) >= d.get("metadata", {}).get("generation", 1)):
                        ready.add(name)
            else:
                ready = expected
            devices = query("get indev -o json").get("items", [])
            now = time.time()
            fresh = {d["metadata"]["name"] for d in devices
                     if d.get("status", {}).get("metrics", {}).get("source") == mode
                     and 0 <= now - float(d.get("status", {}).get("metrics", {}).get("observedAtEpochSeconds", 0)) <= 30}
            missing = expected - (ready & fresh)
            if expected and not missing:
                return
            detail = "No NetCache switches" if not expected else "Not ready or stale: " + ", ".join(sorted(missing))
        except (subprocess.SubprocessError, ValueError, TimeoutError) as exc:
            detail = str(exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(5, remaining))
    raise TimeoutError(f"Metrics not ready after {timeout}s: {detail}")


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ewma(previous_value: float | None, sample_value: float) -> float:
    alpha = min(1.0, max(0.0, NETCACHE_RATE_EWMA_ALPHA))
    if previous_value is None:
        return sample_value
    return alpha * sample_value + (1.0 - alpha) * previous_value


def cpu_load_for_rates(cache_hit_rate: float, cache_miss_rate: float) -> float:
    if NETCACHE_CPU_BUDGET_PPS <= 0:
        return 0.0
    return min(
        1.0,
        max(
            0.0,
            (
                NETCACHE_HIT_CPU_COST * cache_hit_rate
                + NETCACHE_MISS_CPU_COST * cache_miss_rate
            ) / NETCACHE_CPU_BUDGET_PPS,
        ),
    )


def inner_kubectl(
    remote_args: str,
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    stdin_suffix = ""
    exec_stdin_flag = ""
    if input_text is not None:
        exec_stdin_flag = "-i "
        marker = "__NETCACHE_SUITE_STDIN__"
        while marker in input_text:
            marker = f"_{marker}_"
        stdin_suffix = f" <<'{marker}'\n{input_text}\n{marker}"
    remote = r"""
set -eu
NS=$(microk8s kubectl get pods -A --no-headers | awk '/kinda-control-plane/ {print $1; exit}')
if [ -z "$NS" ]; then
  echo "Could not discover Kathara namespace with kinda-control-plane" >&2
  exit 2
fi
microk8s kubectl -n "$NS" exec %skinda-control-plane -- \
  kubectl --kubeconfig /etc/kubernetes/admin.conf %s%s
""" % (exec_stdin_flag, remote_args, stdin_suffix)
    return ssh_cmd(remote, check=check, timeout=timeout)


class RunnerMetricsBridge:
    def __init__(self, poll_seconds: int = DEFAULT_METRICS_POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.states: dict[str, DeviceMetricState] = {}
        self.device_names: list[str] = []

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.run_loop, name="netcache-runner-metrics", daemon=True)
        self.thread.start()
        print("[runner-metrics] started", flush=True)

    def stop(self) -> None:
        if self.thread is None:
            return
        self.stop_event.set()
        self.thread.join()
        self.thread = None
        print("[runner-metrics] stopped", flush=True)

    def run_loop(self) -> None:
        while not self.stop_event.wait(self.poll_seconds):
            try:
                self.poll_once(deadline=time.monotonic() + max(60, 10 * len(self.device_names)))
            except Exception as exc:
                print(f"[runner-metrics] poll failed: {type(exc).__name__}: {exc}", flush=True)

    def refresh_devices(self, timeout=30) -> None:
        out = inner_kubectl("get innetworkdevices.cache.evalnet.local -o json", timeout=timeout).stdout
        data = json.loads(out)
        self.device_names = [
            item.get("metadata", {}).get("name", "")
            for item in data.get("items", [])
            if item.get("metadata", {}).get("name")
        ]
        for item in data.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            if name and name not in self.states:
                self.states[name] = self.state_from_metrics(item.get("status", {}).get("metrics", {}))

    def state_from_metrics(self, metrics: dict[str, Any]) -> DeviceMetricState:
        rate_window = metrics.get("rateWindow", {})
        baseline = None
        if rate_window:
            baseline = {
                "observedAt": as_float(rate_window.get("startedAtEpochSeconds")),
                "cacheHits": as_int(rate_window.get("baselineCacheHits")),
                "cacheMisses": as_int(rate_window.get("baselineCacheMisses")),
            }
        return DeviceMetricState(
            baseline=baseline,
            peaks={
                "cacheHitRatePps": as_float(metrics.get("maxCacheHitRatePps")),
                "cacheMissRatePps": as_float(metrics.get("maxCacheMissRatePps")),
                "estimatedCpuLoad": as_float(metrics.get("maxEstimatedCpuLoad")),
            },
            smoothed={
                "cacheHitRatePps": as_float(metrics.get("smoothedCacheHitRatePps")),
                "cacheMissRatePps": as_float(metrics.get("smoothedCacheMissRatePps")),
            },
        )

    def poll_once(self, deadline=None) -> None:
        def remaining():
            if deadline is None:
                return None
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("Metrics readiness deadline exceeded")
            return seconds
        if not self.device_names:
            self.refresh_devices(timeout=min(30, remaining() or 30))
        if not self.device_names:
            return
        samples = self.fetch_switch_metrics(self.device_names, timeout=remaining())
        patches: dict[str, dict[str, Any]] = {}
        for name in self.device_names:
            sample = samples.get(name)
            if not isinstance(sample, dict) or "error" in sample:
                if isinstance(sample, dict) and sample.get("error"):
                    print(f"[runner-metrics] {name}: {sample['error']}", flush=True)
                continue
            patches[name] = {"status": {"metrics": self.metrics_for_sample(name, sample)}}
        if patches:
            self.patch_device_statuses(patches, deadline=deadline)

    def fetch_switch_metrics(self, device_names: list[str], timeout=None) -> dict[str, Any]:
        devices_json_literal = json.dumps(json.dumps(device_names))
        remote = """
python3 - <<'PY'
import json
import subprocess
import sys

devices = json.loads(__DEVICES_JSON__)

def run(cmd, timeout=20):
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )

try:
    pods_all = run(["microk8s", "kubectl", "get", "pods", "-A", "--no-headers"])
    if pods_all.returncode != 0:
        raise RuntimeError(pods_all.stderr.strip() or pods_all.stdout.strip())
    namespace = ""
    for line in pods_all.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "kinda-control-plane":
            namespace = parts[0]
            break
    if not namespace:
        raise RuntimeError("Could not discover Kathara namespace with kinda-control-plane")

    pods_result = run(["microk8s", "kubectl", "-n", namespace, "get", "pods", "-o", "json"])
    if pods_result.returncode != 0:
        raise RuntimeError(pods_result.stderr.strip() or pods_result.stdout.strip())
    pods = json.loads(pods_result.stdout)
    pod_names = [item.get("metadata", {}).get("name", "") for item in pods.get("items", [])]
    out = {}
    for device in devices:
        prefix = f"kathara-{device}-"
        pod = next((name for name in pod_names if name.startswith(prefix)), "")
        if not pod:
            out[device] = {"error": f"pod with prefix {prefix} not found"}
            continue
        try:
            result = run([
                "microk8s", "kubectl", "-n", namespace, "exec", pod, "--",
                "sh", "-lc", "curl -fsS --max-time 5 http://127.0.0.1:18080/metrics",
            ], timeout=15)
        except subprocess.TimeoutExpired as exc:
            out[device] = {"error": f"TimeoutExpired: metrics read timed out after {exc.timeout}s"}
            continue
        if result.returncode != 0:
            out[device] = {"error": result.stderr.strip() or result.stdout.strip()}
            continue
        try:
            out[device] = json.loads(result.stdout)
        except Exception as exc:
            out[device] = {"error": f"{type(exc).__name__}: {exc}; output={result.stdout!r}"}
    print(json.dumps(out))
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(2)
PY
""".replace("__DEVICES_JSON__", devices_json_literal)
        result = ssh_cmd(
            remote,
            check=False,
            timeout=min(timeout, max(60, 8 * len(device_names))) if timeout is not None else max(60, 8 * len(device_names)),
        )
        if result.stdout.strip():
            return json.loads(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "remote metric collection failed")
        return {}

    def metrics_for_sample(self, name: str, sample: dict[str, Any]) -> dict[str, Any]:
        state = self.states.setdefault(name, DeviceMetricState())
        state.peaks = state.peaks or {}
        state.smoothed = state.smoothed or {}

        observed_at = time.time()
        packets = as_int(sample.get("packetsProcessed"))
        byte_count = as_int(sample.get("bytesProcessed"))
        cache_hits = as_int(sample.get("cacheHits"))
        cache_misses = as_int(sample.get("cacheMisses"))
        packet_rate = 0.0
        bit_rate = 0.0
        cache_hit_rate = 0.0
        cache_miss_rate = 0.0
        if state.previous:
            elapsed = max(0.0, observed_at - state.previous["observedAt"])
            if elapsed > 0:
                packet_rate = max(0.0, packets - state.previous["packetsProcessed"]) / elapsed
                bit_rate = max(0.0, byte_count - state.previous["bytesProcessed"]) * 8 / elapsed
                cache_hit_rate = max(0.0, cache_hits - state.previous["cacheHits"]) / elapsed
                cache_miss_rate = max(0.0, cache_misses - state.previous["cacheMisses"]) / elapsed

        if state.baseline is None:
            state.baseline = {
                "observedAt": observed_at,
                "cacheHits": cache_hits,
                "cacheMisses": cache_misses,
            }
        if cache_hits < state.baseline["cacheHits"] or cache_misses < state.baseline["cacheMisses"]:
            state.baseline = {
                "observedAt": observed_at,
                "cacheHits": cache_hits,
                "cacheMisses": cache_misses,
            }

        elapsed_total = max(0.0, observed_at - state.baseline["observedAt"])
        avg_cache_hit_rate = 0.0
        avg_cache_miss_rate = 0.0
        if elapsed_total > 0:
            avg_cache_hit_rate = max(0.0, cache_hits - state.baseline["cacheHits"]) / elapsed_total
            avg_cache_miss_rate = max(0.0, cache_misses - state.baseline["cacheMisses"]) / elapsed_total

        state.smoothed["cacheHitRatePps"] = ewma(state.smoothed.get("cacheHitRatePps"), cache_hit_rate)
        state.smoothed["cacheMissRatePps"] = ewma(state.smoothed.get("cacheMissRatePps"), cache_miss_rate)
        if cache_hit_rate == 0.0 and cache_miss_rate == 0.0 and (cache_hits > 0 or cache_misses > 0):
            state.smoothed["cacheHitRatePps"] = max(state.smoothed["cacheHitRatePps"], avg_cache_hit_rate)
            state.smoothed["cacheMissRatePps"] = max(state.smoothed["cacheMissRatePps"], avg_cache_miss_rate)

        smoothed_estimated_cpu_load = cpu_load_for_rates(
            state.smoothed["cacheHitRatePps"],
            state.smoothed["cacheMissRatePps"],
        )
        state.peaks["cacheHitRatePps"] = max(
            state.peaks.get("cacheHitRatePps", 0.0),
            cache_hit_rate,
            avg_cache_hit_rate,
        )
        state.peaks["cacheMissRatePps"] = max(
            state.peaks.get("cacheMissRatePps", 0.0),
            cache_miss_rate,
            avg_cache_miss_rate,
        )
        state.peaks["estimatedCpuLoad"] = max(
            state.peaks.get("estimatedCpuLoad", 0.0),
            smoothed_estimated_cpu_load,
        )

        cache_query_rate = cache_hit_rate + cache_miss_rate
        cache_miss_ratio = cache_miss_rate / cache_query_rate if cache_query_rate else 0.0
        avg_cache_query_rate = avg_cache_hit_rate + avg_cache_miss_rate
        avg_cache_miss_ratio = avg_cache_miss_rate / avg_cache_query_rate if avg_cache_query_rate else 0.0
        estimated_cpu_load = cpu_load_for_rates(cache_hit_rate, cache_miss_rate)
        avg_estimated_cpu_load = cpu_load_for_rates(avg_cache_hit_rate, avg_cache_miss_rate)

        state.previous = {
            "observedAt": observed_at,
            "packetsProcessed": packets,
            "bytesProcessed": byte_count,
            "cacheHits": cache_hits,
            "cacheMisses": cache_misses,
        }

        return {
            "observedAtEpochSeconds": observed_at,
            "source": "ssh",
            "packetsProcessed": packets,
            "bytesProcessed": byte_count,
            "packetRatePps": round(packet_rate, 3),
            "bitRateBps": round(bit_rate, 3),
            "cacheHits": cache_hits,
            "cacheMisses": cache_misses,
            "cacheHitRatePps": round(cache_hit_rate, 3),
            "cacheMissRatePps": round(cache_miss_rate, 3),
            "smoothedCacheHitRatePps": round(state.smoothed["cacheHitRatePps"], 3),
            "smoothedCacheMissRatePps": round(state.smoothed["cacheMissRatePps"], 3),
            "avgCacheHitRatePps": round(avg_cache_hit_rate, 3),
            "avgCacheMissRatePps": round(avg_cache_miss_rate, 3),
            "maxCacheHitRatePps": round(state.peaks["cacheHitRatePps"], 3),
            "maxCacheMissRatePps": round(state.peaks["cacheMissRatePps"], 3),
            "cacheHitRatio": sample.get("cacheHitRatio", 0.0),
            "cacheMissRatio": round(cache_miss_ratio, 6),
            "avgCacheMissRatio": round(avg_cache_miss_ratio, 6),
            "instantEstimatedCpuLoad": round(estimated_cpu_load, 6),
            "estimatedCpuLoad": round(smoothed_estimated_cpu_load, 6),
            "avgEstimatedCpuLoad": round(avg_estimated_cpu_load, 6),
            "maxEstimatedCpuLoad": round(state.peaks["estimatedCpuLoad"], 6),
            "cpuPressureModel": {
                "source": "runner-netcache-hit-miss-rate",
                "hitCost": NETCACHE_HIT_CPU_COST,
                "missCost": NETCACHE_MISS_CPU_COST,
                "budgetPps": NETCACHE_CPU_BUDGET_PPS,
                "rateSmoothing": "ewma",
                "ewmaAlpha": NETCACHE_RATE_EWMA_ALPHA,
            },
            "rateWindow": {
                "startedAtEpochSeconds": state.baseline["observedAt"],
                "baselineCacheHits": state.baseline["cacheHits"],
                "baselineCacheMisses": state.baseline["cacheMisses"],
            },
            "perPortPackets": sample.get("perPortPackets", {}),
            "perPortBytes": sample.get("perPortBytes", {}),
        }

    def patch_device_statuses(self, patches: dict[str, dict[str, Any]], deadline=None) -> None:
        for name, patch in patches.items():
            remaining = deadline - time.monotonic() if deadline is not None else 30
            if remaining <= 0:
                raise TimeoutError("Metrics readiness deadline exceeded")
            inner_kubectl(
                "patch innetworkdevices.cache.evalnet.local "
                f"{shlex.quote(name)} --subresource=status --type=merge "
                f"-p {shlex.quote(json.dumps(patch))}",
                check=True,
                timeout=min(30, remaining),
            )


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def to_yaml(obj: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(obj, dict):
        lines: list[str] = []
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(to_yaml(value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(value)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        lines = []
        for item in obj:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(to_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{yaml_scalar(obj)}"


def multi_doc_yaml(items: list[dict[str, Any]]) -> str:
    return "\n---\n".join(to_yaml(item) for item in items) + "\n"


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        return [items]
    return [items[index : index + size] for index in range(0, len(items), size)]


def apply_requests_batched(requests: list[dict[str, Any]], batch_size: int, batch_delay: int) -> None:
    batches = chunks(requests, batch_size)
    for index, batch in enumerate(batches, start=1):
        print(f"Applying request batch {index}/{len(batches)} ({len(batch)} requests)", flush=True)
        inner_kubectl("-n default apply -f -", input_text=multi_doc_yaml(batch))
        if index < len(batches) and batch_delay > 0:
            time.sleep(batch_delay)


def wait_for_requests(run_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    selector = f"experiment.evalnet.local/run={run_id}"
    last: dict[str, Any] = {"items": []}
    while time.monotonic() < deadline:
        out = inner_kubectl(f"-n default get ncreq -l {selector} -o json", timeout=max(0.1, min(30, deadline-time.monotonic()))).stdout
        last = json.loads(out)
        items = last.get("items", [])
        phases = [item.get("status", {}).get("phase", "") for item in items]
        if items and all(phase in TERMINAL_PHASES for phase in phases):
            return last
        print(f"Waiting for {run_id}: {len(items)} requests, phases={sorted(set(phases))}", flush=True)
        time.sleep(max(0, min(10, deadline-time.monotonic())))
    return last


def collect_resource(args: str, path: Path) -> None:
    result = inner_kubectl(args, check=False)
    path.write_text(result.stdout)
    if result.returncode != 0:
        (path.with_suffix(path.suffix + ".stderr")).write_text(result.stderr)


def safe_file_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def diagnostic_kubectl(remote_args: str, timeout: int = 30) -> str:
    try:
        result = inner_kubectl(remote_args, check=False, timeout=timeout)
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s while running: kubectl {remote_args}\n"


def collect_pod_diagnostics(run_id: str, out_dir: Path) -> None:
    pods_path = out_dir / "pods.json"
    requests_path = out_dir / "netcacheclientrequests.json"
    if not pods_path.exists() or not requests_path.exists():
        return
    pods = json.loads(pods_path.read_text() or '{"items":[]}')
    requests = json.loads(requests_path.read_text() or '{"items":[]}')
    request_names = {
        item.get("metadata", {}).get("name", "")
        for item in requests.get("items", [])
        if item.get("metadata", {}).get("labels", {}).get("experiment.evalnet.local/run") == run_id
    }
    diag_dir = out_dir / "pod-diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    for pod in pods.get("items", []):
        meta = pod.get("metadata", {})
        labels = meta.get("labels", {})
        name = meta.get("name", "")
        if not name or labels.get("cache.evalnet.local/request") not in request_names:
            continue
        safe_name = safe_file_name(name)
        describe = diagnostic_kubectl(f"-n default describe pod {shlex.quote(name)}")
        (diag_dir / f"{safe_name}.describe.txt").write_text(describe)
        for container in pod.get("spec", {}).get("containers", []):
            container_name = container.get("name", "")
            if not container_name:
                continue
            safe_container = safe_file_name(container_name)
            logs = diagnostic_kubectl(
                f"-n default logs {shlex.quote(name)} -c {shlex.quote(container_name)} --tail=200"
            )
            (diag_dir / f"{safe_name}.{safe_container}.log").write_text(logs)
            previous_logs = diagnostic_kubectl(
                f"-n default logs {shlex.quote(name)} -c {shlex.quote(container_name)} --previous --tail=200"
            )
            if previous_logs:
                (diag_dir / f"{safe_name}.{safe_container}.previous.log").write_text(previous_logs)


def collect_results(case: dict[str, Any], run_id: str, out_dir: Path, request_json: dict[str, Any] | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "case.json").write_text(json.dumps(case, indent=2) + "\n")
    if request_json is not None:
        (out_dir / "netcacheclientrequests.json").write_text(json.dumps(request_json, indent=2) + "\n")
    else:
        collect_resource(f"-n default get ncreq -l experiment.evalnet.local/run={run_id} -o json", out_dir / "netcacheclientrequests.json")
    collect_resource("get ncsw -o json", out_dir / "netcacheswitches.json")
    collect_resource("get indev -o json", out_dir / "innetworkdevices.json")
    collect_resource("get ncreach -o json", out_dir / "netcachereachabilities.json")
    collect_resource("-n default get pods -o json", out_dir / "pods.json")
    collect_resource("-n default get services -o json", out_dir / "services.json")
    if os.environ.get("NETCACHE_COLLECT_POD_DIAGNOSTICS") == "1":
        collect_pod_diagnostics(run_id, out_dir)
    summarize_run(out_dir)


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[index]


def summarize_run(out_dir: Path) -> dict[str, Any]:
    requests = json.loads((out_dir / "netcacheclientrequests.json").read_text())
    switches = json.loads((out_dir / "netcacheswitches.json").read_text()) if (out_dir / "netcacheswitches.json").exists() else {"items": []}
    devices = json.loads((out_dir / "innetworkdevices.json").read_text()) if (out_dir / "innetworkdevices.json").exists() else {"items": []}
    pods = json.loads((out_dir / "pods.json").read_text()) if (out_dir / "pods.json").exists() else {"items": []}
    request_names = {item.get("metadata", {}).get("name", "") for item in requests.get("items", [])}
    pod_by_request = {}
    for pod in pods.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        request_name = labels.get("cache.evalnet.local/request")
        if request_name in request_names:
            pod_by_request.setdefault(request_name, []).append(pod)
    rows: list[dict[str, Any]] = []
    path_costs: list[float] = []
    hop_counts: list[float] = []
    placement_latencies: list[float] = []

    for item in requests.get("items", []):
        meta = item.get("metadata", {})
        status = item.get("status", {})
        created = parse_ts(meta.get("creationTimestamp"))
        finished = None
        for pod in pod_by_request.get(meta.get("name", ""), []):
            for container_status in pod.get("status", {}).get("containerStatuses", []):
                terminated = container_status.get("state", {}).get("terminated", {})
                finished = parse_ts(terminated.get("finishedAt")) or finished
        row = {
            "name": meta.get("name", ""),
            "phase": status.get("phase", ""),
            "cacheMode": status.get("cacheMode", ""),
            "switchName": status.get("switchName", ""),
            "selectedWorker": status.get("selectedWorker") or status.get("attachedWorker", ""),
            "requestedEntries": status.get("requestedEntries", ""),
            "allocatedEntries": status.get("allocatedEntries", ""),
            "allocatedSlots": status.get("allocatedSlots", ""),
            "selectedHopCount": status.get("selectedHopCount", ""),
            "selectedPathCost": status.get("selectedPathCost", ""),
            "selectedBackendServerIp": status.get("selectedBackendServerIp", ""),
            "selectedBackendServerName": status.get("selectedBackendServerName", ""),
            "fallbackToRedisRequests": status.get("fallbackToRedisRequests", 0),
            "reason": status.get("reason", ""),
        }
        rows.append(row)
        if row["cacheMode"] == "netcache" and row["phase"] == "Succeeded":
            if row["selectedPathCost"] != "":
                path_costs.append(float(row["selectedPathCost"]))
            if row["selectedHopCount"] != "":
                hop_counts.append(float(row["selectedHopCount"]))
        if created and finished:
            placement_latencies.append(max(0.0, finished - created))

    csv_path = out_dir / "requests.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["name"])
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    succeeded = sum(1 for row in rows if row["phase"] == "Succeeded")
    netcache = sum(1 for row in rows if row["phase"] == "Succeeded" and row["cacheMode"] == "netcache")
    redis = sum(1 for row in rows if row["phase"] == "Succeeded" and row["cacheMode"] == "redis")
    failed = sum(1 for row in rows if row["phase"] == "Failed")
    switch_rows = []
    for item in switches.get("items", []):
        spec = item.get("spec", {})
        status = item.get("status", {})
        switch_rows.append(
            {
                "name": item.get("metadata", {}).get("name", ""),
                "switchName": spec.get("switchName", ""),
                "usedEntries": status.get("usedEntries", 0),
                "freeEntries": status.get("freeEntries", spec.get("capacityEntries", 0)),
                "usedSlots": status.get("usedSlots", 0),
                "freeSlots": status.get("freeSlots", spec.get("capacitySlots", 0)),
                "servedByNetcacheRequests": status.get("servedByNetcacheRequests", 0),
                "p4Coverage": status.get("p4Coverage", 0),
            }
        )

    device_rows = []
    for item in devices.get("items", []):
        metrics = item.get("status", {}).get("metrics", {})
        device_rows.append(
            {
                "name": item.get("metadata", {}).get("name", ""),
                "packetsProcessed": int(metrics.get("packetsProcessed", 0) or 0),
                "bytesProcessed": int(metrics.get("bytesProcessed", 0) or 0),
                "packetRatePps": float(metrics.get("packetRatePps", 0) or 0),
                "bitRateBps": float(metrics.get("bitRateBps", 0) or 0),
                "cacheHits": int(metrics.get("cacheHits", 0) or 0),
                "cacheMisses": int(metrics.get("cacheMisses", 0) or 0),
                "cacheHitRatePps": float(metrics.get("cacheHitRatePps", 0) or 0),
                "cacheMissRatePps": float(metrics.get("cacheMissRatePps", 0) or 0),
                "smoothedCacheHitRatePps": float(metrics.get("smoothedCacheHitRatePps", metrics.get("cacheHitRatePps", 0)) or 0),
                "smoothedCacheMissRatePps": float(metrics.get("smoothedCacheMissRatePps", metrics.get("cacheMissRatePps", 0)) or 0),
                "avgCacheHitRatePps": float(metrics.get("avgCacheHitRatePps", 0) or 0),
                "avgCacheMissRatePps": float(metrics.get("avgCacheMissRatePps", 0) or 0),
                "maxCacheHitRatePps": float(metrics.get("maxCacheHitRatePps", metrics.get("cacheHitRatePps", 0)) or 0),
                "maxCacheMissRatePps": float(metrics.get("maxCacheMissRatePps", metrics.get("cacheMissRatePps", 0)) or 0),
                "cacheMissRatio": float(metrics.get("cacheMissRatio", 0) or 0),
                "instantEstimatedCpuLoad": float(metrics.get("instantEstimatedCpuLoad", metrics.get("estimatedCpuLoad", 0)) or 0),
                "estimatedCpuLoad": float(metrics.get("estimatedCpuLoad", 0) or 0),
                "avgEstimatedCpuLoad": float(metrics.get("avgEstimatedCpuLoad", 0) or 0),
                "maxEstimatedCpuLoad": float(metrics.get("maxEstimatedCpuLoad", metrics.get("estimatedCpuLoad", 0)) or 0),
            }
        )

    summary = {
        "totalRequests": total,
        "succeeded": succeeded,
        "failed": failed,
        "netcacheSucceeded": netcache,
        "redisSucceeded": redis,
        "netcacheSuccessRatio": netcache / total if total else 0,
        "redisSuccessRatio": redis / total if total else 0,
        "avgPathCost": statistics.mean(path_costs) if path_costs else None,
        "p95PathCost": percentile(path_costs, 95),
        "avgHopCount": statistics.mean(hop_counts) if hop_counts else None,
        "p95HopCount": percentile(hop_counts, 95),
        "avgRequestLatencySeconds": statistics.mean(placement_latencies) if placement_latencies else None,
        "p95RequestLatencySeconds": percentile(placement_latencies, 95),
        "totalPacketsProcessed": sum(row["packetsProcessed"] for row in device_rows),
        "totalBytesProcessed": sum(row["bytesProcessed"] for row in device_rows),
        "maxPacketRatePps": max((row["packetRatePps"] for row in device_rows), default=0),
        "maxBitRateBps": max((row["bitRateBps"] for row in device_rows), default=0),
        "totalCacheHits": sum(row["cacheHits"] for row in device_rows),
        "totalCacheMisses": sum(row["cacheMisses"] for row in device_rows),
        "maxCacheHitRatePps": max((row["maxCacheHitRatePps"] for row in device_rows), default=0),
        "maxCacheMissRatePps": max((row["maxCacheMissRatePps"] for row in device_rows), default=0),
        "avgCacheHitRatePps": sum((row["avgCacheHitRatePps"] for row in device_rows), 0.0) / len(device_rows) if device_rows else 0,
        "avgCacheMissRatePps": sum((row["avgCacheMissRatePps"] for row in device_rows), 0.0) / len(device_rows) if device_rows else 0,
        "maxEstimatedCpuLoad": max((row["maxEstimatedCpuLoad"] for row in device_rows), default=0),
        "avgEstimatedCpuLoad": sum((row["avgEstimatedCpuLoad"] for row in device_rows), 0.0) / len(device_rows) if device_rows else 0,
        "switches": switch_rows,
        "devices": device_rows,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def validate_run(requests, expected_names):
    items = requests.get("items", [])
    actual = {item["metadata"]["name"] for item in items}
    bad = {item["metadata"]["name"]: item.get("status", {}).get("phase", "Pending")
           for item in items if item.get("status", {}).get("phase") != "Succeeded"}
    if actual != expected_names or bad:
        raise RuntimeError(f"Experiment incomplete or failed: missing={sorted(expected_names-actual)}, "
                           f"unexpected={sorted(actual-expected_names)}, phases={bad}")


def selected_case(args):
    cases = load_cases(Path(args.cases))
    if args.case not in cases:
        raise ValueError(f"Unknown case {args.case}")
    return cases[args.case]


def run_case(args: argparse.Namespace) -> None:
    case = selected_case(args)
    mode = metrics_mode()
    if not args.skip_deploy:
        deploy_case(case)
    run_id = args.run_id or f"exp-{time.time_ns()}"
    print(f"Run id: {run_id}")
    requests = request_specs(case, run_id)
    expected_names = {r["metadata"]["name"] for r in requests}
    out_dir = Path(args.results or Path(args.cases).parent / "results") / case["name"] / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    bridge = RunnerMetricsBridge() if mode == "ssh" else None
    request_json = None
    failure = None
    try:
        wait_for_metrics(mode, args.metrics_ready_timeout, bridge)
        if bridge:
            bridge.start()
        apply_requests_batched(requests, args.request_batch_size, args.request_batch_delay)
        request_json = wait_for_requests(run_id, args.timeout)
    except Exception as exc:
        failure = exc
    finally:
        if bridge:
            bridge.stop()
        try:
            collect_results(case, run_id, out_dir, request_json=request_json)
        except Exception as exc:
            (out_dir / "collection-error.txt").write_text(str(exc) + "\n")
            if failure is None:
                failure = exc
    print(f"Wrote results to {out_dir}")
    if failure is not None:
        raise failure
    print((out_dir / "summary.json").read_text())
    validate_run(request_json, expected_names)


def collect_existing(args):
    case = selected_case(args)
    out_dir = Path(args.results or Path(args.cases).parent / "results") / case["name"] / args.run_id
    collect_results(case, args.run_id, out_dir)
    print(f"Wrote results to {out_dir}")


def run_suite(args: argparse.Namespace) -> None:
    cases = load_cases(Path(args.cases))
    selected = args.case or sorted(cases)
    if args.limit:
        selected = selected[: args.limit]
    results_root = Path(args.results or Path(args.cases).parent / "results")
    failures: list[tuple[str, str]] = []
    for index, case_name in enumerate(selected, start=1):
        if case_name not in cases:
            raise SystemExit(f"Unknown case {case_name}. Available: {', '.join(sorted(cases))}")
        if args.resume and case_has_successful_run(results_root, case_name):
            print(f"[{index}/{len(selected)}] Skipping completed {case_name}", flush=True)
            continue
        print(f"[{index}/{len(selected)}] Running {case_name}", flush=True)
        child = argparse.Namespace(
            cases=args.cases,
            case=case_name,
            results=args.results,
            run_id=None,
            timeout=args.timeout,
            skip_deploy=args.skip_deploy,
            metrics_ready_timeout=args.metrics_ready_timeout,
            request_batch_size=args.request_batch_size,
            request_batch_delay=args.request_batch_delay,
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                if attempt > 1:
                    print(f"[{index}/{len(selected)}] Retrying {case_name}, attempt {attempt}/{args.retries + 1}", flush=True)
                run_case(child)
                break
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                print(f"[{index}/{len(selected)}] Case {case_name} failed on attempt {attempt}: {message}", flush=True)
                if attempt <= args.retries:
                    if args.retry_delay > 0:
                        print(f"Waiting {args.retry_delay}s before retrying {case_name}", flush=True)
                        time.sleep(args.retry_delay)
                    continue
                failures.append((case_name, message))
                if not args.keep_going:
                    raise
                break
        if index < len(selected) and args.settle_between > 0:
            print(f"Settling for {args.settle_between}s before the next case", flush=True)
            time.sleep(args.settle_between)
    if failures:
        failed_path = results_root / "failed-cases.json"
        failed_path.parent.mkdir(parents=True, exist_ok=True)
        failed_path.write_text(json.dumps(
            [{"case": case_name, "error": error} for case_name, error in failures],
            indent=2,
        ) + "\n")
        print(f"Wrote {failed_path}", flush=True)
        raise SystemExit(f"{len(failures)} case(s) failed after retries")


def case_has_successful_run(results_root: Path | None, case_name: str) -> bool:
    if results_root is None:
        return False
    case_dir = results_root / case_name
    if not case_dir.exists():
        return False
    for summary_path in sorted(case_dir.glob("*/summary.json"), reverse=True):
        try:
            summary = json.loads(summary_path.read_text())
        except Exception:
            continue
        total = int(summary.get("totalRequests", 0) or 0)
        succeeded = int(summary.get("succeeded", 0) or 0)
        failed = int(summary.get("failed", 0) or 0)
        if total > 0 and succeeded == total and failed == 0:
            return True
    return False


def deploy_case(case):
    raise RuntimeError("Use netcache_experiment_matrix.py to configure the runner")

def request_specs(case, run_id):
    raise RuntimeError("Use netcache_experiment_matrix.py to configure the workloads")
