from gpustack.policies.candidate_selectors.lightx2v_resource_fit_selector import (
    LightX2VResourceFitSelector,
)
from gpustack.schemas.models import Model


class YuE2ResourceFitSelector(LightX2VResourceFitSelector):
    """
    Whole-GPU, single-card candidate selector for the YuE2 built-in engine.

    YuE2 has no multi-GPU path: one instance runs its stages on one device, so
    a replica is always one card. The engine (YuE2-Turbo) keeps the backbone,
    the VAE and a vLLM worker resident and batches up to 4 songs: 22.5 GB idle
    and a 30.8 GiB peak at 4-way on A100-40G (with the cover transcriber
    loaded). That leaves no room for a co-scheduled instance, so the card is
    booked whole.
    """

    _ENGINE_LABEL = "YuE2"

    @staticmethod
    def _resolve_gpus_per_replica(model: Model) -> int:
        # YuE2 is always single-GPU; no profile table, no deploy input.
        return 1
