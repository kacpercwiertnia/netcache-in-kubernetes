# Experiment presets

Files: `presets/{small,medium,large}.json`. Values below count fabric devices.

| Topology | Small | Medium | Large |
|---|---:|---:|---:|
| FatTree | 20 (k=4) | 45 (k=6) | 80 (k=8) |
| Spine-Leaf | 12 (8+4) | 32 (24+8) | 80 (64+16) |
| SlimFly | 18 (q=3) | 50 (q=5) | 98 (q=7) |
| Dragonfly | 6 (p=1) | 36 (p=2) | 114 (p=3) |
| Jellyfish | 18 (degree 4) | 48 (degree 6) | 96 (degree 8) |

Every size contains:

- coverage `one`, `p10`, `p33`, `p100`, rounded upward to an integer device count;
- workloads `zipf-a099-read`, `zipf-a099-read-miss20`, `dynamic-random-miss20`;
- placement seeds 1, 2 and 3;
- 180 cases, with 24 requests per case for small and medium, 64 for large.

Seeds control NetCache placement and workloads. Jellyfish graph generation uses EvalNet randomness. Generated graphs remain in the local lab directory.

## Runtime and results

Each case redeploys the lab and submits `NetCacheClientRequest` resources. Outputs: JSON resources, diagnostics and summary; `summarize` aggregates results into CSV. See [runner flags](configuration.md#experiment-presets).

Metrics mode: `NETCACHE_METRICS_MODE=cluster` (default) or `ssh`. Changing it requires redeployment.

Both modes read switch `/metrics` on port `18080` and update `InNetworkDevice.status.metrics`. SSH mode accesses the API through `microk8s kubectl exec`.

The scheduler uses these metrics, capacity, worker load and network distance. Cluster collectors remain active after experiments; the SSH bridge stops with the runner.

`estimatedCpuLoad` is calculated from hit/miss rates and configured costs, not measured CPU usage.

Run one matrix/deployment at a time per outer cluster.
