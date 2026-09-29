# Configuration

Deployment and experiments: local repo, active `.venv`. Requests: inner Kubernetes; see [README](../README.md#interactive-lab).

## Experiment presets

```bash
python scripts/run_preset.py small --list
python scripts/run_preset.py small \
  --case spine-leaf-small-p33-zipf-a099-read-miss20-seed1 \
  --results .generated/experiments/custom --retries 0
```

| Argument | Meaning / default in `run_preset.py` |
|---|---|
| `small`, `medium`, `large` | Select the matrix size; see [topologies and workloads](presets.md). |
| `--list` | List case names without deploying. |
| `--case NAME` | Select a case; repeat to select several. Without it, run the entire matrix. |
| `--limit N` | Run at most the first N selected cases. |
| `--results PATH` | Results directory; default `.generated/experiments/<size>`. |
| `--request-batch-size N` | Requests submitted per batch; default `4`. |
| `--request-batch-delay SECONDS` | Pause between batches; `10` for small/large, `12` for medium. |
| `--timeout SECONDS` | Wait for request completion after submission; `900` for small, `1800` for medium/large. Does not limit deployment time. |
| `--metrics-ready-timeout SECONDS` | Wait for all collectors and metrics no older than 30 seconds; default `180`. Applies to cluster and SSH modes. |
| `--settle-between SECONDS` | Pause between cases; `90`, `120`, `180` for small/medium/large. |
| `--retries N`, `--retry-delay SECONDS` | Additional attempts per failed case (`3`) and delay (`180`). Use `--retries 0` for one attempt. |
| `--skip-deploy` | Use the existing lab. Its topology and metrics mode must match the selected case. |

Failed, missing or unfinished requests produce a nonzero exit status after results are saved. Defaults: resume completed cases, continue after failures, redeploy for each case. Use a new results directory to repeat completed cases. Topology, device IDs and workload come from the case file.

Direct execution: `python scripts/netcache_experiment_matrix.py run-case --cases presets/small.json --case NAME`. No automatic resume or retries. Collect an existing run with `python scripts/netcache_experiment_matrix.py collect --cases presets/small.json --case NAME --run-id ID --results PATH`. This command only reads results; it does not deploy, submit requests or start metrics collection.

For custom matrices, use `netcache_experiment_matrix.py plan` with `--topologies`, `--sizes`, `--coverages`, `--workloads`, `--seeds` (comma-separated lists), `--request-count` and `--output`. Run the generated `cases.json` with `run-case` or `run-suite --cases PATH`. `--include-redis-fallback-smoke` adds a case with reduced cache capacity that exercises Redis fallback.

## Interactive topology

```bash
./scripts/run_topology.sh xgft my-lab \
  --topology-kind spine-leaf --access-policy auto \
  --netcache-percent 33 --seed 1 -- 1 8 4
```

Creates 8 leaves, 4 spines and workers on the leaves. Replaces the previous lab.

| Argument | Meaning / default |
|---|---|
| `TOPOLOGY LAB` | EvalNet generator and lab name; name uses lowercase letters, digits and hyphens, max 63 characters. |
| `-- ...` | Arguments passed to EvalNet, e.g. `dragonfly ... -- 1`, `fattree ... -- 4`, `xgft ... -- 1 8 4`. |
| `--netcache-percent P` | Percentage of fabric devices running NetCache, rounded upward; `0 < P <= 100`, default `33`. Others use Linux routing. |
| `--netcache-nodes 2,6,7,8` | Explicit graph node IDs; use instead of `--netcache-percent`. IDs start at 0. |
| `--seed N` | Seed for percentage-based NetCache placement; default `1`. Does not seed EvalNet graph generation. |
| `--topology-kind KIND` | Access-layer interpretation: `generic` (default), `fattree`, `spine-leaf`. Set it to match the graph. |
| `--access-policy POLICY` | `auto` (default): edge/leaf devices for FatTree/Spine–Leaf, all devices for generic graphs. `all`: every device. `explicit`: use `--access-nodes`. |
| `--access-nodes 0,1,2,3` | Graph nodes receiving inner workers; use with `--access-policy explicit`. |
| `--generate-only` | Write the lab under `.generated/local-labs/<name>` without deploying. |
| `--inventory PATH` | SSH inventory; overrides `INVENTORY`. |
| `--vars-file PATH` | Ansible configuration YAML; overrides `ENVIRONMENT_VARS`. |
| `--netcache-image IMAGE`, `--host-image IMAGE` | Override NetCache runtime and Linux device/backend images. |

## Interactive request fields

Fields under `NetCacheClientRequest.spec`. Switch, worker and backend are selected automatically. Use a new request name for each execution.

| Field | Meaning / default |
|---|---|
| `operation` | `read` (default), `put`, `put-read` or `delete`. |
| `keys` | Comma-separated keys, e.g. `"demo0001,demo0002"`. |
| `prepopulateCache` | Load entries into the selected switch before the workload; default `true`. `false` skips loading but does not clear existing entries. |
| `requestedEntries` | Capacity requested from the scheduler; defaults to the number of supplied keys or cache entries. |
| `iterations` | Number of passes over all keys; integer >= 1, default `1` with the supplied client. |
| `sleepSeconds` | Pause between passes, in seconds; number >= 0, default `0` with the supplied client. |
| `cacheMissRatio` | Fraction of entries omitted during cache loading, from `0` to `1`; default `0`. The actual miss ratio also depends on existing entries and traffic. |
| `value` | Common value for cache entries and backend writes; default `netcache-client-value`. |
| `cacheEntries` | Explicit list of `{key: ..., value: ...}` entries; takes precedence over `keys` and `value` for cache loading, backend writes and key selection. |
| `image`, `imagePullPolicy` | Override the workload client image and pull policy for this request. |

`value` sets the common value. `cacheEntries` overrides it per key. Backend priming, writes and cache loading use the same values, including Redis fallback. Custom client images must support `NETCACHE_CLIENT_ENTRIES`.

Result fields: `status.phase`, `status.reason`, `status.cacheMode`, `status.switchName`, `status.podName`. `cacheMode: redis` means fallback. Requests execute asynchronously.

## Shared deployment configuration

| Variable | Meaning |
|---|---|
| `INVENTORY` | Inventory path; default `inventory/hosts.yml`. |
| `ENVIRONMENT_VARS` | Optional Ansible YAML configuration path. |
| `NETCACHE_METRICS_MODE` | `cluster` (default): collectors keep running with the lab. `ssh`: metrics are updated by the experiment runner while it runs. Redeploy to change modes. |
| `NETCACHE_IMAGE` | Default NetCache runtime image for topology deployment. |
| `KINDA_NETCACHE_SWITCH_CAPACITY_ENTRIES`, `KINDA_NETCACHE_SWITCH_CAPACITY_SLOTS` | Override advertised per-switch capacity; these do not resize P4 tables. Capacity settings in a custom experiment case take precedence over these environment variables. |

```bash
cp config/environment.example.yml config/environment.yml
export ENVIRONMENT_VARS="$PWD/config/environment.yml"
```

Ansible parameters in the YAML:

- `kinda_control_plane_cpu_request`, `kinda_control_plane_memory_request`, `kinda_worker_cpu_request`, `kinda_worker_memory_request`: inner-node resource requests.
- `kinda_node_image`, `kinda_netcache_scheduler_image`, `kinda_netcache_controller_client_image`: component images; see [building images](images.md).
- `kinda_netcache_scheduler_capacity_weight`, `kinda_netcache_scheduler_worker_weight`, `kinda_netcache_scheduler_device_weight`, `kinda_netcache_scheduler_hop_weight`: placement scoring weights.
- `kinda_in_network_device_metrics_poll_seconds` and the `hit_cpu_cost`, `miss_cpu_cost`, `cpu_budget_pps` parameters with the same prefix: in-cluster collector interval and estimated CPU model. The SSH bridge uses its own constants.
- `kinda_wait_timeout_seconds`, `kinda_join_retries`, `kinda_join_retry_delay_seconds`, `topology_kathara_lclean_timeout_seconds`: readiness, joining and cleanup timing.

Defaults: [kinda_cluster](../ansible/roles/kinda_cluster/defaults/main.yml), [group variables](../ansible/group_vars/all.yml), [topology tasks](../ansible/roles/topology_pipeline/tasks/main.yml). Capacity environment variables override YAML. Scripts read metrics mode from `NETCACHE_METRICS_MODE`.
