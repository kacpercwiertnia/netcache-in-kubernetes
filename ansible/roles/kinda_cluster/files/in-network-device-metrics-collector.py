#!/usr/bin/env python3
import json
import os
import ssl
import time
import urllib.error
import urllib.request


API = "cache.evalnet.local"
VERSION = "v1alpha1"
DEVICE_NAME = os.environ["DEVICE_NAME"]
SWITCH_METRICS_URL = os.environ["SWITCH_METRICS_URL"]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))
NETCACHE_HIT_CPU_COST = float(os.environ.get("NETCACHE_HIT_CPU_COST", "1.0"))
NETCACHE_MISS_CPU_COST = float(os.environ.get("NETCACHE_MISS_CPU_COST", "8.0"))
NETCACHE_CPU_BUDGET_PPS = float(os.environ.get("NETCACHE_CPU_BUDGET_PPS", "100000.0"))
NETCACHE_RATE_EWMA_ALPHA = float(os.environ.get("NETCACHE_RATE_EWMA_ALPHA", "0.5"))


def _api_base():
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{host}:{port}"


def _ssl_context():
    return ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def _token():
    with open("/var/run/secrets/kubernetes.io/serviceaccount/token", "r", encoding="utf-8") as token_file:
        return token_file.read().strip()


BASE_URL = _api_base()
CTX = _ssl_context()
TOKEN = _token()


def request(method, path, body=None, content_type="application/json"):
    data = None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, context=CTX, timeout=15) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def get_json_url(url):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def device_path():
    return f"/apis/{API}/{VERSION}/innetworkdevices/{DEVICE_NAME}"


def cpu_load_for_rates(cache_hit_rate, cache_miss_rate):
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


def ewma(previous_value, sample_value):
    alpha = min(1.0, max(0.0, NETCACHE_RATE_EWMA_ALPHA))
    if previous_value is None:
        return sample_value
    return alpha * sample_value + (1.0 - alpha) * previous_value


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def restore_state():
    try:
        device = request("GET", device_path())
    except Exception as exc:
        print(
            f"[indev-metrics-collector] could not restore state for {DEVICE_NAME}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return None, {}, {}

    metrics = device.get("status", {}).get("metrics", {})
    rate_window = metrics.get("rateWindow", {})
    baseline = None
    if rate_window:
        baseline = {
            "observedAt": as_float(rate_window.get("startedAtEpochSeconds")),
            "cacheHits": as_int(rate_window.get("baselineCacheHits")),
            "cacheMisses": as_int(rate_window.get("baselineCacheMisses")),
        }

    peaks = {
        "cacheHitRatePps": as_float(metrics.get("maxCacheHitRatePps")),
        "cacheMissRatePps": as_float(metrics.get("maxCacheMissRatePps")),
        "estimatedCpuLoad": as_float(metrics.get("maxEstimatedCpuLoad")),
    }
    smoothed = {
        "cacheHitRatePps": as_float(metrics.get("smoothedCacheHitRatePps")),
        "cacheMissRatePps": as_float(metrics.get("smoothedCacheMissRatePps")),
    }
    return baseline, peaks, smoothed


def refresh_metrics(previous, baseline, peaks, smoothed):
    sample = get_json_url(SWITCH_METRICS_URL)
    observed_at = time.time()
    packets = int(sample.get("packetsProcessed", 0))
    byte_count = int(sample.get("bytesProcessed", 0))
    cache_hits = int(sample.get("cacheHits", 0))
    cache_misses = int(sample.get("cacheMisses", 0))
    packet_rate = 0.0
    bit_rate = 0.0
    cache_hit_rate = 0.0
    cache_miss_rate = 0.0
    if previous:
        elapsed = max(0.0, observed_at - previous["observedAt"])
        if elapsed > 0:
            packet_rate = max(0.0, packets - previous["packetsProcessed"]) / elapsed
            bit_rate = max(0.0, byte_count - previous["bytesProcessed"]) * 8 / elapsed
            cache_hit_rate = max(0.0, cache_hits - previous["cacheHits"]) / elapsed
            cache_miss_rate = max(0.0, cache_misses - previous["cacheMisses"]) / elapsed
    if baseline is None:
        baseline = {
            "observedAt": observed_at,
            "cacheHits": cache_hits,
            "cacheMisses": cache_misses,
        }
    if baseline and (cache_hits < baseline["cacheHits"] or cache_misses < baseline["cacheMisses"]):
        baseline = {
            "observedAt": observed_at,
            "cacheHits": cache_hits,
            "cacheMisses": cache_misses,
        }
    avg_cache_hit_rate = 0.0
    avg_cache_miss_rate = 0.0
    if baseline:
        elapsed_total = max(0.0, observed_at - baseline["observedAt"])
        if elapsed_total > 0:
            avg_cache_hit_rate = max(0.0, cache_hits - baseline["cacheHits"]) / elapsed_total
            avg_cache_miss_rate = max(0.0, cache_misses - baseline["cacheMisses"]) / elapsed_total
    smoothed["cacheHitRatePps"] = ewma(smoothed.get("cacheHitRatePps"), cache_hit_rate)
    smoothed["cacheMissRatePps"] = ewma(smoothed.get("cacheMissRatePps"), cache_miss_rate)
    if cache_hit_rate == 0.0 and cache_miss_rate == 0.0 and (cache_hits > 0 or cache_misses > 0):
        smoothed["cacheHitRatePps"] = max(smoothed["cacheHitRatePps"], avg_cache_hit_rate)
        smoothed["cacheMissRatePps"] = max(smoothed["cacheMissRatePps"], avg_cache_miss_rate)
    smoothed_estimated_cpu_load = cpu_load_for_rates(
        smoothed["cacheHitRatePps"],
        smoothed["cacheMissRatePps"],
    )
    peaks["cacheHitRatePps"] = max(peaks.get("cacheHitRatePps", 0.0), cache_hit_rate, avg_cache_hit_rate)
    peaks["cacheMissRatePps"] = max(peaks.get("cacheMissRatePps", 0.0), cache_miss_rate, avg_cache_miss_rate)
    peaks["estimatedCpuLoad"] = max(
        peaks.get("estimatedCpuLoad", 0.0),
        smoothed_estimated_cpu_load,
    )
    cache_query_rate = cache_hit_rate + cache_miss_rate
    cache_miss_ratio = cache_miss_rate / cache_query_rate if cache_query_rate else 0.0
    avg_cache_query_rate = avg_cache_hit_rate + avg_cache_miss_rate
    avg_cache_miss_ratio = avg_cache_miss_rate / avg_cache_query_rate if avg_cache_query_rate else 0.0
    estimated_cpu_load = cpu_load_for_rates(cache_hit_rate, cache_miss_rate)
    avg_estimated_cpu_load = cpu_load_for_rates(avg_cache_hit_rate, avg_cache_miss_rate)

    device = request("GET", device_path())
    status = dict(device.get("status", {}))
    status["metrics"] = {
        "observedAtEpochSeconds": observed_at,
        "source": "cluster",
        "packetsProcessed": packets,
        "bytesProcessed": byte_count,
        "packetRatePps": round(packet_rate, 3),
        "bitRateBps": round(bit_rate, 3),
        "cacheHits": cache_hits,
        "cacheMisses": cache_misses,
        "cacheHitRatePps": round(cache_hit_rate, 3),
        "cacheMissRatePps": round(cache_miss_rate, 3),
        "smoothedCacheHitRatePps": round(smoothed["cacheHitRatePps"], 3),
        "smoothedCacheMissRatePps": round(smoothed["cacheMissRatePps"], 3),
        "avgCacheHitRatePps": round(avg_cache_hit_rate, 3),
        "avgCacheMissRatePps": round(avg_cache_miss_rate, 3),
        "maxCacheHitRatePps": round(peaks["cacheHitRatePps"], 3),
        "maxCacheMissRatePps": round(peaks["cacheMissRatePps"], 3),
        "cacheHitRatio": sample.get("cacheHitRatio", 0.0),
        "cacheMissRatio": round(cache_miss_ratio, 6),
        "avgCacheMissRatio": round(avg_cache_miss_ratio, 6),
        "instantEstimatedCpuLoad": round(estimated_cpu_load, 6),
        "estimatedCpuLoad": round(smoothed_estimated_cpu_load, 6),
        "avgEstimatedCpuLoad": round(avg_estimated_cpu_load, 6),
        "maxEstimatedCpuLoad": round(peaks["estimatedCpuLoad"], 6),
        "cpuPressureModel": {
            "source": "netcache-hit-miss-rate",
            "hitCost": NETCACHE_HIT_CPU_COST,
            "missCost": NETCACHE_MISS_CPU_COST,
            "budgetPps": NETCACHE_CPU_BUDGET_PPS,
            "rateSmoothing": "ewma",
            "ewmaAlpha": NETCACHE_RATE_EWMA_ALPHA,
        },
        "rateWindow": {
            "startedAtEpochSeconds": baseline["observedAt"],
            "baselineCacheHits": baseline["cacheHits"],
            "baselineCacheMisses": baseline["cacheMisses"],
        },
        "perPortPackets": sample.get("perPortPackets", {}),
        "perPortBytes": sample.get("perPortBytes", {}),
    }
    request(
        "PATCH",
        f"{device_path()}/status",
        {"status": status},
        content_type="application/merge-patch+json",
    )
    return {
        "observedAt": observed_at,
        "packetsProcessed": packets,
        "bytesProcessed": byte_count,
        "cacheHits": cache_hits,
        "cacheMisses": cache_misses,
    }, baseline, peaks, smoothed


def main():
    print(f"[indev-metrics-collector] started for {DEVICE_NAME}", flush=True)
    previous = None
    baseline, peaks, smoothed = restore_state()
    while True:
        try:
            previous, baseline, peaks, smoothed = refresh_metrics(previous, baseline, peaks, smoothed)
        except Exception as exc:
            print(
                f"[indev-metrics-collector] refresh failed for {DEVICE_NAME}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
