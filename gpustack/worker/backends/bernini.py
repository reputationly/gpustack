import logging
import os
from typing import Optional, List, Dict, Tuple

from gpustack.schemas.models import ModelInstanceDeploymentMetadata
from gpustack.utils.command import format_backend_parameters
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
# == container path). This is how a Bernini instance gets the RW NFS mount that
# holds the async task input media (src_video / src_ref_images, absolute NFS
# paths injected by the M4 facade) and the output files (save_result_path). Read
# from os.environ so a model deployer cannot mount arbitrary host paths into the
# privileged container — identical rationale to worker/backends/lightx2v.py.
_EXTRA_MOUNTS_ENV_KEY = "GPUSTACK_EXTRA_MOUNTS"

# The engine image bakes in the resident server (server.py) at WORKDIR /opt/bernini
# with PYTHONPATH=/opt/bernini, launched as ``python3 server.py``. server.py detects
# it is not under torchrun and re-execs itself via ``torchrun --nproc-per-node=N``
# (N = allocated GPUs from CUDA_VISIBLE_DEVICES), loading the model ONCE and serving
# all tasks from the resident process. Checkpoints dir + bound port go via env
# (BERNINI_CONFIG / BERNINI_PORT); no torchrun / host / port CLI flag is passed.
_SERVER_SCRIPT = "server.py"


class BerniniServer(InferenceServer):
    def start(self):
        try:
            self._start()
        except Exception as e:
            self._handle_error(e)

    def _start(self):
        logger.info(f"Starting Bernini model instance: {self._model_instance.name}")

        deployment_metadata = self._get_deployment_metadata()

        env = self._get_configured_env()
        # server.py reads the checkpoints dir from BERNINI_CONFIG. The model path
        # is bind-mounted host==container (base._get_configured_mounts), so the
        # same absolute path is valid inside the container. Keep the engine
        # air-gapped: never reach out to HF/ModelScope at inference time.
        env.setdefault("BERNINI_CONFIG", self._model_path)
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        env.setdefault("HF_HUB_DISABLE_XET", "1")
        # server.py binds 0.0.0.0:BERNINI_PORT. It runs under torchrun (self-relaunch)
        # so a uvicorn --port CLI flag isn't usable; pass the assigned port via env.
        # The scheduler-assigned port is what routing/health checks use, so it must
        # override any BERNINI_PORT leaked in from the worker or model env.
        port = self._get_serving_port()
        if env.get("BERNINI_PORT") not in (None, str(port)):
            logger.warning(
                "Ignoring BERNINI_PORT=%s from env; the scheduler-assigned port %d "
                "is authoritative for routing and health checks.",
                env["BERNINI_PORT"],
                port,
            )
        env["BERNINI_PORT"] = str(port)

        command = None
        if self.inference_backend:
            command = self.inference_backend.get_container_entrypoint(
                self._model.backend_version
            )

        command_script = self._get_serving_command_script(env)

        command_args, injected = self._build_command_args(
            port=port,
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
            raise ValueError("Failed to get Bernini backend image")

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

        logger.info(f"Creating Bernini container workload: {deployment_metadata.name}")
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

        logger.info(f"Created Bernini container workload: {deployment_metadata.name}")

    def _get_extra_mounts(self) -> List[ContainerMount]:
        """
        Append extra RW host bind-mounts (one ContainerMount per comma-separated
        host path; host path == container path, mirroring the model_dir bind in
        base._get_configured_mounts). This is how a Bernini instance gets its RW
        NFS mount holding the async task input media and outputs (e.g.
        GPUSTACK_EXTRA_MOUNTS=/nfs-output,/nfs-models).

        SECURITY: read from the WORKER PROCESS environment (os.environ), NOT the
        model's env — the container runs privileged, so a model deployer able to
        set this via model.env could bind-mount arbitrary host paths. The worker
        env is set by the admin who runs the worker, the correct trust boundary
        (identical rationale to worker/backends/lightx2v.py).

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
        # Launch the resident server. server.py self-relaunches under torchrun
        # (nproc = allocated GPUs) and binds 0.0.0.0:BERNINI_PORT — so GPUStack
        # passes NO torchrun / host / port CLI flags; the checkpoints dir and port
        # go via env (BERNINI_CONFIG / BERNINI_PORT). This mirrors LightX2V, whose
        # launcher likewise owns the torchrun fan-out.
        arguments = [
            "python3",
            _SERVER_SCRIPT,
        ]
        # Allow version-specific command override if configured (registry-level,
        # admin-controlled).
        arguments = self.build_versioned_command_args(
            arguments,
            model_path=self._model_path,
            port=port,
        )

        # The command target is server.py, which self-relaunches under torchrun and
        # takes no CLI surface (all knobs are BERNINI_* env vars). A user-supplied
        # engine flag would reach torchrun/server.py and abort startup, so ignore
        # user backend parameters entirely (same rationale as before).
        user_backend_parameters = self._flatten_backend_param()
        if user_backend_parameters:
            logger.warning(
                "Bernini ignores backend_parameters %s: the serving command is "
                "server.py (no engine CLI); configure the engine via model env "
                "(BERNINI_* variables) instead.",
                user_backend_parameters,
            )

        injected = self._get_injected_backend_parameters(arguments, [], entrypoint)

        return arguments, injected
