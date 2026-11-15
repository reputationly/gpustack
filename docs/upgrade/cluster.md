# Upgrade a Cluster Deployment

GPUStack organizes worker nodes into [clusters](../user-guide/cluster-management.md). This page covers upgrading the worker deployment of a cluster after the GPUStack server has been upgraded.

!!! note

    1. Upgrade the GPUStack **server** first (see [Upgrade via Docker](docker.md) or [Upgrade via Helm](helm.md)), then upgrade the cluster deployments.

    2. Upgrade a worker to the **same version** as the server. Do not mix a `main`(dev) or release candidate(rc) worker image with a stable server.

!!! warning "Compare the build, not the `/version` string"

    `/version` returns the version string compiled into the Python package, so a **fork build
    reports the upstream release it was based on** (e.g. `v2.2.3` / `1cfcbbf`) even though the
    image is a local rebuild. Concluding "the worker is older, so I may swap in the upstream
    image" from that string is wrong and can take the cluster down.

    Read the identity of both sides from the image labels instead:

    ```bash
    # server (on the server host)
    docker inspect gpustack-server --format \
      '{{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
    # one worker
    docker inspect gpustack-worker --format \
      '{{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
    ```

    The two must name the **same build** (same `version`/`revision`). If they do, they already
    match and there is nothing to upgrade — even when `/version` shows a stable release.

    Replacing a fork worker with the upstream image of the same release mixes backend metadata:
    the worker reports a different set of supported frameworks, the scheduler filters every
    worker out, and every model instance turns `pending` with

    ```
    Worker <name> does not support backend version <v> or the backend version not exists.
    ```

    (2026-11-15 incident on this deployment: server and workers were both
    `lx2v-20260924-0656-450e453f`; swapping the workers to upstream `v2.2.3` broke scheduling for
    all 67 replicas until the workers were rolled back.)

    Rollback is cheap when the previous container was kept: rename it back and start it, e.g.
    `docker rm -f gpustack-worker-new && docker rename gpustack-worker gpustack-worker-new &&
    docker rename gpustack-worker-prev gpustack-worker && docker start gpustack-worker`.

    To actually move to a newer base, upgrade the **fork** — rebuild both the server and the
    worker images from the rebased branch, then canary a single worker and confirm model
    instances schedule before rolling the remaining nodes.

The upgrade procedure mirrors how the cluster was originally deployed (see [Cluster Management](../user-guide/cluster-management.md)): re-run the deployment for a Docker cluster, or re-apply the manifests for a Kubernetes cluster, using the new image.

## Docker Cluster

Workers in a Docker cluster run as containers started by the `docker run` command from the cluster's `Add Worker` step. Upgrade each worker by pulling the new image, removing the old container, and recreating it with the **same arguments and volumes** as before:

```bash
docker pull gpustack/gpustack:vx.y.z   # match the server version

docker stop gpustack-worker
docker rm gpustack-worker

docker run -d --name gpustack-worker \
  ... \
  gpustack/gpustack:vx.y.z \
  --server-url http://your_gpustack_server_url \
  --token your_worker_token \
  --advertise-address your_worker_ip
```

!!! tip

    You can obtain an up-to-date `docker run` command for the target version from the GPUStack UI: on the `Clusters` page, open the cluster's `Add Worker` step and copy the generated `Run command`.

## Kubernetes Cluster

Workers in a Kubernetes cluster are deployed as DaemonSets, managed by the GPUStack Operator, from the manifests applied during cluster registration. To upgrade, re-apply the manifests generated for the new version:

1. Go to the `Clusters` page and find the Kubernetes cluster you want to upgrade.
2. Click the ellipsis button in the operations column, then select `Register Cluster`.
3. Follow the steps to the `Run command` step and copy the generated command.
4. Run the command in an environment where `kubectl` is installed and `kubeconfig` is configured for the target cluster. This re-applies the updated manifests, and the Operator rolls the worker DaemonSets to the new version.

Once the worker pods reach `Ready`, the cluster is running the new version.

## Cloud Provider Cluster

For cloud-provisioned clusters (e.g. DigitalOcean), each worker node is bootstrapped by cloud-init, which writes the worker config to `/var/lib/gpustack/config.yaml` and starts a `gpustack-worker` container via `/opt/gpustack-run-worker.sh`. To upgrade an existing node, SSH into it and recreate the container with the new image, reusing the same config and volumes:

```bash
# SSH into the worker node, then:
NEW_IMAGE=gpustack/gpustack:vx.y.z   # match the server version

docker pull "$NEW_IMAGE"

docker rm -f gpustack-worker

docker run -d --name gpustack-worker \
  -e "GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME=gpustack-worker" \
  --restart=unless-stopped \
  --privileged \
  --network=host \
  -v /var/lib/gpustack:/var/lib/gpustack \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "$NEW_IMAGE" \
  --config-file=/var/lib/gpustack/config.yaml
```

!!! note

    So that **newly provisioned** nodes (e.g. from scaling up a worker pool) come up on the new version too, also update the cluster's image configuration in the UI. See [Cluster Management](../user-guide/cluster-management.md#operating-worker-pools).
