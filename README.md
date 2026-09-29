# NetCache in Kubernetes

Network lab on an existing MicroK8s cluster: EvalNet topology, P4/NetCache and Linux switches, inner Kubernetes cluster, experiment presets and manual requests.

## Prerequisites

- **Local machine:** Linux or macOS, Python 3.11–3.13, Git, OpenSSH and running Docker with Linux/amd64 support.
- **Cluster:** an existing Linux/amd64 MicroK8s control plane and workers, all `Ready`, with working networking, DNS, storage and image pulls. Privileged containers and hostPath mounts must be allowed.
- **Access:** SSH key/agent access to every node, passwordless sudo and permission to run `microk8s` as the SSH user.
- **Internet:** access to GitHub, Python packages and container registries from the relevant machines.

Use a dedicated lab cluster: deployment labels/taints the outer control plane and replaces existing Kathara labs. The repository does not provision VMs or install MicroK8s.

## Install dependencies

Run from the repository root:

```bash
./scripts/setup.sh
source .venv/bin/activate
```

Installs `requirements.txt` into `.venv/` and downloads EvalNet and NetCache into `.deps/` at revisions from `dependencies.json`. Uses published container images. Optional: [build images](docs/images.md).

## Example flow

### 1. Configure access

```bash
cp inventory/aws.example.yml inventory/hosts.yml
# Edit hosts.yml: node IPs, SSH user and private key path.
export INVENTORY="$PWD/inventory/hosts.yml"
ansible all -i "$INVENTORY" -m ping
```

Use one host in `control` and the remaining MicroK8s nodes in `workers`. Do not set `ansible_become` in inventory. For AGH, use `inventory/agh.example.yml`. See [AWS](docs/aws.md) or [AGH](docs/agh.md) for access details.

### 2. Compile P4 and prepare the cluster

```bash
./scripts/build_p4.sh
./scripts/prepare_cluster.sh
```

Compiles NetCache P4 and configures Multus, Megalos and Kathara.

### 3. Deploy a network and inner Kubernetes cluster

```bash
./scripts/run_topology.sh dragonfly demo \
  --netcache-percent 33 --access-policy explicit --access-nodes 0,1,2,3 \
  -- 1
```

Creates two NetCache switches, four Linux switches, four inner workers and an inner control plane. Includes scheduler, controller and metrics collectors. For manual requests, continue with [Interactive lab](#interactive-lab).

### 4. Run an experiment

```bash
python scripts/run_preset.py small \
  --case dragonfly-small-p33-zipf-a099-read-miss20-seed1
```

The preset **replaces the demo lab with its own topology**, then runs 24 requests. Metrics are collected inside the cluster by default (`NETCACHE_METRICS_MODE=cluster`). Results and logs are saved under `.generated/experiments/small/`.

Presets are available for `small`, `medium` and `large`; omit `--case` to run the entire selected matrix. See [Presets](docs/presets.md) and [experiment flags](docs/configuration.md#experiment-presets).

## Interactive lab

See [topology options](docs/configuration.md#interactive-topology) and [request fields](docs/configuration.md#interactive-request-fields).

After steps 1–3, SSH to the outer control plane as the inventory user. Default metrics mode: `cluster`. Run:

```bash
NS=$(microk8s kubectl get pods -A -l kinda.local/role=control-plane \
  -o jsonpath='{.items[0].metadata.namespace}')

ikubectl() {
  microk8s kubectl -n "$NS" exec -i kinda-control-plane -- \
    kubectl --kubeconfig /etc/kubernetes/admin.conf "$@"
}

REQUEST="manual-read-$(date +%s)"
ikubectl create -f - <<EOF
apiVersion: cache.evalnet.local/v1alpha1
kind: NetCacheClientRequest
metadata:
  name: ${REQUEST}
  namespace: default
spec:
  operation: read
  keys: "demo0001,demo0002,demo0003"
  requestedEntries: 3
  prepopulateCache: true
  iterations: 10
  sleepSeconds: 0.2
EOF

ikubectl -n default wait --for=jsonpath='{.status.phase}'=Succeeded \
  "ncreq/$REQUEST" --timeout=180s
ikubectl -n default get "ncreq/$REQUEST" -o yaml
POD=$(ikubectl -n default get "ncreq/$REQUEST" -o jsonpath='{.status.podName}')
ikubectl -n default logs "$POD" -c client
```

Loads three cache entries and reads each ten times. Inspect `status.phase`, `status.cacheMode` and `status.switchName`; on failure, check `status.reason`.

## Documentation

[Configuration](docs/configuration.md) · [Architecture and stopping the lab](docs/architecture.md) · [Images](docs/images.md) · [Presets](docs/presets.md)
