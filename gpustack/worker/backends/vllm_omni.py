import logging
import os
from typing import Optional, List, Dict, Tuple

from gpustack.schemas.models import ModelInstanceDeploymentMetadata
from gpustack.utils.command import (
    extend_args_no_exist,
    format_backend_parameters,
    is_parameter_key,
)
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
# == container path). This is how a vLLM-Omni instance gets the RW fast-disk NFS
# mount that holds the async task output files (save_result_path) and the
# reference audio (ref_audio / ref_audio_2), both absolute NFS paths injected by
# the facade. Read from os.environ so a model deployer cannot mount arbitrary
# host paths into the privileged container — see _get_extra_mounts and the
# identical rationale in worker/backends/{acestep,indextts,lightx2v}.py.
_EXTRA_MOUNTS_ENV_KEY = "GPUSTACK_EXTRA_MOUNTS"

# Binding args the scheduler owns; a user must never set them. Stripped from
# backend_parameters below so the scheduler-assigned host/port always win.
_SCHEDULER_OWNED_KEYS = frozenset({"host", "port"})


def _strip_scheduler_owned_binding(argv: List[str]) -> Tuple[List[str], List[str]]:
    """Drop any user-supplied --host/--port (and their value) from a flat argv.

    vLLM-Omni serves the engine CLI (``vllm serve``) directly with NO launcher
    port proxy (unlike LightX2V), so a stray user --host/--port would bind the
    container to an address the worker proxy / health check can't reach, leaving
    the instance unreachable even though it started. extend_args_no_exist would
    otherwise honor the user value (it only adds absent keys), so remove them
    first. Returns (filtered_argv, dropped_tokens).
    """
    filtered: List[str] = []
    dropped: List[str] = []
    skip_value = False
    for i, token in enumerate(argv):
        if skip_value:
            skip_value = False
            dropped.append(token)
            continue
        key = token.lstrip("-").split("=", 1)[0].lower()
        if is_parameter_key(token) and key in _SCHEDULER_OWNED_KEYS:
            dropped.append(token)
            # "--port 8000" (value in the next token) vs "--port=8000" (inline).
            if (
                "=" not in token
                and i + 1 < len(argv)
                and not is_parameter_key(argv[i + 1])
            ):
                skip_value = True
            continue
        filtered.append(token)
    return filtered, dropped


class VLLMOmniServer(InferenceServer):
    """vLLM-Omni multi-model speech/TTS engine (supersedes IndexTTS for TTS).

    Unlike ACE-Step/IndexTTS whose serving command is bare uvicorn, vLLM-Omni's
    command IS the engine CLI ``vllm serve <model> --omni``, so user
    backend_parameters (notably ``--deploy-config <yaml>`` for the 8B MOSS models
    that split stages across 2 GPUs) are passed THROUGH, mirroring LightX2V.
    The 2-card deploy-config yamls are baked into the engine image at
    ``/deploy-configs/``; the model path is bind-mounted host==container by
    base._get_configured_mounts, so it is valid inside the container.
    """

    def start(self):
        try:
            self._start()
        except Exception as e:
            self._handle_error(e)

    def _start(self):
        logger.info(f"Starting vLLM-Omni model instance: {self._model_instance.name}")

        deployment_metadata = self._get_deployment_metadata()

        env = self._get_configured_env()
        # Keep the engine air-gapped: never reach out to HF/ModelScope at runtime.
        # Auxiliary models (codecs, T5/CLIP for AudioX, MOSS-Audio-Tokenizer) are
        # pre-filled into the HF cache on NFS and loaded offline.
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
            raise ValueError("Failed to get vLLM-Omni backend image")

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

        logger.info(
            f"Creating vLLM-Omni container workload: {deployment_metadata.name}"
        )
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

        logger.info(f"Created vLLM-Omni container workload: {deployment_metadata.name}")

    def _get_extra_mounts(self) -> List[ContainerMount]:
        """
        Append extra RW host bind-mounts (one ContainerMount per comma-separated
        host path; host path == container path, mirroring the model_dir bind in
        base._get_configured_mounts). This is how a vLLM-Omni instance gets its
        RW fast-disk NFS mount holding the async task outputs and reference audio
        (e.g. GPUSTACK_EXTRA_MOUNTS=/nfs-rw).

        SECURITY: read from the WORKER PROCESS environment (os.environ), NOT the
        model's env — the container runs privileged, so a model deployer able to
        set this via model.env could bind-mount arbitrary host paths. The worker
        env is set by the admin who runs the worker, the correct trust boundary
        (identical rationale to worker/backends/{acestep,indextts,lightx2v}.py).

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
        # The command IS the engine CLI: ``vllm serve <model> --omni``. The model
        # path is the positional arg (bind-mounted host==container). Unlike
        # ACE-Step/IndexTTS (uvicorn), user backend_parameters are passed THROUGH
        # (mirroring LightX2V) so an operator can select a multi-card deploy
        # config, e.g. ``--deploy-config /deploy-configs/moss_ttsd_a100_40g.yaml``
        # to split the 8B MOSS talker + codec across 2 GPUs.
        arguments = [
            "vllm",
            "serve",
            self._model_path,
            "--omni",
        ]
        # Allow version-specific command override if configured (before appending
        # extra args), registry-level, admin-controlled.
        arguments = self.build_versioned_command_args(
            arguments,
            model_path=self._model_path,
            port=port,
        )

        user_backend_parameters = self._flatten_backend_param()
        # Strip any user-supplied --host/--port BEFORE appending so the
        # scheduler-assigned binding always wins (see _strip_scheduler_owned_binding:
        # no launcher proxy, so a user override would make the instance
        # unreachable). extend_args_no_exist only fills absent keys, so removing
        # the user values here is what forces our host/port through.
        user_backend_parameters, dropped_binding = _strip_scheduler_owned_binding(
            user_backend_parameters
        )
        if dropped_binding:
            logger.warning(
                "vLLM-Omni ignores user-supplied binding args %s: --host/--port are "
                "owned by the scheduler (the worker proxy/health check target the "
                "assigned binding).",
                dropped_binding,
            )
        arguments.extend(user_backend_parameters)
        # Append immutable arguments (only if not already present): the engine
        # needs trust-remote-code for the community model code, and must bind the
        # scheduler-assigned host/port so a user param cannot displace it.
        extend_args_no_exist(
            arguments,
            "--trust-remote-code",
            ("--host", self._worker.ip),
            ("--port", str(port)),
        )

        injected = self._get_injected_backend_parameters(
            arguments, user_backend_parameters, entrypoint
        )

        return arguments, injected
