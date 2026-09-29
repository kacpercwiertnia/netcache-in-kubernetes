# Building images

Sources: NetCache fork from `dependencies.json`, `images/p4/` and `scheduler-plugins/netcache-scheduler/`.

After `scripts/setup.sh`, build locally:

```bash
export IMAGE_PREFIX=docker.io/YOUR_USER
export IMAGE_TAG=YOUR_VERSION
./scripts/build_images.sh
```

Builds `p4-runtime`, `netcache-client` and `netcache-scheduler` for Linux/amd64. Push the images to a registry accessible from all cluster nodes.

Then configure:

```bash
export NETCACHE_IMAGE="$IMAGE_PREFIX/p4-runtime:$IMAGE_TAG"
cp config/environment.example.yml config/environment.yml
# Set kinda_netcache_scheduler_image and kinda_netcache_controller_client_image
# to the corresponding images you built and pushed.
export ENVIRONMENT_VARS="$PWD/config/environment.yml"
```

Compile P4 with `scripts/build_p4.sh`. Ansible copies the JSON to NetCache switches and starts BMv2.

## Compiler and BMv2 builder

Default base images are pinned by digest in the Dockerfiles. To build a custom compiler/BMv2 image:

```bash
docker build --platform linux/amd64 -f images/p4/builder/Dockerfile.builder \
  -t "$IMAGE_PREFIX/p4-builder:$IMAGE_TAG" images/p4/builder
```

BMv2, p4c and p4-utils source commits are pinned in the Dockerfiles. To use a custom builder for P4 compilation, set `P4_BUILDER_IMAGE`. Set the runtime Dockerfile’s builder `FROM` reference when rebuilding its runtime against that custom builder.

MicroK8s and inner containerd pull images from the registry; local Docker images are not shared.
