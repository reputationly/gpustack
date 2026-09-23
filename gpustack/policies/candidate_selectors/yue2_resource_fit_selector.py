from gpustack.policies.candidate_selectors.lightx2v_resource_fit_selector import (
    LightX2VResourceFitSelector,
)
from gpustack.schemas.models import Model


class YuE2ResourceFitSelector(LightX2VResourceFitSelector):
    """
    Whole-GPU, single-card candidate selector for the YuE2 built-in engine.

    YuE2 has no multi-GPU path: its three generative stages run serially on one
    device (the pipeline even moves the backbone to CPU for the VAE decode), so
    a replica is always one card. Peak VRAM is flat at 8.0-8.9 GiB on A100
    whatever the song length (measured from a 1-minute to a 4-minute song), well
    under the 22 GiB per-process cap the engine sets for itself; the card is
    still booked whole so that cap can never collide with a co-scheduled
    instance.
    """

    _ENGINE_LABEL = "YuE2"

    @staticmethod
    def _resolve_gpus_per_replica(model: Model) -> int:
        # YuE2 is always single-GPU; no profile table, no deploy input.
        return 1
