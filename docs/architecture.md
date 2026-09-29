# Architecture

| Component | Function |
|---|---|
| Existing MicroK8s | Hosts the lab pods. Nodes come from inventory. |
| Multus, Megalos, Kathara | Connect fabric devices and inner workers. Installed by `ansible/site.yml`. |
| EvalNet + `adj_to_kathara.py` | Generate graph, addresses, routes, startup files, `topology.meta.json` and `topology.db`. |
| Fabric | BMv2/NetCache on selected devices; Linux IP forwarding on the rest. Started by Ansible. |
| NetCache fork | Switch controller, cache API, metrics API and key-value backends. |
| `kinda_cluster` | Privileged `kindest/node` pods, kubeadm control plane, workers attached to access switches. |
| NetCacheAware scheduler | Selects switch and worker for each request. |
| Kubernetes request controller | Creates client pods and handles Redis fallback. |

Inner CRDs: `InNetworkDevice`, `NetCacheSwitch`, `NetCacheReachability`, `NetCacheClientRequest`.

## Network

| Setting | Value |
|---|---|
| Inner pod CIDR | `172.30.0.0/16` |
| Inner service CIDR | `172.31.0.0/16` |
| Switch control API | `18080` |
| NetCache protocol | `50000` |

One active lab per dedicated outer cluster. Deployment replaces the previous lab and removes existing Kathara namespaces. Metrics use [cluster collectors or SSH](configuration.md#shared-deployment-configuration).

## Inspect and stop

On the outer control plane, as the inventory user:

```bash
microk8s kubectl get nodes
microk8s kubectl get pods -A
cd ~/topology-workspace/outputs/LAB_NAME
kathara linfo
timeout --kill-after=10s 120s python3 -m kathara lclean
```

Inner nodes and services share the lab namespace. If cleanup leaves resources, delete that lab's namespace. MicroK8s and its networking add-ons remain installed.

## Inner-node cgroups

Bootstrap checks PID 1's cgroup path against the outer pod UID and creates a `kinda-pods` child directory. Kubelet uses `cgroupfs` with this full path as `--cgroup-root`; containerd uses `SystemdCgroup=false`.
