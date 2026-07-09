from gpustack.policies.candidate_selectors.lightx2v_resource_fit_selector import (
    LightX2VResourceFitSelector,
)
from gpustack.schemas.models import Model


class IndexTTSResourceFitSelector(LightX2VResourceFitSelector):
    """
    Whole-GPU, single-card candidate selector for the IndexTTS-2 built-in TTS
    engine.

    IndexTTS-2 is deployed one instance per GPU (whole-GPU exclusive). Its idle
    footprint is only ~8 GiB, but long-text synthesis pushes VRAM up — the AR
    KV cache grows with the generated audio length and BigVGAN vocodes the whole
    mel in one shot — so a card is dedicated to a single instance to remove OOM
    risk from co-scheduling. Placement is therefore identical to LightX2V's
    whole-GPU profile with a fixed 1 GPU/replica: book each chosen GPU's whole
    free VRAM so replicas spread one-per-GPU and never oversubscribe.

    Only two things differ from the parent: the per-replica GPU count is always
    1 (IndexTTS has no multi-GPU profile, so skip the deploy-input resolution and
    its multi-GPU warning), and the diagnostic label reads "IndexTTS".
    """

    _ENGINE_LABEL = "IndexTTS"

    @staticmethod
    def _resolve_gpus_per_replica(model: Model) -> int:
        # IndexTTS-2 is always single-GPU; no profile table, no deploy input.
        return 1
