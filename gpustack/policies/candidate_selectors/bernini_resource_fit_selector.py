from gpustack.policies.candidate_selectors.lightx2v_resource_fit_selector import (
    LightX2VResourceFitSelector,
)


class BerniniResourceFitSelector(LightX2VResourceFitSelector):
    """Candidate selector for the Bernini built-in video engine.

    Bernini (native Bernini-R renderer) is a video engine like LightX2V: video
    tasks run multi-GPU Ulysses (2-card in production; images are single-card),
    so — unlike IndexTTS which hard-codes 1 GPU — we do NOT override
    ``_resolve_gpus_per_replica``. It inherits LightX2V's deploy-input resolution
    (reads ``gpu_selector.gpus_per_replica`` / ``gpu_ids`` set at deploy time) and
    whole-GPU booking, so a Bernini replica gets exactly the GPUs the operator
    picks in the UI. Only the diagnostic label differs.
    """

    _ENGINE_LABEL = "Bernini"
