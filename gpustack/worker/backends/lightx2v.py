import logging
import os
from typing import Optional, List, Dict, Tuple

from gpustack.schemas.models import ModelInstanceDeploymentMetadata
from gpustack.utils.command import extend_args_no_exist, format_backend_parameters
from gpustack.utils.envs import sanitize_env
from gpustack.worker.backends.base import InferenceServer

from gpustack_runtime import envs as runtime_envs
from gpustack_runtime.deployer import (
    Container,
    ContainerEnv,
    ContainerExecution,
    ContainerMount,
    ContainerProfileEnum,
    WorkloadPlan,
    create_workload,
    ContainerRestartPolicyEnum,
)

logger = logging.getLogger(__name__)

# WORKER-PROCESS env key (admin-set, NOT model env) holding a comma-separated
# list of extra host paths to bind-mount into the inference container (host path
# == container path). Generic hook to give LightX2V instances the RW fast-disk
# NFS output mount (e.g. "/nfs-rw"). Read from os.environ so a model deployer
# cannot mount arbitrary host paths into the privileged container — see
# _get_extra_mounts. Carries no LightX2V-specific logic (small rebase surface).
_EXTRA_MOUNTS_ENV_KEY = "GPUSTACK_EXTRA_MOUNTS"

# The launcher binary baked into the engine image. It owns ALL engine logic:
# it reads the container's visible GPUs (NVIDIA_VISIBLE_DEVICES, injected by
# GPUStack), selects the profile from the image-embedded YAML, spawns
# ``python -m lightx2v.server`` (N==1) or ``torchrun --nproc_per_node=N ...``
# (N>1), and occupies {{port}} as a front proxy that self-answers ``/ready``
# (503 before warmup, 200 after) while forwarding everything else to the
# internal lightx2v.server. GPUStack never constructs torchrun or touches the
# profile, so calibrating a profile only means swapping the engine image.
_LAUNCHER_BIN = "gpustack-lx2v-launcher"


class LightX2VServer(InferenceServer):
    def start(self):
        try:
            self._start()
        except Exception as e:
            self._handle_error(e)

    def _start(self):
        logger.info(f"Starting LightX2V model instance: {self._model_instance.name}")

        deployment_metadata = self._get_deployment_metadata()

        env = self._get_configured_env()

        command = None
        if self.inference_backend:
            command = self.inference_backend.get_container_entrypoint(
                self._model.backend_version
            )

        command_script = self._get_serving_command_script(env)

        command_args, injected = self._build_command_args(
            port=self._get_serving_port(),
            entrypoint=command,
        )

        try:
            self._update_model_instance(
                self._model_instance.id,
                injected_backend_parameters=format_backend_parameters(injected) or None,
            )
        except Exception as e:
            logger.warning(
                f"Failed to persist injected backend parameters for {self._model_instance.name}: {e}"
            )

        self._create_workload(
            deployment_metadata=deployment_metadata,
            command=command,
            command_script=command_script,
            command_args=command_args,
            env=env,
        )

    def _create_workload(
        self,
        deployment_metadata: ModelInstanceDeploymentMetadata,
        command: Optional[List[str]],
        command_script: Optional[str],
        command_args: List[str],
        env: Dict[str, str],
    ):
        image = self._get_configured_image()
        if not image:
            raise ValueError("Failed to get LightX2V backend image")

        command, command_args = self._override_entrypoint(
            command, command_args, command_script
        )

        resources = self._get_configured_resources()

        mounts = self._get_configured_mounts()
        mounts.extend(self._get_extra_mounts())

        ports = self._get_configured_ports()

        # Read container config from environment variables
        container_config = self._get_container_env_config(env)

        run_container = Container(
            image=image,
            name="default",
            profile=ContainerProfileEnum.RUN,
            restart_policy=ContainerRestartPolicyEnum.NEVER,
            execution=ContainerExecution(
                privileged=True,
                command=command,
                command_script=command_script,
                args=command_args,
                run_as_user=container_config.user,
                run_as_group=container_config.group,
            ),
            envs=[
                ContainerEnv(
                    name=name,
                    value=value,
                )
                for name, value in env.items()
            ],
            resources=resources,
            mounts=mounts,
            ports=ports,
        )

        logger.info(f"Creating LightX2V container workload: {deployment_metadata.name}")
        logger.info(
            f"With image: {image}, "
            f"command: [{' '.join(command) if command else ''}], "
            f"arguments: [{' '.join(command_args)}], "
            f"ports: [{','.join([str(port.internal) for port in ports])}], "
            f"mounts: [{','.join([m.path for m in mounts])}], "
            f"envs(inconsistent input items mean unchangeable):{os.linesep}"
            f"{os.linesep.join(f'{k}={v}' for k, v in sorted(sanitize_env(env).items()))}"
        )

        workload_plan = WorkloadPlan(
            name=deployment_metadata.name,
            host_network=True,
            shm_size=int(container_config.shm_size_gib * (1 << 30)),
            containers=[run_container],
            run_as_user=container_config.user,
            run_as_group=container_config.group,
        )
        create_workload(self._transform_workload_plan(workload_plan))

        logger.info(f"Created LightX2V container workload: {deployment_metadata.name}")

    def _get_extra_mounts(self) -> List[ContainerMount]:
        """
        Append extra RW host bind-mounts (one ContainerMount per comma-separated
        host path; host path == container path, mirroring the model_dir bind in
        base._get_configured_mounts). This is how a LightX2V instance gets its RW
        fast-disk NFS output mount (e.g. GPUSTACK_EXTRA_MOUNTS=/nfs-rw).

        SECURITY: read from the WORKER PROCESS environment (os.environ), NOT the
        model's env. The container runs privileged, so a model deployer who could
        set this via model.env would be able to bind-mount arbitrary host paths
        (e.g. /, /etc) into a privileged container. The worker env is set by the
        admin who runs the worker (e.g. `docker run -e GPUSTACK_EXTRA_MOUNTS=...`),
        which is the correct trust boundary for a host mount.

        Mirrored runtime deployments own their mounts, so skip in that case to
        match base behavior.
        """
        if runtime_envs.GPUSTACK_RUNTIME_DEPLOY_MIRRORED_DEPLOYMENT:
            return []

        raw = os.environ.get(_EXTRA_MOUNTS_ENV_KEY, "")
        mounts: List[ContainerMount] = []
        seen = set()
        for path in raw.split(","):
            path = path.strip()
            if not path or path in seen:
                continue
            seen.add(path)
            mounts.append(ContainerMount(path=path))
        return mounts

    def _build_command_args(
        self, port: int, entrypoint: Optional[List[str]] = None
    ) -> Tuple[List[str], List[str]]:
        # Invoke the image-embedded launcher; it self-discovers the assigned
        # GPUs and picks the profile / parallelism internally. GPUStack only
        # injects the model path, port and host — no torchrun, no profile here.
        arguments = [
            _LAUNCHER_BIN,
            "--model",
            self._model_path,
        ]
        # Allow version-specific command override if configured (before appending extra args)
        arguments = self.build_versioned_command_args(
            arguments,
            model_path=self._model_path,
            port=port,
        )

        user_backend_parameters = self._flatten_backend_param()
        arguments.extend(user_backend_parameters)
        # Append immutable arguments to ensure the launcher binds the assigned
        # port and host. Only add if not already present in arguments.
        extend_args_no_exist(
            arguments, ("--host", self._worker.ip), ("--port", str(port))
        )

        injected = self._get_injected_backend_parameters(
            arguments, user_backend_parameters, entrypoint
        )

        return arguments, injected
