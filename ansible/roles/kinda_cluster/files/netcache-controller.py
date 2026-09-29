#!/usr/bin/env python3
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


API = "cache.evalnet.local"
VERSION = "v1alpha1"
NAMESPACE = os.environ.get("WATCH_NAMESPACE", "default")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))
CLIENT_IMAGE = os.environ.get("NETCACHE_CLIENT_IMAGE", "docker.io/kacpercwiertnia/netcache-client:values-f89671e@sha256:cfb21ebd6ad39a50ecd3ee49aac87039484d2d545e0391f074e3ecbdf567a859")
CLIENT_IMAGE_PULL_POLICY = os.environ.get("NETCACHE_CLIENT_IMAGE_PULL_POLICY", "Always")
REDIS_IMAGE = os.environ.get("REDIS_IMAGE", "redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499")
REDIS_STABLE_READY_POLLS = int(os.environ.get("REDIS_STABLE_READY_POLLS", "3"))
REDIS_MAX_RESTARTS = int(os.environ.get("REDIS_MAX_RESTARTS", "4"))
REDIS_MAX_UNSTABLE_POLLS = int(os.environ.get("REDIS_MAX_UNSTABLE_POLLS", "6"))
REDIS_MAX_CLIENTS = int(os.environ.get("REDIS_MAX_CLIENTS", "8"))
REDIS_MAX_FALLBACK_ATTEMPTS = int(os.environ.get("REDIS_MAX_FALLBACK_ATTEMPTS", "3"))
REDIS_KEEP_FAILED_PODS = os.environ.get("REDIS_KEEP_FAILED_PODS", "false").lower() in {"1", "true", "yes"}
MAX_RUNTIME_START_RETRIES = int(os.environ.get("MAX_RUNTIME_START_RETRIES", "3"))
NETCACHE_SCHEDULER_NAME = os.environ.get("NETCACHE_SCHEDULER_NAME", "netcache-scheduler")
NETCACHE_PLACEMENT_IMAGE = os.environ.get("NETCACHE_PLACEMENT_IMAGE", "registry.k8s.io/pause:3.10@sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a")
PLACEMENT_PENDING_TIMEOUT_SECONDS = int(os.environ.get("PLACEMENT_PENDING_TIMEOUT_SECONDS", "90"))

SMALL_POD_RESOURCES = {
    "requests": {
        "cpu": "25m",
        "memory": "32Mi",
        "ephemeral-storage": "16Mi",
    }
}
REDIS_POD_RESOURCES = {
    "requests": {
        "cpu": "50m",
        "memory": "64Mi",
        "ephemeral-storage": "32Mi",
    }
}


def _api_base():
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{host}:{port}"


def _ssl_context():
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    return ssl.create_default_context(cafile=ca_path)


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
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        if err.code == 404:
            return None
        raise RuntimeError(f"{method} {path} failed with {err.code}: {raw}") from err


def crd_path(resource):
    return f"/apis/{API}/{VERSION}/{resource}"


def namespaced_crd_path(resource, namespace=NAMESPACE):
    return f"/apis/{API}/{VERSION}/namespaces/{namespace}/{resource}"


def list_switches():
    response = request("GET", crd_path("netcacheswitches"))
    return response.get("items", []) if response else []


def list_devices():
    response = request("GET", crd_path("innetworkdevices"))
    return response.get("items", []) if response else []


def list_requests():
    response = request("GET", namespaced_crd_path("netcacheclientrequests"))
    return response.get("items", []) if response else []


def get_pod(name):
    return request("GET", f"/api/v1/namespaces/{NAMESPACE}/pods/{name}")


def get_service(name):
    return request("GET", f"/api/v1/namespaces/{NAMESPACE}/services/{name}")


def list_pods():
    response = request("GET", f"/api/v1/namespaces/{NAMESPACE}/pods")
    return response.get("items", []) if response else []


def patch_status(path, status):
    return request(
        "PATCH",
        f"{path}/status",
        {"status": status},
        content_type="application/merge-patch+json",
    )


def patch_switch_status(name, status):
    return patch_status(crd_path(f"netcacheswitches/{name}"), status)


def patch_request_status(name, status):
    return patch_status(namespaced_crd_path(f"netcacheclientrequests/{name}"), status)


def create_pod(pod):
    existing = get_pod(pod["metadata"]["name"])
    if existing is not None:
        return existing
    return request("POST", f"/api/v1/namespaces/{NAMESPACE}/pods", pod)


def create_service(service):
    existing = get_service(service["metadata"]["name"])
    if existing is not None:
        return existing
    return request("POST", f"/api/v1/namespaces/{NAMESPACE}/services", service)


def delete_pod(name):
    if not name:
        return None
    return request("DELETE", f"/api/v1/namespaces/{NAMESPACE}/pods/{name}")


def pod_runtime_start_error(pod):
    for container_status in pod.get("status", {}).get("containerStatuses", []):
        for state in (container_status.get("state", {}), container_status.get("lastState", {})):
            terminated = state.get("terminated")
            waiting = state.get("waiting")
            if terminated:
                reason = terminated.get("reason", "")
                message = terminated.get("message", "")
                exit_code = int(terminated.get("exitCode", 0))
                if reason == "StartError" or exit_code == 128:
                    return True, reason or f"Exit{exit_code}", message
                if "failed to create containerd task" in message or "cgroup" in message:
                    return True, reason or "ContainerRuntimeError", message
            if waiting:
                reason = waiting.get("reason", "")
                message = waiting.get("message", "")
                if reason in {"CreateContainerError", "RunContainerError"}:
                    return True, reason, message
                if "failed to create containerd task" in message or "cgroup" in message:
                    return True, reason or "ContainerRuntimeError", message
    return False, "", ""


def retry_runtime_start_failure(client_request, status, pod, counter_key):
    failed, reason, message = pod_runtime_start_error(pod)
    if not failed:
        return False
    if pod.get("metadata", {}).get("deletionTimestamp"):
        return True
    retries = int(status.get(counter_key, 0))
    if retries >= MAX_RUNTIME_START_RETRIES:
        return False
    delete_pod(pod.get("metadata", {}).get("name", ""))
    updated = dict(status)
    updated[counter_key] = retries + 1
    updated["lastRuntimeStartError"] = reason or "StartError"
    if message:
        updated["lastRuntimeStartErrorMessage"] = message[:512]
    patch_request_status(client_request["metadata"]["name"], updated)
    return True


def delete_service(name):
    if not name:
        return None
    return request("DELETE", f"/api/v1/namespaces/{NAMESPACE}/services/{name}")


def k8s_name(*parts):
    raw = "-".join(str(part).lower().replace("_", "-") for part in parts if str(part))
    safe = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    return safe[:63].rstrip("-")


def int_status(obj, key, default=0):
    value = obj.get("status", {}).get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def requested_entries(client_request):
    spec = client_request.get("spec", {})
    if "requestedEntries" in spec:
        return int(spec["requestedEntries"])
    if "cacheEntries" in spec:
        return max(1, len(spec["cacheEntries"]))
    keys = [key for key in str(spec.get("keys", "")).split(",") if key.strip()]
    return max(1, len(keys))


def desired_cache_entries(client_request):
    spec = client_request.get("spec", {})
    if "cacheEntries" in spec:
        return [
            {"key": str(entry["key"]), "value": str(entry["value"])}
            for entry in spec["cacheEntries"]
        ]
    return [
        {"key": key.strip(), "value": str(spec.get("value", "netcache-client-value"))}
        for key in str(spec.get("keys", "one,two,three")).split(",")
        if key.strip()
    ]


def desired_control_entries(client_request):
    entries = desired_cache_entries(client_request)
    spec = client_request.get("spec", {})
    miss_ratio = max(0.0, min(1.0, float(spec.get("cacheMissRatio", 0.0))))
    if miss_ratio <= 0.0:
        return entries
    keep = int(len(entries) * (1.0 - miss_ratio))
    return entries[:keep]


def desired_keys(client_request):
    spec = client_request.get("spec", {})
    if "cacheEntries" in spec:
        return ",".join(str(entry["key"]) for entry in spec["cacheEntries"])
    return spec.get("keys", "one,two,three")


def should_prepopulate_cache(client_request):
    return bool(client_request.get("spec", {}).get("prepopulateCache", True))


def requested_slots(client_request):
    slots = 0
    for entry in desired_cache_entries(client_request):
        value_size = len(str(entry.get("value", "")))
        slots += int((value_size + 7) / 8) if value_size > 0 else 0
    return slots


def device_for_switch(switch, devices):
    device_ref = switch.get("spec", {}).get("deviceRef")
    return devices.get(device_ref) if device_ref else None


def get_switch(name):
    return request("GET", crd_path(f"netcacheswitches/{name}"))


def switch_for_worker(worker_name):
    for switch in list_switches():
        if switch.get("spec", {}).get("attachedWorker") == worker_name:
            return switch
    return None


def any_switch_can_fit(entries, slots):
    devices = {
        device["metadata"]["name"]: device
        for device in list_devices()
    }
    for switch in list_switches():
        spec = switch.get("spec", {})
        device = device_for_switch(switch, devices)
        if (
            (device is None or device.get("status", {}).get("phase", "Ready") == "Ready")
            and int(spec.get("capacityEntries", 0)) >= entries
            and int(spec.get("capacitySlots", spec.get("capacityEntries", 0))) >= slots
        ):
            return True
    return False


def object_age_seconds(obj):
    created_at = obj.get("metadata", {}).get("creationTimestamp", "")
    if not created_at:
        return 0
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - created).total_seconds()))


def release_netcache_capacity(status):
    switch_name = status.get("switchName")
    if not switch_name or status.get("accounted") is not True:
        return status
    entries = int(status.get("allocatedEntries", 0))
    if entries <= 0:
        return status
    slots = int(status.get("allocatedSlots", entries))

    switch = get_switch(switch_name)
    if not switch:
        return status
    switch_status = dict(switch.get("status", {}))
    capacity_entries = int(switch.get("spec", {}).get("capacityEntries", 0))
    capacity_slots = int(switch.get("spec", {}).get("capacitySlots", 0))
    used_entries = max(0, int_status(switch, "usedEntries") - entries)
    free_entries = min(capacity_entries, int_status(switch, "freeEntries", capacity_entries) + entries)
    used_slots = max(0, int_status(switch, "usedSlots") - slots)
    free_slots = min(capacity_slots, int_status(switch, "freeSlots", capacity_slots) + slots)
    switch_status.update(
        {
            "phase": "Full" if free_entries == 0 else "Available",
            "usedEntries": used_entries,
            "freeEntries": free_entries,
            "usedSlots": used_slots,
            "freeSlots": free_slots,
        }
    )
    patch_switch_status(switch_name, switch_status)
    updated = dict(status)
    updated["accounted"] = False
    updated["reservationReleased"] = True
    return updated


def build_placement_pod(client_request, entries, slots):
    name = client_request["metadata"]["name"]
    pod_name = k8s_name("netcache-placement", name)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": NAMESPACE,
            "labels": {
                "cache.evalnet.local/request": name,
                "cache.evalnet.local/component": "placement",
            },
            "annotations": {
                "cache.evalnet.local/netcache-placement": "true",
                "cache.evalnet.local/request": name,
                "cache.evalnet.local/requested-entries": str(entries),
                "cache.evalnet.local/requested-slots": str(slots),
            },
        },
        "spec": {
            "schedulerName": NETCACHE_SCHEDULER_NAME,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "placement",
                    "image": NETCACHE_PLACEMENT_IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "resources": SMALL_POD_RESOURCES,
                }
            ],
        },
    }


def switch_control_url(switch):
    spec = switch.get("spec", {})
    endpoint = spec.get("controlEndpoint", {})
    if endpoint.get("url"):
        return str(endpoint["url"])
    if endpoint.get("host"):
        return f"http://{endpoint['host']}:{endpoint.get('port', 18080)}"
    switch_control_host = spec.get("accessSwitchIp", "").split("/")[0]
    if not switch_control_host:
        return ""
    return f"http://{switch_control_host}:18080"


def server_ip_for_switch(switch):
    return str(switch["spec"]["backendServerIp"])


def build_client_pod(client_request, switch, entries, control_url=None, worker_name=None):
    name = client_request["metadata"]["name"]
    spec = client_request.get("spec", {})
    pod_name = spec.get("podName", f"netcache-client-{name}")
    switch_spec = switch.get("spec", {})
    node_selector = (
        {"kubernetes.io/hostname": worker_name}
        if worker_name
        else {"topology.local/access-switch": switch_spec["switchName"]}
    )
    server_ip = server_ip_for_switch(switch)
    env = [
        {"name": "NETCACHE_SERVER_IPS", "value": server_ip},
        {"name": "NETCACHE_CLIENT_OP", "value": spec.get("operation", "read")},
        {"name": "NETCACHE_CLIENT_KEYS", "value": desired_keys(client_request)},
        {"name": "NETCACHE_CLIENT_ENTRIES", "value": json.dumps(desired_cache_entries(client_request))},
        {"name": "NETCACHE_CLIENT_VALUE", "value": str(spec.get("value", "netcache-client-value"))},
        {
            "name": "NETCACHE_PRIME_BACKEND",
            "value": str(spec.get("primeBackend", spec.get("operation", "read") == "read")).lower(),
        },
    ]
    if should_prepopulate_cache(client_request):
        env.extend(
            [
                {"name": "NETCACHE_SWITCH_CONTROL_URL", "value": control_url or switch_control_url(switch)},
                {"name": "NETCACHE_SWITCH_CONTROL_TIMEOUT_SECONDS", "value": str(spec.get("switchControlTimeoutSeconds", 60))},
                {"name": "NETCACHE_CONTROL_ENTRIES", "value": json.dumps(desired_control_entries(client_request))},
            ]
        )
    if "iterations" in spec:
        env.append({"name": "NETCACHE_CLIENT_REPEAT", "value": str(spec["iterations"])})
    if "sleepSeconds" in spec:
        env.append({"name": "NETCACHE_CLIENT_SLEEP_SECONDS", "value": str(spec["sleepSeconds"])})
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": NAMESPACE,
            "labels": {
                "cache.evalnet.local/request": name,
                "cache.evalnet.local/switch": switch_spec["switchName"],
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": node_selector,
            "containers": [
                {
                    "name": "client",
                    "image": spec.get("image", CLIENT_IMAGE),
                    "imagePullPolicy": spec.get("imagePullPolicy", CLIENT_IMAGE_PULL_POLICY),
                    "env": env,
                    "resources": SMALL_POD_RESOURCES,
                }
            ],
        },
    }

    # The existing client primes only read workloads. For cached writes, seed
    # the backend before the main container installs the switch cache entry.
    if spec.get("operation", "read") in {"put", "put-read"} and should_prepopulate_cache(client_request):
        pod["spec"]["initContainers"] = [{
            "name": "prime-backend",
            "image": spec.get("image", CLIENT_IMAGE),
            "imagePullPolicy": spec.get("imagePullPolicy", CLIENT_IMAGE_PULL_POLICY),
            "env": [
                {"name": "NETCACHE_SERVER_IPS", "value": server_ip},
                {"name": "NETCACHE_CLIENT_OP", "value": "put"},
                {"name": "NETCACHE_CLIENT_KEYS", "value": desired_keys(client_request)},
                {"name": "NETCACHE_CLIENT_ENTRIES", "value": json.dumps(desired_cache_entries(client_request))},
                {"name": "NETCACHE_CLIENT_VALUE", "value": str(spec.get("value", "netcache-client-value"))},
                {"name": "NETCACHE_CLIENT_REPEAT", "value": "1"},
                {"name": "NETCACHE_PRIME_BACKEND", "value": "false"},
            ],
            "resources": SMALL_POD_RESOURCES,
        }]
    return pod


def build_redis_server_pod(client_request, attempt=0):
    name = client_request["metadata"]["name"]
    pod_name = k8s_name("redis", name, attempt)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": NAMESPACE,
            "labels": {
                "cache.evalnet.local/request": name,
                "cache.evalnet.local/fallback": "redis",
                "cache.evalnet.local/component": "redis",
                "cache.evalnet.local/redis-instance": pod_name,
            },
        },
        "spec": {
            "restartPolicy": "Always",
            "containers": [
                {
                    "name": "redis",
                    "image": REDIS_IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["redis-server"],
                    "args": ["--save", "", "--appendonly", "no", "--protected-mode", "no"],
                    "ports": [{"containerPort": 6379, "name": "redis"}],
                    "resources": REDIS_POD_RESOURCES,
                }
            ],
        },
    }


def build_redis_service(client_request, redis_pod):
    name = client_request["metadata"]["name"]
    service_name = redis_pod["metadata"]["name"]
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": service_name,
            "namespace": NAMESPACE,
            "labels": {
                "cache.evalnet.local/request": name,
                "cache.evalnet.local/fallback": "redis",
                "cache.evalnet.local/component": "redis",
                "cache.evalnet.local/redis-instance": service_name,
            },
        },
        "spec": {
            "selector": {
                "cache.evalnet.local/fallback": "redis",
                "cache.evalnet.local/component": "redis",
                "cache.evalnet.local/redis-instance": service_name,
            },
            "ports": [
                {
                    "name": "redis",
                    "port": 6379,
                    "targetPort": 6379,
                }
            ],
        },
    }


def service_host(service_name):
    service = get_service(service_name)
    if not service:
        return service_name
    return service.get("spec", {}).get("clusterIP") or service_name


def build_redis_client_pod(client_request, redis_service_name, redis_host):
    name = client_request["metadata"]["name"]
    spec = client_request.get("spec", {})
    env = [
        {"name": "NETCACHE_CLIENT_BACKEND", "value": "redis"},
        {"name": "REDIS_HOST", "value": redis_host},
        {"name": "REDIS_PORT", "value": "6379"},
        {"name": "REDIS_RETRIES", "value": "60"},
        {"name": "NETCACHE_CLIENT_OP", "value": spec.get("operation", "put-read")},
        {"name": "NETCACHE_CLIENT_KEYS", "value": desired_keys(client_request)},
        {"name": "NETCACHE_CLIENT_ENTRIES", "value": json.dumps(desired_cache_entries(client_request))},
        {"name": "NETCACHE_CLIENT_VALUE", "value": str(spec.get("value", "netcache-client-value"))},
    ]
    if "iterations" in spec:
        env.append({"name": "NETCACHE_CLIENT_REPEAT", "value": str(spec["iterations"])})
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"redis-client-{name}",
            "namespace": NAMESPACE,
            "labels": {
                "cache.evalnet.local/request": name,
                "cache.evalnet.local/fallback": "redis",
                "cache.evalnet.local/component": "client",
                "cache.evalnet.local/redis-service": redis_service_name,
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "affinity": {
                "podAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {
                                "matchLabels": {
                                    "cache.evalnet.local/fallback": "redis",
                                    "cache.evalnet.local/component": "redis",
                                    "cache.evalnet.local/redis-instance": redis_service_name,
                                }
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            },
            "containers": [
                {
                    "name": "client",
                    "image": spec.get("image", CLIENT_IMAGE),
                    "imagePullPolicy": spec.get("imagePullPolicy", CLIENT_IMAGE_PULL_POLICY),
                    "env": env,
                    "resources": SMALL_POD_RESOURCES,
                }
            ],
        },
    }


def redis_ready_state(redis_pod):
    if redis_pod.get("status", {}).get("phase") != "Running":
        return False, "", 0
    container_status = redis_container_status(redis_pod)
    redis_ready = container_status.get("ready") is True
    redis_host = redis_pod.get("status", {}).get("podIP", "")
    redis_restarts = int(container_status.get("restartCount", 0)) if container_status else 0
    return redis_ready and bool(redis_host), redis_host, redis_restarts


def active_redis_client_count(redis_service_name):
    count = 0
    for pod in list_pods():
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("cache.evalnet.local/fallback") != "redis":
            continue
        if labels.get("cache.evalnet.local/component") != "client":
            continue
        if labels.get("cache.evalnet.local/redis-service") != redis_service_name:
            continue
        if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        count += 1
    return count


def reusable_redis_instance():
    candidates = []
    for pod in list_pods():
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("cache.evalnet.local/fallback") != "redis":
            continue
        if labels.get("cache.evalnet.local/component") != "redis":
            continue
        ready, redis_host, redis_restarts = redis_ready_state(pod)
        if not ready or redis_restarts >= REDIS_MAX_RESTARTS:
            continue
        service_name = pod["metadata"]["name"]
        service = get_service(service_name)
        if service is None:
            continue
        redis_service_host = service.get("spec", {}).get("clusterIP") or service_name
        active_clients = active_redis_client_count(service_name)
        if active_clients >= REDIS_MAX_CLIENTS:
            continue
        candidates.append((active_clients, pod["metadata"].get("creationTimestamp", ""), service_name, redis_service_host, redis_host, redis_restarts))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    active_clients, _, service_name, redis_service_host, redis_host, redis_restarts = candidates[0]
    return {
        "serviceName": service_name,
        "serviceHost": redis_service_host,
        "podName": service_name,
        "podIp": redis_host,
        "restartCount": redis_restarts,
        "activeClients": active_clients,
    }


def attach_redis_client(client_request, entries, redis_instance, reused):
    name = client_request["metadata"]["name"]
    redis_service_host = redis_instance.get("podIp") or redis_instance.get("serviceHost") or service_host(redis_instance["serviceName"])
    client_pod = build_redis_client_pod(client_request, redis_instance["serviceName"], redis_service_host)
    create_pod(client_pod)
    patch_request_status(
        name,
        {
            "phase": "Scheduled",
            "cacheMode": "redis",
            "reason": "RedisReused" if reused else "RedisReady",
            "requestedEntries": entries,
            "allocatedEntries": 0,
            "podName": client_pod["metadata"]["name"],
            "redisPodName": redis_instance["podName"],
            "redisServiceName": redis_instance["serviceName"],
            "redisServiceIp": redis_service_host,
            "redisPodIp": redis_instance.get("podIp", ""),
            "redisRestartCount": redis_instance.get("restartCount", 0),
            "redisActiveClients": redis_instance.get("activeClients", 0) + 1,
            "redisMaxClients": REDIS_MAX_CLIENTS,
        },
    )


def schedule_redis_fallback(client_request, entries, attempt=0, failed_instances=None):
    name = client_request["metadata"]["name"]
    redis_instance = reusable_redis_instance()
    if redis_instance is not None:
        attach_redis_client(client_request, entries, redis_instance, reused=True)
        return
    if attempt >= REDIS_MAX_FALLBACK_ATTEMPTS:
        patch_request_status(
            name,
            {
                "phase": "Failed",
                "cacheMode": "redis",
                "reason": "RedisFallbackExhausted",
                "requestedEntries": entries,
                "allocatedEntries": 0,
                "redisFallbackAttempt": attempt,
                "redisFailedInstances": ",".join(failed_instances or []),
            },
        )
        return

    redis_pod = build_redis_server_pod(client_request, attempt)
    redis_service = build_redis_service(client_request, redis_pod)
    create_pod(redis_pod)
    create_service(redis_service)
    patch_request_status(
        name,
        {
            "phase": "RedisStarting",
            "cacheMode": "redis",
            "requestedEntries": entries,
            "allocatedEntries": 0,
            "redisPodName": redis_pod["metadata"]["name"],
            "redisServiceName": redis_service["metadata"]["name"],
            "redisFallbackAttempt": attempt,
            "redisFailedInstances": ",".join(failed_instances or []),
            "redisReadyPolls": 0,
            "redisUnstablePolls": 0,
            "redisRestartCount": 0,
            "redisPodIp": "",
            "redisActiveClients": 0,
            "redisMaxClients": REDIS_MAX_CLIENTS,
        },
    )


def retry_redis_fallback(client_request, status, reason):
    entries = int(status.get("requestedEntries", requested_entries(client_request)))
    failed_instances = [
        instance for instance in str(status.get("redisFailedInstances", "")).split(",") if instance
    ]
    current_instance = status.get("redisPodName", "")
    if current_instance and current_instance not in failed_instances:
        failed_instances.append(current_instance)
    next_attempt = int(status.get("redisFallbackAttempt", 0)) + 1
    updated = dict(status)
    updated["phase"] = "RedisRetrying"
    updated["reason"] = reason
    updated["redisFailedInstances"] = ",".join(failed_instances)
    patch_request_status(client_request["metadata"]["name"], updated)
    if not REDIS_KEEP_FAILED_PODS:
        delete_pod(status.get("redisPodName", ""))
        delete_service(status.get("redisServiceName", ""))
    schedule_redis_fallback(client_request, entries, attempt=next_attempt, failed_instances=failed_instances)


def redis_container_status(redis_pod):
    for container_status in redis_pod.get("status", {}).get("containerStatuses", []):
        if container_status.get("name") == "redis":
            return container_status
    return {}


def sync_redis_starting(client_request):
    status = client_request.get("status", {})
    redis_pod_name = status.get("redisPodName")
    if not redis_pod_name:
        return
    redis_pod = get_pod(redis_pod_name)
    if redis_pod is None:
        return
    phase = redis_pod.get("status", {}).get("phase")
    if phase == "Failed":
        retry_redis_fallback(client_request, status, "RedisPodFailed")
        return
    container_status = redis_container_status(redis_pod)
    waiting_reason = container_status.get("state", {}).get("waiting", {}).get("reason", "")
    redis_restarts = int(container_status.get("restartCount", 0)) if container_status else 0
    if waiting_reason in {"CrashLoopBackOff", "RunContainerError", "CreateContainerError"}:
        unstable_polls = int(status.get("redisUnstablePolls", 0)) + 1
        updated = dict(status)
        updated["redisRestartCount"] = redis_restarts
        updated["redisUnstablePolls"] = unstable_polls
        updated["redisLastReason"] = waiting_reason
        patch_request_status(client_request["metadata"]["name"], updated)
        if unstable_polls >= REDIS_MAX_UNSTABLE_POLLS or redis_restarts >= REDIS_MAX_RESTARTS:
            retry_redis_fallback(client_request, updated, f"RedisUnstable:{waiting_reason}")
        return
    if phase != "Running":
        return
    redis_ready, redis_host, redis_restarts = redis_ready_state(redis_pod)
    if not redis_ready:
        unstable_polls = int(status.get("redisUnstablePolls", 0)) + 1
        updated = dict(status)
        updated["redisRestartCount"] = redis_restarts
        updated["redisUnstablePolls"] = unstable_polls
        updated["redisLastReason"] = waiting_reason or "RedisNotReady"
        patch_request_status(client_request["metadata"]["name"], updated)
        if unstable_polls >= REDIS_MAX_UNSTABLE_POLLS:
            retry_redis_fallback(client_request, updated, "RedisNotReady")
        return
    if redis_restarts >= REDIS_MAX_RESTARTS:
        updated = dict(status)
        updated["redisRestartCount"] = redis_restarts
        updated["redisLastReason"] = "RedisRestartLimit"
        patch_request_status(client_request["metadata"]["name"], updated)
        retry_redis_fallback(client_request, updated, "RedisRestartLimit")
        return
    observed_ip = status.get("redisPodIp", "")
    observed_restarts = int(status.get("redisRestartCount", -1))
    ready_polls = int(status.get("redisReadyPolls", 0))
    if observed_ip == redis_host and observed_restarts == redis_restarts:
        ready_polls += 1
    else:
        ready_polls = 1
    if ready_polls < REDIS_STABLE_READY_POLLS:
        updated = dict(status)
        updated["redisPodIp"] = redis_host
        updated["redisRestartCount"] = redis_restarts
        updated["redisReadyPolls"] = ready_polls
        updated["redisUnstablePolls"] = 0
        updated["redisLastReason"] = "RedisReady"
        patch_request_status(client_request["metadata"]["name"], updated)
        return
    redis_instance = {
        "serviceName": status.get("redisServiceName", status.get("redisPodName", "")),
        "serviceHost": service_host(status.get("redisServiceName", status.get("redisPodName", ""))),
        "podName": status.get("redisPodName", ""),
        "podIp": redis_host,
        "restartCount": redis_restarts,
        "activeClients": active_redis_client_count(status.get("redisServiceName", "")),
    }
    attach_redis_client(client_request, int(status.get("requestedEntries", requested_entries(client_request))), redis_instance, reused=False)


def schedule_netcache_placement(client_request, entries, slots):
    name = client_request["metadata"]["name"]
    if not any_switch_can_fit(entries, slots):
        schedule_redis_fallback(client_request, entries)
        return

    placement_pod = build_placement_pod(client_request, entries, slots)
    create_pod(placement_pod)
    patch_request_status(
        name,
        {
            "phase": "PlacementPending",
            "cacheMode": "netcache",
            "requestedEntries": entries,
            "allocatedEntries": 0,
            "allocatedSlots": slots,
            "placementPodName": placement_pod["metadata"]["name"],
            "schedulerName": NETCACHE_SCHEDULER_NAME,
        },
    )


def allocate(client_request):
    entries = requested_entries(client_request)
    slots = requested_slots(client_request)
    schedule_netcache_placement(client_request, entries, slots)


def account_netcache_success(client_request, status):
    switch_name = status.get("switchName")
    if not switch_name or status.get("requestAccounted") is True:
        return status

    switch = get_switch(switch_name)
    if not switch:
        return status
    switch_status = dict(switch.get("status", {}))
    total = int_status(switch, "totalCacheRequests") + 1
    served = int_status(switch, "servedByNetcacheRequests") + 1
    switch_status.update(
        {
            "totalCacheRequests": total,
            "servedByNetcacheRequests": served,
            "p4Coverage": served / total if total else 0,
        }
    )
    patch_switch_status(switch_name, switch_status)
    updated = dict(status)
    updated["requestAccounted"] = True
    return updated


def sync_placement_phase(client_request):
    status = client_request.get("status", {})
    placement_pod_name = status.get("placementPodName")
    if not placement_pod_name:
        return

    placement_pod = get_pod(placement_pod_name)
    if placement_pod is None:
        entries = int(status.get("requestedEntries", requested_entries(client_request)))
        slots = int(status.get("allocatedSlots", requested_slots(client_request)))
        create_pod(build_placement_pod(client_request, entries, slots))
        return

    if placement_pod.get("status", {}).get("phase") == "Failed":
        if retry_runtime_start_failure(client_request, status, placement_pod, "placementRuntimeRetries"):
            return
        updated = dict(status)
        updated["phase"] = "Failed"
        updated["reason"] = "PlacementPodFailed"
        patch_request_status(client_request["metadata"]["name"], updated)
        return

    node_name = placement_pod.get("spec", {}).get("nodeName", "")
    if not node_name:
        if object_age_seconds(placement_pod) >= PLACEMENT_PENDING_TIMEOUT_SECONDS:
            entries = int(status.get("requestedEntries", requested_entries(client_request)))
            delete_pod(placement_pod_name)
            schedule_redis_fallback(client_request, entries)
        return

    annotations = placement_pod.get("metadata", {}).get("annotations", {})
    switch_name = annotations.get("cache.evalnet.local/selected-switch", "")
    selected_control_url = annotations.get("cache.evalnet.local/selected-switch-control-url", "")
    switch = get_switch(switch_name) if switch_name else switch_for_worker(node_name)
    if not switch:
        updated = dict(status)
        updated["phase"] = "Failed"
        updated["reason"] = "ScheduledWorkerHasNoNetCacheSwitch"
        updated["attachedWorker"] = node_name
        patch_request_status(client_request["metadata"]["name"], updated)
        return

    if not switch.get("spec", {}).get("backendServerIp"):
        patch_request_status(client_request["metadata"]["name"], {"phase": "Failed", "reason": "MissingSwitchBackendServerIp"})
        delete_pod(placement_pod_name)
        return

    entries = int(status.get("requestedEntries", requested_entries(client_request)))
    slots = int(status.get("allocatedSlots", requested_slots(client_request)))
    client_pod = build_client_pod(client_request, switch, entries, selected_control_url or None, node_name)
    create_pod(client_pod)
    delete_pod(placement_pod_name)

    switch_name = switch["metadata"]["name"]
    updated = dict(status)
    updated.update(
        {
            "phase": "Scheduled",
            "cacheMode": "netcache",
            "switchName": switch.get("spec", {}).get("switchName", switch_name),
            "attachedWorker": node_name,
            "selectedWorker": node_name,
            "selectedSwitchControlUrl": selected_control_url or switch_control_url(switch),
            "selectedBackendServerIp": server_ip_for_switch(switch),
            "selectedBackendServerName": switch.get("spec", {}).get("backendServerName", ""),
            "podName": client_pod["metadata"]["name"],
            "allocatedEntries": entries,
            "allocatedSlots": slots,
            "selectedHopCount": int(annotations.get("cache.evalnet.local/selected-hop-count", "0")),
            "selectedPathCost": int(annotations.get("cache.evalnet.local/selected-path-cost", "0")),
            "accounted": True,
            "reservationId": f"{client_request['metadata'].get('uid', client_request['metadata']['name'])}:{switch_name}:{entries}:{slots}:scheduler",
        }
    )
    patch_request_status(client_request["metadata"]["name"], updated)


def sync_pod_phase(client_request):
    status = client_request.get("status", {})
    pod_name = status.get("podName")
    if not pod_name:
        return
    pod = get_pod(pod_name)
    if pod is None:
        if status.get("phase") == "Scheduled" and status.get("cacheMode") == "netcache":
            switch_name = status.get("switchName")
            switch = get_switch(switch_name) if switch_name else None
            if switch:
                create_pod(
                    build_client_pod(
                        client_request,
                        switch,
                        int(status.get("allocatedEntries", 0)),
                        status.get("selectedSwitchControlUrl"),
                        status.get("attachedWorker"),
                    )
                )
        return
    if status.get("cacheMode") == "redis":
        for container_status in pod.get("status", {}).get("containerStatuses", []):
            if container_status.get("name") != "client":
                continue
            terminated = container_status.get("state", {}).get("terminated")
            if not terminated:
                return
            exit_code = int(terminated.get("exitCode", 1))
            updated = dict(status)
            updated["phase"] = "Succeeded" if exit_code == 0 else "Failed"
            updated["fallbackToRedisRequests"] = 1
            patch_request_status(client_request["metadata"]["name"], updated)
            return
    phase = pod.get("status", {}).get("phase")
    if phase in {"Succeeded", "Failed"} and status.get("phase") != phase:
        updated = dict(status)
        updated["phase"] = phase
        if phase == "Succeeded" and status.get("cacheMode") == "netcache":
            updated = account_netcache_success(client_request, updated)
        elif phase == "Failed" and status.get("cacheMode") == "netcache":
            if retry_runtime_start_failure(client_request, status, pod, "clientRuntimeRetries"):
                return
            updated = release_netcache_capacity(updated)
            updated["reason"] = "ClientPodFailed"
        patch_request_status(client_request["metadata"]["name"], updated)


def reconcile_once():
    for client_request in list_requests():
        phase = client_request.get("status", {}).get("phase", "")
        if phase in {"", "Pending"}:
            allocate(client_request)
        elif phase == "PlacementPending":
            sync_placement_phase(client_request)
        elif phase == "RedisStarting":
            sync_redis_starting(client_request)
        elif phase == "Scheduled":
            sync_pod_phase(client_request)


def main():
    print("[netcache-controller] started", flush=True)
    while True:
        try:
            reconcile_once()
        except Exception as exc:
            print(f"[netcache-controller] reconcile failed: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
