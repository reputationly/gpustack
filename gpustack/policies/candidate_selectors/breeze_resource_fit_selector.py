from gpustack.policies.candidate_selectors.lightx2v_resource_fit_selector import (
    LightX2VResourceFitSelector,
)
from gpustack.schemas.models import Model


class BreezeTTSResourceFitSelector(LightX2VResourceFitSelector):
    """
    Whole-GPU, single-card candidate selector for the Breeze TTS 2 built-in
    engine.

    Breeze TTS 2 has no multi-GPU profile, so a replica is always one card.
    Steady-state VRAM is flat at ~8.4 GiB regardless of text length — the CUDA
    graphs run off a StaticCache preallocated at max_seq_len, so a long script
    just fills slots that were already reserved — but graph capture during
    warmup peaks higher (8.67 GiB on the default nocodec tier, 26.93 GiB if a
    deployment switches to fast_all). Book the whole card so that transient
    peak can never collide with a co-scheduled instance.
    """

    _ENGINE_LABEL = "BreezeTTS"

    @staticmethod
    def _resolve_gpus_per_replica(model: Model) -> int:
        # Breeze TTS 2 is always single-GPU; no profile table, no deploy input.
        return 1
