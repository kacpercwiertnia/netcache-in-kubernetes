# NetCache scheduler plugin

Kubernetes scheduler with the `NetCacheAware` plugin.

Handles controller placement pods with `spec.schedulerName: netcache-scheduler`. Requested entries and slots come from pod annotations.

Extension points:

- `PreFilter`: reads request size from pod annotations and snapshots
  `NetCacheSwitch` / `InNetworkDevice` CRDs.
- `Filter`: keeps only nodes attached to Ready NetCache devices with enough
  free entries and slots.
- `Score`: combines NetCache capacity, in-network device CPU pressure, hop count,
  and worker load.
- `Reserve` / `Unreserve`: reserves or releases switch capacity through
  `NetCacheSwitch.status`.
- `PostBind`: annotates the placement pod with the selected switch and worker.

Kubernetes access: API server.
