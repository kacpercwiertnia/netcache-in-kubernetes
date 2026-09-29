#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT_DIR/.deps/netcache/src/p4"
[ -f "$SOURCE/core/netcache.p4" ] || { echo 'Run scripts/setup.sh first.' >&2; exit 1; }
mkdir -p "$ROOT_DIR/.generated/p4"
# The compiler is an external dependency. Override P4_BUILDER_IMAGE if needed.
docker run --rm --platform "${DOCKER_PLATFORM:-linux/amd64}" \
  -v "$SOURCE:/src:ro" -v "$ROOT_DIR/.generated/p4:/out" \
  "${P4_BUILDER_IMAGE:-kacpercwiertnia/p4-builder:dev@sha256:2248ce043e6a107d3c056f46ea1bb0fdcc0159d35f3b525ea2d28bc8f6b57dea}" \
  p4c-bm2-ss --std p4-16 --arch v1model -o /out/netcache.json /src/core/netcache.p4
[ -f "$ROOT_DIR/.generated/p4/netcache.json" ]
echo "Built $ROOT_DIR/.generated/p4/netcache.json"
