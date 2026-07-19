from gpustack.policies.candidate_selectors.lightx2v_resource_fit_selector import (
    LightX2VResourceFitSelector,
)
from gpustack.schemas.models import Model

# Absolute VRAM floor for a MULTI-GPU vLLM-Omni instance (the 8B MOSS models:
# TTSD / SoundEffect / VoiceGenerator). On a homogeneous A100-40G fleet it is a
# no-op (every card is 40G >= 30G); on a heterogeneous fleet it keeps those big
# models off cards too small to host a stage. Deliberately NOT applied to the
# single-card TTS models (Qwen3-TTS/VoxCPM2/CosyVoice3/GLM-TTS/MOSS-Nano, some
# needing only ~1-4.5 GiB) — they must stay schedulable on smaller cards.
_MIN_TOTAL_VRAM_BYTES = 30 * 1024**3


class VLLMOmniResourceFitSelector(LightX2VResourceFitSelector):
    """
    Whole-GPU candidate selector for the vLLM-Omni built-in speech engine.

    vLLM-Omni is deployed whole-GPU exclusive (one replica books each chosen
    GPU's entire free VRAM), same as LightX2V/IndexTTS/ACE-Step. The one
    difference from IndexTTS/ACE-Step: those override ``_resolve_gpus_per_replica``
    to a hard ``return 1`` (single-card only). vLLM-Omni does NOT — it INHERITS
    the parent's deploy-input-driven count so small TTS models run on 1 card
    while the 8B MOSS models (TTSD / SoundEffect) run on 2 cards. Those 2-card
    models MUST set ``gpu_selector.gpus_per_replica: 2`` in the catalog entry (and
    pass a matching ``--deploy-config`` that splits the talker + codec across the
    two devices) — GPUStack cannot read the engine-image deploy configs, so the
    card count is a deploy input exactly like the LightX2V wan int8 4-card profile.

    Only two things differ from the parent: the diagnostic label reads
    "vLLM-Omni", and an absolute VRAM floor is enforced via ``_gpu_is_whole`` —
    but ONLY for multi-GPU (8B MOSS) deployments, so small single-card TTS models
    stay schedulable on smaller cards on a heterogeneous fleet.
    """

    _ENGINE_LABEL = "vLLM-Omni"

    @staticmethod
    def _resolve_gpus_per_replica(model: Model) -> int:
        # Deploy-input-driven count (2 for the 8B MOSS models set in the catalog),
        # but default to 1 SILENTLY: single-card is the norm for vLLM-Omni TTS, so
        # unlike the parent we do not emit its "no gpus_per_replica … defaulting to
        # 1" warning (which would also mislabel the engine as "LightX2V") on every
        # single-card deploy. Multi-card models MUST set gpu_selector; if they
        # forget, they fall back to 1 the same as the parent — the catalog entries
        # for MOSS-TTSD/VoiceGenerator/SoundEffect carry gpus_per_replica: 2.
        gpu_selector = model.gpu_selector
        if (
            gpu_selector
            and gpu_selector.gpus_per_replica
            and gpu_selector.gpus_per_replica > 0
        ):
            return gpu_selector.gpus_per_replica
        if gpu_selector and gpu_selector.gpu_ids:
            replicas = model.replicas or 1
            per_replica = len(gpu_selector.gpu_ids) // replicas
            return per_replica if per_replica > 0 else 1
        return 1

    def _gpu_is_whole(self, free_vram: int, total_vram: int) -> bool:
        # Instance method (parent calls it as ``self._gpu_is_whole(...)``) so the
        # large-card floor can be conditional: apply it ONLY to multi-GPU (8B
        # MOSS) deployments, where each stage needs a big card. Single-card TTS
        # models (MOSS-Nano ~1-4.5 GiB) skip the floor and stay schedulable on
        # smaller cards. The parent's whole-GPU free-ratio check always applies.
        if self._gpus_per_replica >= 2 and total_vram < _MIN_TOTAL_VRAM_BYTES:
            return False
        return LightX2VResourceFitSelector._gpu_is_whole(free_vram, total_vram)
