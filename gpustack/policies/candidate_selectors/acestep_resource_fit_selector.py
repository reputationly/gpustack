from gpustack.policies.candidate_selectors.lightx2v_resource_fit_selector import (
    LightX2VResourceFitSelector,
)
from gpustack.schemas.models import Model

# Absolute VRAM floor for an ACE-Step instance. The default xl-turbo path peaks
# ~26.5 GiB (600s clip); require a card with headroom so a heterogeneous cluster's
# small-but-free GPU (e.g. a 24 GiB card) is never chosen. The parent's whole-GPU
# check is a relative free-ratio only, with no absolute minimum — on the
# homogeneous A100-40G fleet this floor is a no-op, but it prevents guaranteed
# start/generation OOMs on smaller cards.
_MIN_TOTAL_VRAM_BYTES = 30 * 1024**3


class ACEStepResourceFitSelector(LightX2VResourceFitSelector):
    """
    Whole-GPU, single-card candidate selector for the ACE-Step-1.5 built-in
    text-to-music engine.

    ACE-Step is deployed one instance per GPU (whole-GPU exclusive), same profile
    as IndexTTS/LightX2V. Its footprint (turbo ~21 GiB / xl-turbo ~26.5 GiB peak
    for a 600s clip on a 40 GiB A100) leaves no room for a second full replica, so
    a card is dedicated to a single instance: book each chosen GPU's whole free
    VRAM so replicas spread one-per-GPU and never oversubscribe.

    Only two things differ from the parent: the per-replica GPU count is always 1
    (ACE-Step has no multi-GPU profile, so skip the deploy-input resolution and its
    multi-GPU warning), and the diagnostic label reads "ACE-Step".
    """

    _ENGINE_LABEL = "ACE-Step"

    @staticmethod
    def _resolve_gpus_per_replica(model: Model) -> int:
        # ACE-Step is always single-GPU; no profile table, no deploy input.
        return 1

    @staticmethod
    def _gpu_is_whole(free_vram: int, total_vram: int) -> bool:
        # Enforce an absolute VRAM floor on top of the parent's free-ratio check
        # so ACE-Step is never placed on a card too small for its footprint.
        if total_vram < _MIN_TOTAL_VRAM_BYTES:
            return False
        return LightX2VResourceFitSelector._gpu_is_whole(free_vram, total_vram)
