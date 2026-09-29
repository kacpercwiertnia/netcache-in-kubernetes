#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import experiment_runner as base


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_ROOT = ROOT / ".generated" / "experiments"
RUN_TOPOLOGY = ROOT / "scripts" / "run_topology.sh"

PROFILES = ["small", "medium", "large"]
COVERAGE_LEVELS = ["one", "p10", "p33", "p100"]
WORKLOADS = ["zipf-a099-read", "zipf-a099-read-miss20", "dynamic-random-miss20"]
MISS_WORKLOAD_SUFFIX = "-miss20"
MISS_WORKLOAD_RATIO = 0.20
REDIS_FALLBACK_SMOKE_WORKLOAD = "redis-fallback-pressure"
REDIS_FALLBACK_SMOKE_KEYS_PER_REQUEST = 128
REDIS_FALLBACK_SMOKE_REQUEST_COUNT = 24
REDIS_FALLBACK_SMOKE_SWITCH_CAPACITY = 512


def fattree_nodes(k: int) -> int:
    return 5 * (k // 2) * (k // 2)


def slimfly_nodes(q: int) -> int:
    return 2 * q * q


def dragonfly_nodes(p: int) -> int:
    return 4 * p**3 + 2 * p


def topology_specs() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "fattree": {
            "small": {"args": ["fattree", "4"], "kind": "fattree", "accessPolicy": "auto", "nodes": fattree_nodes(4)},
            "medium": {"args": ["fattree", "6"], "kind": "fattree", "accessPolicy": "auto", "nodes": fattree_nodes(6)},
            "large": {"args": ["fattree", "8"], "kind": "fattree", "accessPolicy": "auto", "nodes": fattree_nodes(8)},
        },
        "spine-leaf": {
            "small": {"args": ["xgft", "1", "8", "4"], "kind": "spine-leaf", "accessPolicy": "auto", "nodes": 12},
            "medium": {"args": ["xgft", "1", "24", "8"], "kind": "spine-leaf", "accessPolicy": "auto", "nodes": 32},
            "large": {"args": ["xgft", "1", "64", "16"], "kind": "spine-leaf", "accessPolicy": "auto", "nodes": 80},
        },
        "slimfly": {
            "small": {"args": ["slimfly", "3"], "kind": "generic", "accessPolicy": "explicit", "nodes": slimfly_nodes(3)},
            "medium": {"args": ["slimfly", "5"], "kind": "generic", "accessPolicy": "explicit", "nodes": slimfly_nodes(5)},
            "large": {"args": ["slimfly", "7"], "kind": "generic", "accessPolicy": "explicit", "nodes": slimfly_nodes(7)},
        },
        "dragonfly": {
            "small": {"args": ["dragonfly", "1"], "kind": "generic", "accessPolicy": "explicit", "nodes": dragonfly_nodes(1)},
            "medium": {"args": ["dragonfly", "2"], "kind": "generic", "accessPolicy": "explicit", "nodes": dragonfly_nodes(2)},
            "large": {"args": ["dragonfly", "3"], "kind": "generic", "accessPolicy": "explicit", "nodes": dragonfly_nodes(3)},
        },
        "jellyfish": {
            "small": {"args": ["jellyfish", "4", "18"], "kind": "generic", "accessPolicy": "explicit", "nodes": 18},
            "medium": {"args": ["jellyfish", "6", "48"], "kind": "generic", "accessPolicy": "explicit", "nodes": 48},
            "large": {"args": ["jellyfish", "8", "96"], "kind": "generic", "accessPolicy": "explicit", "nodes": 96},
        },
    }


def access_nodes_for(spec: dict[str, Any]) -> list[int]:
    n = int(spec["nodes"])
    if spec["accessPolicy"] != "explicit":
        return []
    count = min(n, max(4, min(32, math.ceil(n / 2))))
    return list(range(count))


def coverage_count(total: int, level: str) -> int:
    if level == "one":
        return 1
    if level == "p10":
        return max(1, math.ceil(total * 0.10))
    if level == "p33":
        return max(1, math.ceil(total * 0.33))
    if level == "p100":
        return total
    raise ValueError(f"unknown coverage level: {level}")


def random_nodes(total: int, count: int, seed: int) -> list[int]:
    rng = random.Random(seed)
    return sorted(rng.sample(range(total), min(total, count)))


def parse_csv_filter(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def case_name(topology: str, size: str, coverage: str, workload: str, seed: int) -> str:
    return f"{topology}-{size}-{coverage}-{workload}-seed{seed}".replace("_", "-")


def default_request_count(workload: str, size: str) -> int:
    base_count = {
        "small": 24,
        "medium": 24,
        "large": 64,
    }.get(size, 64)
    if workload.startswith("dynamic"):
        return max(24, base_count)
    if "rw20" in workload:
        return base_count
    return base_count


def plan_request_count(args: argparse.Namespace, workload: str, size: str) -> int:
    if args.request_count is not None:
        return args.request_count
    return default_request_count(workload, size)


def build_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    specs = topology_specs()
    topologies = parse_csv_filter(args.topologies, list(specs))
    sizes = parse_csv_filter(args.sizes, PROFILES)
    coverages = parse_csv_filter(args.coverages, COVERAGE_LEVELS)
    workloads = parse_csv_filter(args.workloads, WORKLOADS)
    seeds = [int(seed) for seed in parse_csv_filter(args.seeds, ["1", "2", "3"])]
    cases: list[dict[str, Any]] = []
    for topology in topologies:
        if topology not in specs:
            raise SystemExit(f"unknown topology {topology}; available: {', '.join(sorted(specs))}")
        for size in sizes:
            if size not in specs[topology]:
                raise SystemExit(f"unknown size {size} for {topology}")
            spec = specs[topology][size]
            total = int(spec["nodes"])
            access_nodes = access_nodes_for(spec)
            for coverage in coverages:
                count = coverage_count(total, coverage)
                for seed in seeds:
                    placement_seed = args.random_seed + seed * 1009 + total * 17
                    nodes = random_nodes(total, count, placement_seed)
                    actual = round(100.0 * len(nodes) / total, 2)
                    for workload in workloads:
                        name = case_name(topology, size, coverage, workload, seed)
                        lab_name = f"{name}-{int(actual * 100):04d}"
                        cases.append(
                            {
                                "name": name,
                                "topology": topology,
                                "size": size,
                                "coverage": coverage,
                                "requestedPercent": coverage,
                                "actualPercent": actual,
                                "randomSeed": seed,
                                "placement": "random",
                                "backendMode": "selected-switch-local",
                                "workload": workload,
                                "requestCount": plan_request_count(args, workload, size),
                                "topologyArgs": spec["args"],
                                "topologyKind": spec["kind"],
                                "accessPolicy": spec["accessPolicy"],
                                "accessNodes": access_nodes,
                                "nodeCount": total,
                                "netcacheNodes": nodes,
                                "labName": lab_name,
                            }
                        )
    if args.include_redis_fallback_smoke:
        specs = topology_specs()
        topology = "spine-leaf"
        size = "small"
        coverage = "p33"
        seed = 1
        spec = specs[topology][size]
        total = int(spec["nodes"])
        access_nodes = access_nodes_for(spec)
        count = coverage_count(total, coverage)
        placement_seed = args.random_seed + seed * 1009 + total * 17
        nodes = random_nodes(total, count, placement_seed)
        actual = round(100.0 * len(nodes) / total, 2)
        name = case_name(topology, size, coverage, REDIS_FALLBACK_SMOKE_WORKLOAD, seed)
        cases.append(
            {
                "name": name,
                "topology": topology,
                "size": size,
                "coverage": coverage,
                "requestedPercent": coverage,
                "actualPercent": actual,
                "randomSeed": seed,
                "placement": "random",
                "backendMode": "redis-fallback-smoke",
                "workload": REDIS_FALLBACK_SMOKE_WORKLOAD,
                "requestCount": REDIS_FALLBACK_SMOKE_REQUEST_COUNT,
                "topologyArgs": spec["args"],
                "topologyKind": spec["kind"],
                "accessPolicy": spec["accessPolicy"],
                "accessNodes": access_nodes,
                "nodeCount": total,
                "netcacheNodes": nodes,
                "labName": f"{name}-{int(actual * 100):04d}",
                "keysPerRequest": REDIS_FALLBACK_SMOKE_KEYS_PER_REQUEST,
                "uniqueKeysPerRequest": REDIS_FALLBACK_SMOKE_KEYS_PER_REQUEST,
                "iterations": 12,
                "operation": "put-read",
                "netcacheSwitchCapacityEntries": REDIS_FALLBACK_SMOKE_SWITCH_CAPACITY,
                "netcacheSwitchCapacitySlots": REDIS_FALLBACK_SMOKE_SWITCH_CAPACITY * 8,
                "expectedCacheMode": "mixed-netcache-then-redis",
                "purpose": "Smoke-test Redis fallback after a standard-sized batch exhausts reduced NetCache capacity.",
            }
        )
    return cases


def write_plan(args: argparse.Namespace) -> None:
    out_dir = Path(args.output or DEFAULT_RESULTS_ROOT / "plan")
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = build_cases(args)
    (out_dir / "cases.json").write_text(json.dumps({"cases": cases}, indent=2) + "\n")
    print(f"Wrote {out_dir / 'cases.json'}")


def load_cases(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text())
    cases = raw["cases"] if isinstance(raw, dict) else raw
    return {case["name"]: case for case in cases}


def deploy_case(case: dict[str, Any], extra_env: dict[str, str] | None = None) -> None:
    env = os.environ.copy()
    env.update(extra_env or {})
    env.setdefault("ANSIBLE_HOST_KEY_CHECKING", "False")
    env.setdefault("ANSIBLE_LOCAL_TEMP", str(ROOT / ".generated" / "ansible-tmp"))
    env.setdefault("ANSIBLE_REMOTE_TEMP", "/tmp")
    if case.get("netcacheSwitchCapacityEntries"):
        env["KINDA_NETCACHE_SWITCH_CAPACITY_ENTRIES"] = str(case["netcacheSwitchCapacityEntries"])
    if case.get("netcacheSwitchCapacitySlots"):
        env["KINDA_NETCACHE_SWITCH_CAPACITY_SLOTS"] = str(case["netcacheSwitchCapacitySlots"])

    topology_args = [str(part) for part in case["topologyArgs"]]
    evalnet_topology = topology_args[0]
    evalnet_args = topology_args[1:]
    cmd = [
        str(RUN_TOPOLOGY),
        evalnet_topology,
        case["labName"],
        "--topology-kind",
        case["topologyKind"],
        "--access-policy",
        case["accessPolicy"],
        "--netcache-nodes",
        ",".join(str(node) for node in case["netcacheNodes"]),
    ]
    if case.get("accessNodes"):
        cmd.extend(["--access-nodes", ",".join(str(node) for node in case["accessNodes"])])
    cmd.append("--")
    cmd.extend(evalnet_args)
    print("+ " + " ".join(shlex.quote(part) for part in cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), env=env, check=True)


def zipf_keys(rng: random.Random, keyspace: int, alpha: float, count: int, prefix: str) -> list[str]:
    ranks = list(range(1, keyspace + 1))
    weights = [1.0 / (rank**alpha) for rank in ranks]
    selected = rng.choices(ranks, weights=weights, k=count)
    return [f"{prefix}{rank:05d}" for rank in selected]


def unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def workload_params(workload: str) -> dict[str, Any]:
    if workload.endswith(MISS_WORKLOAD_SUFFIX):
        params = workload_params(workload.removesuffix(MISS_WORKLOAD_SUFFIX))
        params["cacheMissRatio"] = MISS_WORKLOAD_RATIO
        return params
    if workload == "zipf-a090-read":
        return {"alpha": 0.90, "writeRatio": 0.0, "writeSkew": "none", "dynamic": None}
    if workload == "zipf-a095-read":
        return {"alpha": 0.95, "writeRatio": 0.0, "writeSkew": "none", "dynamic": None}
    if workload == "zipf-a099-read":
        return {"alpha": 0.99, "writeRatio": 0.0, "writeSkew": "none", "dynamic": None}
    if workload == "zipf-a099-rw10-uniform":
        return {"alpha": 0.99, "writeRatio": 0.10, "writeSkew": "uniform", "dynamic": None}
    if workload == "zipf-a099-rw20-skewed":
        return {"alpha": 0.99, "writeRatio": 0.20, "writeSkew": "skewed", "dynamic": None}
    if workload == "dynamic-hot-in":
        return {"alpha": 0.99, "writeRatio": 0.0, "writeSkew": "none", "dynamic": "hot-in"}
    if workload == "dynamic-random":
        return {"alpha": 0.99, "writeRatio": 0.0, "writeSkew": "none", "dynamic": "random"}
    if workload == "dynamic-hot-out":
        return {"alpha": 0.99, "writeRatio": 0.0, "writeSkew": "none", "dynamic": "hot-out"}
    if workload == REDIS_FALLBACK_SMOKE_WORKLOAD:
        return {"alpha": 0.99, "writeRatio": 1.0, "writeSkew": "none", "dynamic": None}
    raise ValueError(f"unknown workload: {workload}")


def dynamic_prefix(dynamic: str | None, phase: int) -> str:
    if dynamic == "hot-in":
        return "c" if phase > 0 else "b"
    if dynamic == "random":
        return f"r{phase}"
    if dynamic == "hot-out":
        return "s" if phase > 0 else "b"
    return "b"


def request_specs(case: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    workload = case["workload"]
    params = workload_params(workload)
    count = int(case.get("requestCount", 24))
    size = case.get("size", "small")
    keyspace = int(case.get("keyspace", 4096 if size == "small" else 8192 if size == "medium" else 16384))
    keys_per_request = int(case.get("keysPerRequest", 96 if size == "small" else 128 if size == "medium" else 192))
    iterations = int(case.get("iterations", 12 if size == "small" else 16 if size == "medium" else 20))
    rng = random.Random(int(case.get("randomSeed", 1)) * 7919 + len(workload))
    specs: list[dict[str, Any]] = []
    phases = 3 if params["dynamic"] else 1
    for index in range(count):
        phase = min(phases - 1, index * phases // max(1, count))
        name = f"{run_id}-r{index:03d}"
        is_write = rng.random() < float(params["writeRatio"])
        unique_count = int(case.get("uniqueKeysPerRequest", 0))
        if unique_count > 0:
            keys = [f"f{index:03d}{key_index:04d}" for key_index in range(unique_count)]
        elif is_write and params["writeSkew"] == "uniform":
            keys = [f"w{rng.randint(1, keyspace):05d}" for _ in range(max(8, keys_per_request // 4))]
        else:
            prefix = dynamic_prefix(params["dynamic"], phase)
            keys = zipf_keys(rng, keyspace, float(params["alpha"]), keys_per_request, prefix)
        unique_keys = unique_preserve(keys)
        cache_miss_ratio = float(case.get("cacheMissRatio", params.get("cacheMissRatio", 0.0)))
        cache_miss_ratio = max(0.0, min(1.0, cache_miss_ratio))
        cached_entry_count = int(len(unique_keys) * (1.0 - cache_miss_ratio))
        requested_entries = int(case.get("forceRequestedEntries", max(1, cached_entry_count)))
        operation = str(case.get("operation", "put-read" if is_write else "read"))
        labels = {
            "experiment.evalnet.local/run": run_id,
            "experiment.evalnet.local/case": case["name"],
            "experiment.evalnet.local/workload": workload,
            "experiment.evalnet.local/topology": case["topology"],
            "experiment.evalnet.local/size": case["size"],
            "experiment.evalnet.local/coverage": case["coverage"],
            "experiment.evalnet.local/random-seed": str(case["randomSeed"]),
        }
        if params["dynamic"]:
            labels["experiment.evalnet.local/phase"] = str(phase)
            labels["experiment.evalnet.local/dynamic"] = params["dynamic"]
        spec = {
            "operation": operation,
            "keys": ",".join(unique_keys),
            "requestedEntries": requested_entries,
            "iterations": iterations,
        }
        if "prepopulateCache" in case:
            spec["prepopulateCache"] = bool(case["prepopulateCache"])
        if cache_miss_ratio > 0.0:
            spec["cacheMissRatio"] = cache_miss_ratio
        if "sleepSeconds" in case:
            spec["sleepSeconds"] = case["sleepSeconds"]
        specs.append(
            {
                "apiVersion": "cache.evalnet.local/v1alpha1",
                "kind": "NetCacheClientRequest",
                "metadata": {"name": name, "labels": labels},
                "spec": spec,
            }
        )
    return specs


def run_case(args: argparse.Namespace) -> None:
    base.deploy_case = deploy_case
    base.request_specs = request_specs
    base.run_case(args)


def run_suite(args: argparse.Namespace) -> None:
    base.deploy_case = deploy_case
    base.request_specs = request_specs
    base.run_suite(args)


def summarize_all(args: argparse.Namespace) -> None:
    root = Path(args.results)
    root.mkdir(parents=True, exist_ok=True)
    summaries = sorted(root.glob("*/**/summary.json"))
    rows = []
    for path in summaries:
        summary = json.loads(path.read_text())
        case_path = path.parent / "case.json"
        case = json.loads(case_path.read_text()) if case_path.exists() else {}
        rows.append(
            {
                "case": case.get("name", path.parent.parent.name),
                "run": path.parent.name,
                "topology": case.get("topology", ""),
                "size": case.get("size", ""),
                "nodeCount": case.get("nodeCount", ""),
                "coverage": case.get("coverage", ""),
                "actualPercent": case.get("actualPercent", ""),
                "randomSeed": case.get("randomSeed", ""),
                "workload": case.get("workload", ""),
                "totalRequests": summary.get("totalRequests", 0),
                "succeeded": summary.get("succeeded", 0),
                "failed": summary.get("failed", 0),
                "satisfiedIncJobs": summary.get("netcacheSucceeded", 0),
                "satisfiedIncRatio": summary.get("netcacheSuccessRatio", 0),
                "redisSucceeded": summary.get("redisSucceeded", 0),
                "avgSwitchDetour": summary.get("avgPathCost", ""),
                "p95SwitchDetour": summary.get("p95PathCost", ""),
                "avgHopCount": summary.get("avgHopCount", ""),
                "p95HopCount": summary.get("p95HopCount", ""),
                "avgPlacementLatencySeconds": summary.get("avgRequestLatencySeconds", ""),
                "p95PlacementLatencySeconds": summary.get("p95RequestLatencySeconds", ""),
                "totalPacketsProcessed": summary.get("totalPacketsProcessed", ""),
                "totalBytesProcessed": summary.get("totalBytesProcessed", ""),
                "maxPacketRatePps": summary.get("maxPacketRatePps", ""),
                "maxBitRateBps": summary.get("maxBitRateBps", ""),
                "totalCacheHits": summary.get("totalCacheHits", ""),
                "totalCacheMisses": summary.get("totalCacheMisses", ""),
                "maxCacheHitRatePps": summary.get("maxCacheHitRatePps", ""),
                "maxCacheMissRatePps": summary.get("maxCacheMissRatePps", ""),
                "avgCacheHitRatePps": summary.get("avgCacheHitRatePps", ""),
                "avgCacheMissRatePps": summary.get("avgCacheMissRatePps", ""),
                "maxEstimatedCpuLoad": summary.get("maxEstimatedCpuLoad", ""),
                "avgEstimatedCpuLoad": summary.get("avgEstimatedCpuLoad", ""),
            }
        )
    out_csv = root / "summary.csv"
    with out_csv.open("w", newline="") as f:
        fieldnames = list(rows[0].keys()) if rows else ["case"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_csv}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan and run NetCache NetCache scheduling experiments.")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Generate cases.json.")
    plan.add_argument("--topologies", help="Comma-separated topologies. Default: all.")
    plan.add_argument("--sizes", help="Comma-separated sizes: small,medium,large.")
    plan.add_argument("--coverages", help="Comma-separated coverages: one,p10,p33,p100.")
    plan.add_argument("--workloads", help="Comma-separated workloads.")
    plan.add_argument("--seeds", help="Comma-separated random placement seeds.")
    plan.add_argument("--random-seed", type=int, default=20260627, help="Global randomization salt.")
    plan.add_argument(
        "--request-count",
        type=int,
        help="Override the per-case request count in generated plan cases.",
    )
    plan.add_argument(
        "--include-redis-fallback-smoke",
        action="store_true",
        help="Append one spine-leaf small case whose standard-sized batch exhausts reduced NetCache capacity and must use Redis fallback.",
    )
    plan.add_argument("--output", help="Output directory.")
    plan.set_defaults(func=write_plan)

    run_case_parser = sub.add_parser("run-case", help="Deploy one case, run workload, and collect data.")
    run_case_parser.add_argument("--cases", required=True)
    run_case_parser.add_argument("--case", required=True)
    run_case_parser.add_argument("--results")
    run_case_parser.add_argument("--run-id")
    run_case_parser.add_argument("--timeout", type=int, default=1200)
    run_case_parser.add_argument("--metrics-ready-timeout", type=int, default=180)
    run_case_parser.add_argument("--request-batch-size", type=int, default=4)
    run_case_parser.add_argument("--request-batch-delay", type=int, default=12)
    run_case_parser.add_argument("--skip-deploy", action="store_true")
    run_case_parser.set_defaults(func=run_case)

    run_suite_parser = sub.add_parser("run-suite", help="Run several cases from a matrix.")
    run_suite_parser.add_argument("--cases", required=True)
    run_suite_parser.add_argument("--case", action="append")
    run_suite_parser.add_argument("--results")
    run_suite_parser.add_argument("--limit", type=int)
    run_suite_parser.add_argument("--timeout", type=int, default=1200)
    run_suite_parser.add_argument("--metrics-ready-timeout", type=int, default=180)
    run_suite_parser.add_argument("--request-batch-size", type=int, default=4)
    run_suite_parser.add_argument("--request-batch-delay", type=int, default=12)
    run_suite_parser.add_argument("--settle-between", type=int, default=120)
    run_suite_parser.add_argument("--skip-deploy", action="store_true")
    run_suite_parser.add_argument("--resume", action="store_true", help="Skip cases that already have a successful summary.json.")
    run_suite_parser.add_argument("--keep-going", action="store_true", help="Continue with later cases after a case fails all retries.")
    run_suite_parser.add_argument("--retries", type=int, default=0, help="How many times to retry a failed case before marking it failed.")
    run_suite_parser.add_argument("--retry-delay", type=int, default=120, help="Seconds to wait before retrying a failed case.")
    run_suite_parser.set_defaults(func=run_suite)

    collect = sub.add_parser("collect", help="Collect an existing run without deployment or workloads.")
    collect.add_argument("--cases", required=True)
    collect.add_argument("--case", required=True)
    collect.add_argument("--run-id", required=True)
    collect.add_argument("--results")
    collect.set_defaults(func=base.collect_existing)

    summary = sub.add_parser("summarize", help="Build a CSV from completed run summaries.")
    summary.add_argument("--results", required=True)
    summary.set_defaults(func=summarize_all)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as err:
        print(err.stdout or "", file=sys.stderr)
        print(err.stderr or "", file=sys.stderr)
        return err.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
