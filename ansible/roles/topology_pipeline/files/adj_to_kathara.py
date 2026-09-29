#!/usr/bin/env python3

from __future__ import annotations

import ipaddress
import json
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str  # switch | host | kinda_worker
    index: int


@dataclass(frozen=True)
class Link:
    lan: str
    subnet: ipaddress.IPv4Network
    a: Endpoint
    b: Endpoint


TOPOLOGY_KINDS = {"generic", "fattree", "spine-leaf"}
ACCESS_POLICIES = {"auto", "all", "explicit"}


def parse_adj_file(path: Path) -> tuple[int, list[tuple[int, int]], list[list[int]]]:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if not lines:
        raise ValueError("Empty adjacency file.")

    first = lines[0].split()
    if len(first) != 2:
        raise ValueError("First line must contain: <num_nodes> <num_edges>")

    n = int(first[0])
    expected_edges = int(first[1])

    if len(lines) != n + 1:
        raise ValueError(
            f"Expected {n} adjacency lines after header, got {len(lines) - 1}."
        )

    adjacency: list[list[int]] = []
    for i in range(n):
        row = sorted({int(x) for x in lines[i + 1].split()})
        adjacency.append(row)

    edges_set: set[tuple[int, int]] = set()
    for u in range(n):
        for v in adjacency[u]:
            if v < 0 or v >= n:
                raise ValueError(f"Node {u} references invalid neighbor {v}.")
            if u == v:
                raise ValueError(f"Self-loop detected at node {u}.")
            a, b = sorted((u, v))
            edges_set.add((a, b))

    edges = sorted(edges_set)

    for u in range(n):
        for v in adjacency[u]:
            if u not in adjacency[v]:
                raise ValueError(
                    f"Adjacency is not symmetric: {u} lists {v}, but {v} does not list {u}."
                )

    if len(edges) != expected_edges:
        raise ValueError(
            f"Header says {expected_edges} edges, but parsed {len(edges)} undirected edges."
        )

    ensure_connected(n, adjacency)
    return n, edges, adjacency


def ensure_connected(n: int, adjacency: list[list[int]]) -> None:
    visited = [False] * n
    q = deque([0])
    visited[0] = True
    while q:
        u = q.popleft()
        for v in adjacency[u]:
            if not visited[v]:
                visited[v] = True
                q.append(v)
    missing = [str(i) for i, ok in enumerate(visited) if not ok]
    if missing:
        raise ValueError(f"Graph is disconnected. Unreachable nodes: {', '.join(missing)}")


def bfs_next_hops(n: int, adjacency: list[list[int]], src: int) -> dict[int, int]:
    parent = [-1] * n
    visited = [False] * n
    q = deque([src])
    visited[src] = True

    while q:
        u = q.popleft()
        for v in adjacency[u]:
            if not visited[v]:
                visited[v] = True
                parent[v] = u
                q.append(v)

    next_hop: dict[int, int] = {}
    for dst in range(n):
        if dst == src:
            continue
        if not visited[dst]:
            raise ValueError(f"Graph is disconnected. Node {src} cannot reach node {dst}.")

        cur = dst
        while parent[cur] != src:
            cur = parent[cur]
            if cur == -1:
                raise ValueError(f"Unexpected BFS reconstruction failure for {src} -> {dst}.")
        next_hop[dst] = cur

    return next_hop


def bfs_distances(n: int, adjacency: list[list[int]], src: int) -> dict[int, int]:
    distances = [-1] * n
    q = deque([src])
    distances[src] = 0

    while q:
        u = q.popleft()
        for v in adjacency[u]:
            if distances[v] == -1:
                distances[v] = distances[u] + 1
                q.append(v)

    missing = [str(i) for i, distance in enumerate(distances) if distance < 0]
    if missing:
        raise ValueError(f"Graph is disconnected. Node {src} cannot reach nodes: {', '.join(missing)}")
    return {node: distance for node, distance in enumerate(distances)}


def iface_name(idx: int) -> str:
    return f"net{idx}"


def validate_node_idx(label: str, idx: int, n: int) -> None:
    if idx < 0 or idx >= n:
        raise ValueError(f"{label} node index {idx} is outside valid range 0..{n - 1}")


def mac_for(node_kind: str, node_id: int, port: int) -> str:
    if node_kind == "switch":
        return f"02:00:{node_id & 0xff:02x}:{port & 0xff:02x}:00:01"
    return f"02:10:{node_id & 0xff:02x}:{port & 0xff:02x}:00:01"


def ip_add(addr: ipaddress.IPv4Address, offset: int) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(int(addr) + offset)


def infer_fattree_access_nodes(n: int) -> list[int]:
    p_float = math.sqrt(n / 5.0)
    p = int(p_float)
    if p * p * 5 != n:
        raise ValueError(
            f"Cannot infer fat-tree access switches from {n} nodes. "
            "Use --access-nodes to provide them explicitly."
        )
    return list(range(2 * p * p))


def bipartition(adjacency: list[list[int]]) -> tuple[list[int], list[int]]:
    color: dict[int, int] = {}
    for start in range(len(adjacency)):
        if start in color:
            continue
        color[start] = 0
        q = deque([start])
        while q:
            node = q.popleft()
            for neigh in adjacency[node]:
                expected = 1 - color[node]
                if neigh in color:
                    if color[neigh] != expected:
                        raise ValueError(
                            "Spine-leaf auto access detection requires a bipartite graph. "
                            "Use --access-nodes to provide access switches explicitly."
                        )
                    continue
                color[neigh] = expected
                q.append(neigh)
    left = sorted(node for node, value in color.items() if value == 0)
    right = sorted(node for node, value in color.items() if value == 1)
    return left, right


def infer_spine_leaf_access_nodes(adjacency: list[list[int]]) -> list[int]:
    left, right = bipartition(adjacency)
    # In common leaf-spine fabrics leaves are at least as numerous as spines.
    # On a tie, pick the side with the lower first node for deterministic output.
    if len(left) > len(right):
        return left
    if len(right) > len(left):
        return right
    return left if (left[0] if left else 0) <= (right[0] if right else 0) else right


def choose_access_nodes(
    n: int,
    adjacency: list[list[int]],
    topology_kind: str,
    access_policy: str,
    access_nodes: list[int] | None,
) -> list[int]:
    if topology_kind not in TOPOLOGY_KINDS:
        raise ValueError(f"Unsupported topology kind: {topology_kind}")
    if access_policy not in ACCESS_POLICIES:
        raise ValueError(f"Unsupported access policy: {access_policy}")

    if access_policy == "explicit":
        if not access_nodes:
            raise ValueError("--access-policy explicit requires --access-nodes")
        selected = access_nodes
    elif access_policy == "all":
        selected = list(range(n))
    elif access_nodes:
        selected = access_nodes
    elif topology_kind == "fattree":
        selected = infer_fattree_access_nodes(n)
    elif topology_kind == "spine-leaf":
        selected = infer_spine_leaf_access_nodes(adjacency)
    else:
        selected = list(range(n))

    for node in selected:
        validate_node_idx("access", node, n)
    return sorted(set(selected))


def access_layer_for(topology_kind: str) -> str:
    if topology_kind == "fattree":
        return "edge"
    if topology_kind == "spine-leaf":
        return "leaf"
    return "access"


def kathara_lan_name(index: int) -> str:
    return f"l{index:04d}"


def build_p4utils_topology_db(
    n: int,
    switch_names: dict[int, str],
    switch_ports: dict[int, dict[Any, int]],
    host_definitions: dict[str, dict[str, Any]],
    endpoint_ip: dict[tuple[str, str], ipaddress.IPv4Interface],
    endpoint_mac: dict[tuple[str, str], str],
    loopbacks: dict[int, ipaddress.IPv4Interface],
) -> dict[str, Any]:
    topology: dict[str, Any] = {}

    # Initialize switches
    for node in range(n):
        sname = switch_names[node]
        topology[sname] = {
            "type": "switch",
            "subtype": "p4switch",
            "sw_id": node,
            "cpu_port": False,
            "thrift_port": 9090,
            "loopback": str(loopbacks[node]),
            "interfaces_to_node": {},
            "interfaces_to_port": {},
        }

    # Initialize hosts
    for host_name, host_info in host_definitions.items():
        topology[host_name] = {
            "type": "host",
            "interfaces_to_node": {},
            "interfaces_to_port": {},
        }

    # Add edges / neighbor attributes
    # switches -> switches and switches -> hosts
    for node in range(n):
        sname = switch_names[node]
        for peer_key, port in switch_ports[node].items():
            if isinstance(peer_key, int):
                peer_name = switch_names[peer_key]
                peer_port = switch_ports[peer_key][node]
            else:
                peer_name = peer_key
                peer_port = 0

            local_ip = endpoint_ip[(sname, peer_name)]
            local_mac = endpoint_mac[(sname, peer_name)]
            intf = f"{sname}-eth{port}"

            topology[sname]["interfaces_to_node"][intf] = peer_name
            topology[sname]["interfaces_to_port"][intf] = port
            topology[sname][peer_name] = {
                "port": port,
                "peer_port": peer_port,
                "intf": intf,
                "ip": str(local_ip),
                "mac": local_mac,
                "bw": 0,
                "delay": "0ms",
                "loss": 0,
                "queue_length": 0,
                "weight": 1,
            }

    # host-side neighbor attrs + convenient host attrs
    for host_name, host_info in host_definitions.items():
        sw = host_info["attached_switch"]
        sname = switch_names[sw]
        host_ip = endpoint_ip[(host_name, sname)]
        host_mac = endpoint_mac[(host_name, sname)]
        gw_ip = endpoint_ip[(sname, host_name)].ip
        intf = f"{host_name}-eth0"

        topology[host_name]["interfaces_to_node"][intf] = sname
        topology[host_name]["interfaces_to_port"][intf] = 0
        topology[host_name]["ip"] = str(host_ip)
        topology[host_name]["mac"] = host_mac
        topology[host_name]["defaultRoute"] = f"via {gw_ip} dev eth0"
        topology[host_name][sname] = {
            "port": 0,
            "peer_port": switch_ports[sw][host_name],
            "intf": intf,
            "ip": str(host_ip),
            "mac": host_mac,
            "bw": 0,
            "delay": "0ms",
            "loss": 0,
            "queue_length": 0,
            "weight": 1,
        }

    return topology


def build_p4_fabric_data(
    input_file: Path,
    image: str,
    host_image: str,
    lab_name: str,
    p4_json: str,
    netcache_nodes: list[int],
    topology_kind: str = "generic",
    access_policy: str = "auto",
    access_nodes: list[int] | None = None,
) -> dict[str, Any]:
    n, edges, adjacency = parse_adj_file(input_file)
    access_node_ids = choose_access_nodes(
        n=n,
        adjacency=adjacency,
        topology_kind=topology_kind,
        access_policy=access_policy,
        access_nodes=access_nodes,
    )
    access_node_set = set(access_node_ids)
    access_layer = access_layer_for(topology_kind)

    graph_netcache_nodes = sorted(set(netcache_nodes))
    if not graph_netcache_nodes:
        raise ValueError("At least one NetCache graph node is required.")
    for node in graph_netcache_nodes:
        validate_node_idx("netcache", node, n)

    switch_names = {node: f"s{node}" for node in range(n)}
    switch_ports: dict[int, dict[Any, int]] = {node: {} for node in range(n)}
    switch_neighbors_by_port: dict[int, dict[int, Endpoint]] = {node: {} for node in range(n)}
    switch_roles = {}
    for node in range(n):
        switch_roles[node] = "base"
    for node in graph_netcache_nodes:
        switch_roles[node] = "netcache"

    for node in range(n):
        port = 0
        for neigh in adjacency[node]:
            ep = Endpoint(name=switch_names[neigh], kind="switch", index=neigh)
            switch_ports[node][neigh] = port
            switch_neighbors_by_port[node][port] = ep
            port += 1

    host_definitions = {}
    next_host_id = 0
    netcache_backend_hosts: dict[int, str] = {}
    for node in graph_netcache_nodes:
        host_name = f"hsrv{node}"
        if host_name in host_definitions:
            raise ValueError(f"Generated host name conflict for NetCache backend host {host_name}.")
        host_definitions[host_name] = {
            "attached_switch": node,
            "host_id": next_host_id,
            "role": "netcache_backend",
        }
        netcache_backend_hosts[node] = host_name
        next_host_id += 1

    for host_name, host_info in host_definitions.items():
        sw = host_info["attached_switch"]
        port = len(switch_ports[sw])
        host_ep = Endpoint(name=host_name, kind="host", index=host_info["host_id"])
        switch_ports[sw][host_name] = port
        switch_neighbors_by_port[sw][port] = host_ep

    lan_counter = 0
    next_sw_subnet_base = ipaddress.IPv4Address("10.0.0.0")
    next_host_subnet_base = ipaddress.IPv4Address("10.200.0.0")
    links: list[Link] = []
    endpoint_ip: dict[tuple[str, str], ipaddress.IPv4Interface] = {}
    endpoint_mac: dict[tuple[str, str], str] = {}

    for (u, v) in edges:
        subnet = ipaddress.ip_network(f"{ip_add(next_sw_subnet_base, lan_counter * 4)}/30", strict=False)
        lan_name = kathara_lan_name(lan_counter)
        lan_counter += 1
        a = Endpoint(name=switch_names[u], kind="switch", index=u)
        b = Endpoint(name=switch_names[v], kind="switch", index=v)
        links.append(Link(lan=lan_name, subnet=subnet, a=a, b=b))
        hosts = list(subnet.hosts())
        endpoint_ip[(a.name, b.name)] = ipaddress.ip_interface(f"{hosts[0]}/{subnet.prefixlen}")
        endpoint_ip[(b.name, a.name)] = ipaddress.ip_interface(f"{hosts[1]}/{subnet.prefixlen}")
        endpoint_mac[(a.name, b.name)] = mac_for("switch", u, switch_ports[u][v])
        endpoint_mac[(b.name, a.name)] = mac_for("switch", v, switch_ports[v][u])

    for offset, host_name in enumerate(sorted(host_definitions, key=lambda name: host_definitions[name]["host_id"])):
        sw = host_definitions[host_name]["attached_switch"]
        subnet = ipaddress.ip_network(f"{ip_add(next_host_subnet_base, offset * 4)}/30", strict=False)
        lan_name = kathara_lan_name(lan_counter)
        lan_counter += 1
        a = Endpoint(name=host_name, kind="host", index=host_definitions[host_name]["host_id"])
        b = Endpoint(name=switch_names[sw], kind="switch", index=sw)
        links.append(Link(lan=lan_name, subnet=subnet, a=a, b=b))
        hosts = list(subnet.hosts())
        endpoint_ip[(a.name, b.name)] = ipaddress.ip_interface(f"{hosts[0]}/{subnet.prefixlen}")
        endpoint_ip[(b.name, a.name)] = ipaddress.ip_interface(f"{hosts[1]}/{subnet.prefixlen}")
        endpoint_mac[(a.name, b.name)] = mac_for("host", host_definitions[host_name]["host_id"], 0)
        endpoint_mac[(b.name, a.name)] = mac_for("switch", sw, switch_ports[sw][host_name])

    kinda_access_by_node: dict[int, dict[str, Any]] = {}
    next_kinda_access_subnet_base = ipaddress.IPv4Address("10.210.0.0")
    for offset, node in enumerate(access_node_ids):
        sname = switch_names[node]
        worker_name = f"kinda-worker-{sname}"
        port = len(switch_ports[node])
        switch_ports[node][worker_name] = port
        worker_ep = Endpoint(name=worker_name, kind="kinda_worker", index=node)
        switch_neighbors_by_port[node][port] = worker_ep

        subnet = ipaddress.ip_network(f"{ip_add(next_kinda_access_subnet_base, offset * 4)}/30", strict=False)
        lan_name = kathara_lan_name(lan_counter)
        lan_counter += 1
        switch_ep = Endpoint(name=sname, kind="switch", index=node)
        links.append(Link(lan=lan_name, subnet=subnet, a=worker_ep, b=switch_ep))
        hosts = list(subnet.hosts())
        endpoint_ip[(worker_name, sname)] = ipaddress.ip_interface(f"{hosts[0]}/{subnet.prefixlen}")
        endpoint_ip[(sname, worker_name)] = ipaddress.ip_interface(f"{hosts[1]}/{subnet.prefixlen}")
        endpoint_mac[(worker_name, sname)] = mac_for("kinda_worker", node + 128, 0)
        endpoint_mac[(sname, worker_name)] = mac_for("switch", node, port)
        kinda_access_by_node[node] = {
            "lan": lan_name,
            "switch_port": port,
            "switch_ifname": iface_name(port),
            "switch_ip": str(endpoint_ip[(sname, worker_name)]),
            "switch_mac": endpoint_mac[(sname, worker_name)],
            "worker_ip": str(endpoint_ip[(worker_name, sname)]),
            "worker_mac": endpoint_mac[(worker_name, sname)],
            "pod_subnet": f"172.30.{node + 1}.0/24",
        }

    loopbacks = {node: ipaddress.ip_interface(f"10.255.0.{node + 1}/32") for node in range(n)}
    next_hops_cache = {node: bfs_next_hops(n, adjacency, node) for node in range(n)}

    meta: dict[str, Any] = {
        "profile": "mixed-fabric-netcache-node",
        "topology_kind": topology_kind,
        "access_policy": access_policy,
        "source_file": str(input_file),
        "lab_name": lab_name,
        "switch_image": image,
        "host_image": host_image,
        "p4_json": p4_json,
        "access_switches": [],
        "switches": {},
        "hosts": {},
        "links": [],
    }

    graph_netcache_switches = [switch_names[node] for node in graph_netcache_nodes]
    meta["netcache"] = {
        "enabled": True,
        "mode": "graph-nodes" if len(graph_netcache_switches) > 1 else "graph-node",
        "switches": graph_netcache_switches,
        "switch_nodes": graph_netcache_nodes,
        "switch_role": "graph_netcache_router",
        "switch_image": image,
        "p4_json": p4_json,
        "generated_topology_db": "generated/netcache/topology.db",
        "topology_db_path": "generated/netcache/topology.db",
    }

    for link in links:
        meta["links"].append(
            {
                "lan": link.lan,
                "subnet": str(link.subnet),
                "endpoints": [
                    {
                        "name": link.a.name,
                        "kind": link.a.kind,
                        "peer": link.b.name,
                        "ip": str(endpoint_ip[(link.a.name, link.b.name)]),
                        "mac": endpoint_mac[(link.a.name, link.b.name)],
                    },
                    {
                        "name": link.b.name,
                        "kind": link.b.kind,
                        "peer": link.a.name,
                        "ip": str(endpoint_ip[(link.b.name, link.a.name)]),
                        "mac": endpoint_mac[(link.b.name, link.a.name)],
                    },
                ],
            }
        )

    for node in range(n):
        sname = switch_names[node]
        ports_meta: dict[str, Any] = {}
        for port, peer_ep in sorted(switch_neighbors_by_port[node].items()):
            local_ip = endpoint_ip[(sname, peer_ep.name)]
            local_mac = endpoint_mac[(sname, peer_ep.name)]
            peer_ip = endpoint_ip[(peer_ep.name, sname)]
            peer_mac = endpoint_mac[(peer_ep.name, sname)]
            ports_meta[str(port)] = {
                "ifname": iface_name(port),
                "peer": peer_ep.name,
                "peer_kind": peer_ep.kind,
                "peer_port": switch_ports[peer_ep.index][node] if peer_ep.kind == "switch" else 0,
                "ip": str(local_ip),
                "mac": local_mac,
                "peer_ip": str(peer_ip),
                "peer_mac": peer_mac,
            }
        role = switch_roles[node]
        selected_p4_json = None
        selected_image = host_image
        if role == "netcache":
            selected_p4_json = p4_json
            selected_image = image
        switch_meta: dict[str, Any] = {
            "node_id": node,
            "role": role,
            "fabric_type": role,
            "topology_role": access_layer if node in access_node_set else "fabric",
            "topology_layer": access_layer if node in access_node_set else None,
            "access": node in access_node_set,
            "netcache_capable": role == "netcache",
            "netcache_active": role == "netcache",
            "loopback": str(loopbacks[node]),
            "ports": ports_meta,
            "p4_json": selected_p4_json,
            "image": selected_image,
        }
        if role == "netcache":
            control_plane_ports = [
                {
                    "ifname": port_meta["ifname"],
                    "ip": port_meta["ip"],
                }
                for _, port_meta in sorted(ports_meta.items(), key=lambda item: int(item[0]))
                if "ip" in port_meta
            ]
            control_plane_routes = []
            next_hops = next_hops_cache[node]
            for access_node, access_info in sorted(kinda_access_by_node.items()):
                route_prefixes = [
                    access_info["pod_subnet"],
                    f"{ipaddress.ip_interface(access_info['worker_ip']).ip}/32",
                ]
                if node == access_node:
                    worker_name = f"kinda-worker-{switch_names[access_node]}"
                    via = str(ipaddress.ip_interface(access_info["worker_ip"]).ip)
                    dev = iface_name(switch_ports[node][worker_name])
                    for prefix in route_prefixes:
                        control_plane_routes.append({"prefix": prefix, "via": via, "dev": dev})
                    continue
                nh = next_hops[access_node]
                via = str(endpoint_ip[(switch_names[nh], sname)].ip)
                dev = iface_name(switch_ports[node][nh])
                for prefix in route_prefixes:
                    control_plane_routes.append({"prefix": prefix, "via": via, "dev": dev})
            switch_meta["netcache"] = {
                "mode": "graph-node",
                "topology_db_path": "generated/netcache/topology.db",
                "thrift_port": 9090,
            }
            switch_meta["control_plane"] = {
                "loopback": str(loopbacks[node]),
                "ports": control_plane_ports,
                "routes": control_plane_routes,
            }
        meta["switches"][sname] = switch_meta

    meta["access_switches"] = [
        {
            "switch": switch_names[node],
            "node_id": node,
            "layer": access_layer,
            "worker_name": f"kinda-worker-{switch_names[node]}",
            "netcache_capable": meta["switches"][switch_names[node]]["netcache_capable"],
            "netcache_active": meta["switches"][switch_names[node]]["netcache_active"],
            "kinda_access": kinda_access_by_node[node],
        }
        for node in access_node_ids
    ]
    meta["kinda"] = {
        "enabled": True,
        "worker_count": len(access_node_ids),
        "workers": [
            {
                "name": f"kinda-worker-{switch_names[node]}",
                "attached_switch": switch_names[node],
                "attached_node_id": node,
                "access_layer": access_layer,
                "netcache_capable": meta["switches"][switch_names[node]]["netcache_capable"],
                "netcache_active": meta["switches"][switch_names[node]]["netcache_active"],
                "access_lan": kinda_access_by_node[node]["lan"],
                "access_switch_port": kinda_access_by_node[node]["switch_port"],
                "access_switch_ifname": kinda_access_by_node[node]["switch_ifname"],
                "access_switch_ip": kinda_access_by_node[node]["switch_ip"],
                "access_switch_mac": kinda_access_by_node[node]["switch_mac"],
                "access_worker_ip": kinda_access_by_node[node]["worker_ip"],
                "access_worker_mac": kinda_access_by_node[node]["worker_mac"],
                "client_pod_subnet": kinda_access_by_node[node]["pod_subnet"],
            }
            for node in access_node_ids
        ],
    }

    access_netcache_distances = {
        access_node: bfs_distances(n, adjacency, access_node)
        for access_node in access_node_ids
    }
    netcache_switch_resources = []
    for nc_node in graph_netcache_nodes:
        nearest_access_node, nearest_distance = min(
            (
                (access_node, access_netcache_distances[access_node][nc_node])
                for access_node in access_node_ids
            ),
            key=lambda item: (item[1], item[0]),
        )
        control_host = str(loopbacks[nc_node].ip)
        switch_resource = {
            "switchName": switch_names[nc_node],
            "switchNodeId": nc_node,
            "deviceRef": switch_names[nc_node],
            "controlAccessSwitch": switch_names[nearest_access_node],
            "controlAccessNodeId": nearest_access_node,
            "controlPathCost": nearest_distance,
            "controlEndpoint": {
                "host": control_host,
                "port": 18080,
                "url": f"http://{control_host}:18080",
            },
        }
        if nc_node in netcache_backend_hosts:
            backend_host = netcache_backend_hosts[nc_node]
            backend_ip = endpoint_ip[(backend_host, switch_names[nc_node])]
            switch_resource.update(
                {
                    "backendServerName": backend_host,
                    "backendServerIp": str(backend_ip.ip),
                }
            )
        if nc_node in access_node_set:
            switch_resource.update(
                {
                    "attachedWorker": f"kinda-worker-{switch_names[nc_node]}",
                    "attachedNodeId": nc_node,
                    "accessSwitchIp": kinda_access_by_node[nc_node]["switch_ip"],
                    "accessWorkerIp": kinda_access_by_node[nc_node]["worker_ip"],
                    "clientPodSubnet": kinda_access_by_node[nc_node]["pod_subnet"],
                }
            )
        netcache_switch_resources.append(switch_resource)

    meta.setdefault("netcache", {})["reachability"] = {
        "backendMode": "selected-switch-local",
        "workers": [
            {
                "workerName": f"kinda-worker-{switch_names[access_node]}",
                "attachedSwitch": switch_names[access_node],
                "attachedNodeId": access_node,
                "candidates": [
                    {
                        "switchName": switch_names[nc_node],
                        "switchNodeId": nc_node,
                        "hops": distance,
                        "pathCost": distance,
                    }
                    for nc_node, distance in sorted(
                        (
                            (nc_node, access_netcache_distances[access_node][nc_node])
                            for nc_node in graph_netcache_nodes

                        ),
                        key=lambda item: (item[1], item[0]),
                    )
                ],
            }
            for access_node in access_node_ids
        ]
    }
    meta.setdefault("netcache", {})["switchResources"] = netcache_switch_resources

    for host_name, host_info in host_definitions.items():
        sw = host_info["attached_switch"]
        sname = switch_names[sw]
        host_ip = endpoint_ip[(host_name, sname)]
        gw_ip = endpoint_ip[(sname, host_name)]
        host_mac = endpoint_mac[(host_name, sname)]
        gw_mac = endpoint_mac[(sname, host_name)]
        meta["hosts"][host_name] = {
            "attached_to": sname,
            "attached_switch_node": sw,
            "role": host_info["role"],
            "interface": "net0",
            "ip": str(host_ip),
            "subnet": str(host_ip.network),
            "gateway": str(gw_ip.ip),
            "gateway_with_prefix": str(gw_ip),
            "mac": host_mac,
            "gateway_mac": gw_mac,
        }

    switch_cli: dict[str, list[str]] = {switch_names[node]: [] for node in range(n)}

    for node in range(n):
        sname = switch_names[node]
        for port, peer_ep in sorted(switch_neighbors_by_port[node].items()):
            local_ip = endpoint_ip[(sname, peer_ep.name)].ip
            local_mac = endpoint_mac[(sname, peer_ep.name)]
            switch_cli[sname].append(
                f"table_add arp_router_reply send_arp_reply {local_ip} {port} => {local_mac} {local_ip}"
            )

    host_route_targets: dict[str, dict[str, Any]] = {}
    for host_name, host_info in host_definitions.items():
        attached = host_info["attached_switch"]
        sname = switch_names[attached]
        host_ip = endpoint_ip[(host_name, sname)]
        host_route_targets[host_name] = {
            "prefix_32": f"{host_ip.ip}/32",
            "subnet": str(host_ip.network),
            "attached_switch": attached,
        }

    for src in range(n):
        sname = switch_names[src]
        next_hops = next_hops_cache[src]

        def add_route(prefix: str, next_peer_name: str) -> None:
            if next_peer_name == sname:
                return
            if next_peer_name in host_definitions or next_peer_name.startswith("kinda-worker-"):
                port = switch_ports[src][next_peer_name]
            else:
                port = switch_ports[src][int(next_peer_name[1:])]
            dst_mac = endpoint_mac[(next_peer_name, sname)]
            src_mac = endpoint_mac[(sname, next_peer_name)]
            switch_cli[sname].append(
                f"table_add ipv4_lpm set_nhop {prefix} => {dst_mac} {src_mac} {port}"
            )

        for dst in range(n):
            if dst == src:
                continue
            nh = next_hops[dst]
            add_route(str(loopbacks[dst]), switch_names[nh])

        for host_name, route_info in host_route_targets.items():
            attached = route_info["attached_switch"]
            prefixes = [route_info["prefix_32"], route_info["subnet"]]
            if src == attached:
                for prefix in prefixes:
                    add_route(prefix, host_name)
            else:
                nh = next_hops[attached]
                for prefix in prefixes:
                    add_route(prefix, switch_names[nh])

        for access_node, access_info in kinda_access_by_node.items():
            prefix = access_info["pod_subnet"]
            worker_name = f"kinda-worker-{switch_names[access_node]}"
            if src == access_node:
                add_route(prefix, worker_name)
            else:
                nh = next_hops[access_node]
                add_route(prefix, switch_names[nh])

    meta["cli_files"] = {name: f"{name}/{name}.cli" for name in switch_cli if meta["switches"][name]["netcache_capable"]}

    generated_netcache_db = None
    generated_netcache_db = build_p4utils_topology_db(
        n=n,
        switch_names=switch_names,
        switch_ports=switch_ports,
        host_definitions=host_definitions,
        endpoint_ip=endpoint_ip,
        endpoint_mac=endpoint_mac,
        loopbacks=loopbacks,
    )

    return {
        "n": n,
        "adjacency": adjacency,
        "switch_names": switch_names,
        "switch_ports": switch_ports,
        "switch_neighbors_by_port": switch_neighbors_by_port,
        "links": links,
        "endpoint_ip": endpoint_ip,
        "endpoint_mac": endpoint_mac,
        "loopbacks": loopbacks,
        "host_definitions": host_definitions,
        "meta": meta,
        "switch_cli": switch_cli,
        "switch_roles": switch_roles,
        "profile": "mixed-fabric-netcache-node",
        "generated_netcache_db": generated_netcache_db,
        "kinda_access_by_node": kinda_access_by_node,
        "netcache_nodes": graph_netcache_nodes,
    }


def write_p4_fabric_lab(
    output_dir: Path,
    data: dict[str, Any],
    lab_name: str,
    host_image: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    switch_names: dict[int, str] = data["switch_names"]
    switch_neighbors_by_port: dict[int, dict[int, Endpoint]] = data["switch_neighbors_by_port"]
    links: list[Link] = data["links"]
    endpoint_ip: dict[tuple[str, str], ipaddress.IPv4Interface] = data["endpoint_ip"]
    endpoint_mac: dict[tuple[str, str], str] = data["endpoint_mac"]
    switch_cli: dict[str, list[str]] = data["switch_cli"]
    host_definitions: dict[str, dict[str, Any]] = data["host_definitions"]
    meta: dict[str, Any] = data["meta"]
    switch_roles: dict[int, str] = data["switch_roles"]
    adjacency: list[list[int]] = data["adjacency"]
    loopbacks: dict[int, ipaddress.IPv4Interface] = data["loopbacks"]
    switch_ports: dict[int, dict[int, int]] = data["switch_ports"]
    kinda_access_by_node: dict[int, dict[str, Any]] = data["kinda_access_by_node"]

    link_by_endpoint_pair = {frozenset((link.a.name, link.b.name)): link for link in links}

    lab_conf_lines: list[str] = [
        f'LAB_NAME="{lab_name}"',
        f'LAB_DESCRIPTION="Generated {meta["profile"]} from {Path(meta["source_file"]).name}"',
        'LAB_VERSION="1.0"',
        'LAB_AUTHOR="adj_to_kathara.py"',
        "",
    ]

    for node, sname in switch_names.items():
        switch_meta = meta["switches"][sname]
        lab_conf_lines.append(f'{sname}[image]="{switch_meta["image"]}"')
        for port, peer_ep in sorted(switch_neighbors_by_port[node].items()):
            lan = link_by_endpoint_pair[frozenset((sname, peer_ep.name))].lan
            lab_conf_lines.append(f'{sname}[{port}]="{lan}"')
        lab_conf_lines.append("")

    for host_name, host_info in host_definitions.items():
        sname = switch_names[host_info["attached_switch"]]
        lan = link_by_endpoint_pair[frozenset((host_name, sname))].lan
        lab_conf_lines.append(f'{host_name}[image]="{host_image}"')
        lab_conf_lines.append(f'{host_name}[0]="{lan}"')
        lab_conf_lines.append("")

    (output_dir / "lab.conf").write_text("\n".join(lab_conf_lines).rstrip() + "\n")

    for node, sname in switch_names.items():
        switch_meta = meta["switches"][sname]
        role = switch_roles[node]
        startup_lines = [
            "#!/bin/bash",
            "set -e",
            "exec > /tmp/switch-startup.log 2>&1",
            "ip link set lo up",
            "sysctl -w net.ipv6.conf.all.disable_ipv6=1 || true",
            "sysctl -w net.ipv6.conf.default.disable_ipv6=1 || true",
        ]
        if role == "netcache":
            startup_lines.extend(
                [
                    "if command -v iptables >/dev/null 2>&1; then",
                    "  iptables -C FORWARD -p udp --dport 50000 -j DROP 2>/dev/null || iptables -I FORWARD 1 -p udp --dport 50000 -j DROP",
                    "  iptables -C FORWARD -p udp --sport 50000 -j DROP 2>/dev/null || iptables -I FORWARD 1 -p udp --sport 50000 -j DROP",
                    "  iptables -C FORWARD -p tcp --dport 50000 -j DROP 2>/dev/null || iptables -I FORWARD 1 -p tcp --dport 50000 -j DROP",
                    "  iptables -C FORWARD -p tcp --sport 50000 -j DROP 2>/dev/null || iptables -I FORWARD 1 -p tcp --sport 50000 -j DROP",
                    "fi",
                ]
            )

        for port, peer_ep in sorted(switch_neighbors_by_port[node].items()):
            ifname = iface_name(port)
            local_mac = endpoint_mac[(sname, peer_ep.name)]
            local_ip = endpoint_ip[(sname, peer_ep.name)]
            startup_lines.append(f"ip link set {ifname} address {local_mac}")
            startup_lines.append(f"ip link set {ifname} up")
            if role == "netcache":
                startup_lines.append(f"ip addr flush dev {ifname} || true")
                if peer_ep.kind == "kinda_worker":
                    startup_lines.append(f"ip addr replace {local_ip} dev {ifname}")
                    pod_subnet = kinda_access_by_node[node]["pod_subnet"]
                    worker_ip = ipaddress.ip_interface(kinda_access_by_node[node]["worker_ip"]).ip
                    startup_lines.append(f"ip route replace {pod_subnet} via {worker_ip} dev {ifname}")
            else:
                startup_lines.append(f"ip addr replace {local_ip} dev {ifname}")
            startup_lines.append(f"sysctl -w net.ipv6.conf.{ifname}.disable_ipv6=1 || true")

        if role == "netcache":
            startup_lines.append('echo "Interfaces prepared; Ansible starts NetCache after installing its program and topology." >&2')
            cli_lines = switch_cli[sname] + [""]
            cli_dir = output_dir / sname
            cli_dir.mkdir(parents=True, exist_ok=True)
            (cli_dir / f"{sname}.cli").write_text("\n".join(cli_lines))
        else:
            startup_lines.append("sysctl -w net.ipv4.ip_forward=1")
            startup_lines.append(f"ip addr replace {loopbacks[node]} dev lo")
            next_hops = bfs_next_hops(len(switch_names), adjacency, node)
            for dst in range(len(switch_names)):
                if dst == node:
                    continue
                nh = next_hops[dst]
                ifname = iface_name(switch_ports[node][nh])
                nh_ip = endpoint_ip[(switch_names[nh], sname)].ip
                startup_lines.append(f"ip route replace {loopbacks[dst].ip}/32 via {nh_ip} dev {ifname}")

            host_route_targets = {}
            for host_name, host_info in host_definitions.items():
                attached = host_info["attached_switch"]
                host_ip = endpoint_ip[(host_name, switch_names[attached])]
                host_route_targets[host_name] = {
                    "prefix_32": f"{host_ip.ip}/32",
                    "subnet": str(host_ip.network),
                    "attached_switch": attached,
                }
            for host_name, route_info in host_route_targets.items():
                attached = route_info["attached_switch"]
                if attached == node:
                    continue
                nh = next_hops[attached]
                ifname = iface_name(switch_ports[node][nh])
                nh_ip = endpoint_ip[(switch_names[nh], sname)].ip
                for prefix in [route_info["prefix_32"], route_info["subnet"]]:
                    startup_lines.append(f"ip route replace {prefix} via {nh_ip} dev {ifname}")

            for access_node, access_info in kinda_access_by_node.items():
                prefix = access_info["pod_subnet"]
                if access_node == node:
                    worker_name = f"kinda-worker-{switch_names[access_node]}"
                    ifname = iface_name(switch_ports[node][worker_name])
                    worker_ip = ipaddress.ip_interface(access_info["worker_ip"]).ip
                    startup_lines.append(f"ip route replace {prefix} via {worker_ip} dev {ifname}")
                    continue
                nh = next_hops[access_node]
                ifname = iface_name(switch_ports[node][nh])
                nh_ip = endpoint_ip[(switch_names[nh], sname)].ip
                startup_lines.append(f"ip route replace {prefix} via {nh_ip} dev {ifname}")


        startup_lines.append("")
        startup_path = output_dir / f"{sname}.startup"
        startup_path.write_text("\n".join(startup_lines))
        startup_path.chmod(0o755)

    for host_name, host_info in host_definitions.items():
        sw = host_info["attached_switch"]
        sname = switch_names[sw]
        host_ip = endpoint_ip[(host_name, sname)]
        host_mac = endpoint_mac[(host_name, sname)]
        gw_ip = endpoint_ip[(sname, host_name)].ip
        gw_mac = endpoint_mac[(sname, host_name)]

        startup_lines = [
            "#!/bin/bash",
            "set -e",
            "exec > /tmp/host-startup.log 2>&1",
            "ip link set lo up",
            f"ip link set net0 address {host_mac}",
            "ip link set net0 up",
            f"ip addr replace {host_ip} dev net0",
            f"ip neigh replace {gw_ip} lladdr {gw_mac} dev net0 nud permanent",
            "ip route del default || true",
            "for i in $(seq 1 10); do",
            f"    ip route replace default via {gw_ip} dev net0 && break",
            "    sleep 1",
            "done",
            "ip route show",
            "",
        ]
        startup_path = output_dir / f"{host_name}.startup"
        startup_path.write_text("\n".join(startup_lines))
        startup_path.chmod(0o755)

    (output_dir / "topology.meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    if data["generated_netcache_db"] is not None:
        netcache_dir = output_dir / "generated" / "netcache"
        netcache_dir.mkdir(parents=True, exist_ok=True)
        (netcache_dir / "topology.db").write_text(json.dumps(data["generated_netcache_db"], indent=2) + "\n")


def generate_lab(
    input_file: Path,
    output_dir: Path,
    image: str,
    host_image: str,
    lab_name: str,
    p4_json: str,
    netcache_nodes: list[int],
    topology_kind: str = "generic",
    access_policy: str = "auto",
    access_nodes: list[int] | None = None,
) -> None:
    data = build_p4_fabric_data(
        input_file=input_file,
        image=image,
        host_image=host_image,
        lab_name=lab_name,
        p4_json=p4_json,
        netcache_nodes=netcache_nodes,
        topology_kind=topology_kind,
        access_policy=access_policy,
        access_nodes=access_nodes,
    )
    write_p4_fabric_lab(
        output_dir=output_dir,
        data=data,
        lab_name=lab_name,
        host_image=host_image,
    )
