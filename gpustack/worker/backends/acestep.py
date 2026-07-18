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
# == container path). This is how an ACE-Step instance gets the RW fast-disk NFS
# mount that holds the async task output files (save_result_path) and the
# cover/repaint reference audio, both absolute NFS paths injected by the facade.
# Read from os.environ so a model deployer cannot mount arbitrary host paths into
# the privileged container — see _get_extra_mounts and the identical rationale in
# worker/backends/{indextts,lightx2v}.py.
_EXTRA_MOUNTS_ENV_KEY = "GPUSTACK_EXTRA_MOUNTS"

# The engine image bakes in the FastAPI app at WORKDIR /app with PYTHONPATH=/app,
# so ``uvicorn acestep.api_server:app`` resolves the module. GPUStack injects only
# the bound host/port; the checkpoints dir is passed via ACESTEP_CHECKPOINTS_DIR
# (the model_path, bind-mounted host==container by base._get_configured_mounts).
_SERVER_APP = "acestep.api_server:app"


class ACEStepServer(InferenceServer):
    def start(self):
        try:
            self._start()
        except Exception as e:
            self._handle_error(e)

    def _start(self):
        logger.info(f"Starting ACE-Step model instance: {self._model_instance.name}")

        deployment_metadata = self._get_deployment_metadata()

        env = self._get_configured_env()
        # The API server reads the checkpoints root from ACESTEP_CHECKPOINTS_DIR
        # and selects the DiT/LM variant within it via ACESTEP_CONFIG_PATH /
        # ACESTEP_LM_MODEL_PATH. The model path is bind-mounted host==container
        # (base._get_configured_mounts), so the same absolute path is valid inside
        # the container. setdefault lets a deployer override the variant via the
        # model env while keeping a sane production default (xl-turbo + 4B LM).
        env.setdefault("ACESTEP_CHECKPOINTS_DIR", self._model_path)
        env.setdefault("ACESTEP_CONFIG_PATH", "acestep-v15-xl-turbo")
        env.setdefault("ACESTEP_LM_MODEL_PATH", "acestep-5Hz-lm-4B")
        # Eager model init at startup so /ready reflects true readiness. The
        # default is lazy-load, which would leave /ready at 503 forever (nothing
        # triggers a load before a task) and deadlock the facade health check.
        # ACESTEP_INIT_SERVICE is the image entrypoint's flag and is inert here
        # because we launch uvicorn directly.
        env.setdefault("ACESTEP_NO_INIT", "false")
        # 5Hz LM backend: pt (plain PyTorch) is the safe choice on arm A100.
        env.setdefault("ACESTEP_LLM_BACKEND", "pt")
        # Keep the engine air-gapped: never reach out to HF/ModelScope at runtime.
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        env.setdefault("HF_HUB_DISABLE_XET", "1")

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
            raise ValueError("Failed to get ACE-Step backend image")

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

        logger.info(f"Creating ACE-Step container workload: {deployment_metadata.name}")
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

        logger.info(f"Created ACE-Step container workload: {deployment_metadata.name}")

    def _get_extra_mounts(self) -> List[ContainerMount]:
        """
        Append extra RW host bind-mounts (one ContainerMount per comma-separated
        host path; host path == container path, mirroring the model_dir bind in
        base._get_configured_mounts). This is how an ACE-Step instance gets its
        RW fast-disk NFS mount holding the async task outputs and cover/repaint
        reference audio (e.g. GPUSTACK_EXTRA_MOUNTS=/nfs-rw).

        SECURITY: read from the WORKER PROCESS environment (os.environ), NOT the
        model's env — the container runs privileged, so a model deployer able to
        set this via model.env could bind-mount arbitrary host paths. The worker
        env is set by the admin who runs the worker, the correct trust boundary
        (identical rationale to worker/backends/{indextts,lightx2v}.py).

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
        # Launch the image-embedded FastAPI app. GPUStack injects only the bound
        # host/port; checkpoints/variant selection is via ACESTEP_* env, not CLI.
        # --workers 1 is required: the API server uses an in-memory job queue.
        arguments = [
            "python3",
            "-m",
            "uvicorn",
            _SERVER_APP,
            "--workers",
            "1",
        ]
        # Allow version-specific command override if configured (registry-level,
        # admin-controlled).
        arguments = self.build_versioned_command_args(
            arguments,
            model_path=self._model_path,
            port=port,
        )

        # Like IndexTTS, the command target is uvicorn itself (no engine CLI): an
        # engine-style flag would abort startup and a user --host/--port would
        # displace the scheduler-assigned binding. All engine knobs are env vars,
        # so ignore user backend parameters entirely and say so in the log.
        user_backend_parameters = self._flatten_backend_param()
        if user_backend_parameters:
            logger.warning(
                "ACE-Step ignores backend_parameters %s: the serving command is "
                "uvicorn (no engine CLI); configure the engine via model env "
                "(ACESTEP_* variables) instead.",
                user_backend_parameters,
            )
        # Append immutable arguments so uvicorn binds the assigned host/port.
        extend_args_no_exist(
            arguments, ("--host", self._worker.ip), ("--port", str(port))
        )

        injected = self._get_injected_backend_parameters(arguments, [], entrypoint)

        return arguments, injected
