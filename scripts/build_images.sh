#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${IMAGE_PREFIX:?Set IMAGE_PREFIX to your registry namespace, e.g. docker.io/your-user}"
TAG="${IMAGE_TAG:-local}"
PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
[ -f "$ROOT_DIR/.deps/netcache/Dockerfile.client" ] || { echo 'Run scripts/setup.sh first.' >&2; exit 1; }
docker build --platform "$PLATFORM" -f "$ROOT_DIR/images/p4/runtime/Dockerfile.runtime" \
  -t "$IMAGE_PREFIX/p4-runtime:$TAG" "$ROOT_DIR/images/p4"
docker build --platform "$PLATFORM" -f "$ROOT_DIR/.deps/netcache/Dockerfile.client" \
  -t "$IMAGE_PREFIX/netcache-client:$TAG" "$ROOT_DIR/.deps/netcache"
docker build --platform "$PLATFORM" -t "$IMAGE_PREFIX/netcache-scheduler:$TAG" \
  "$ROOT_DIR/scheduler-plugins/netcache-scheduler"
echo 'Images built locally. Push them to a registry accessible to every cluster node before deployment.'
