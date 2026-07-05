import logging
from typing import List

from gpustack.policies.base import ModelInstanceScheduleCandidate
from gpustack.policies.candidate_selectors.custom_backend_resource_fit_selector import (
    CustomBackendResourceFitSelector,
)
from gpustack.policies.event_recorder.recorder import EventLevelEnum
from gpustack.policies.utils import (
    get_worker_allocatable_resource,
    ListMessageBuilder,
    group_workers_by_gpu_type,
)
from gpustack.schemas.models import Model, ModelInstance
from gpustack.schemas.workers import Worker
from gpustack.config import Config

logger = logging.getLogger(__name__)

EVENT_ACTION_PROFILE_FIT = "lightx2v_profile_fit_msg"

# Minimum RAM reserved per LightX2V instance (host offload staging etc.).
_MIN_RAM_CLAIM = 2 * 1024**3

# LightX2V profiles reserve the WHOLE GPU (dedicated nodes). A GPU only counts as
# usable if nearly all of its VRAM is free — otherwise a whole-GPU profile could
# be co-scheduled onto a GPU already holding another allocation and OOM. The
# small slack tolerates driver/system reservations.
_WHOLE_GPU_MIN_FREE_RATIO = 0.9


class LightX2VResourceFitSelector(CustomBackendResourceFitSelector):
    """
    Profile-table-driven candidate selector for the LightX2V built-in backend.

    Unlike the VRAM-search selectors (vLLM/SGLang/Custom), LightX2V runs on
    homogeneous, dedicated nodes with a profile whose GPU count is fixed at
    deploy time (z_image = 1 card, wan int8 = 4 cards; see
    docs/lightx2v-backend-design.md §3/§5). So placement is purely count-based:

    - reserve exactly ``gpus_per_replica`` GPUs on a single worker;
    - book each chosen GPU's whole free VRAM, so replicas spread one-per-GPU
      (z_image) or one-per-4-GPU-node (wan) and never oversubscribe;
    - no model-weight VRAM estimation — video/image engines lack a standard HF
      config, and the profile already fixes the footprint.

    Manual GPU selection is delegated to the inherited machinery.
    """

    def __init__(self, cfg: Config, model: Model, model_instances: List[ModelInstance]):
        super().__init__(cfg, model, model_instances)
        self._gpus_per_replica = self._resolve_gpus_per_replica(model)

    @staticmethod
    def _resolve_gpus_per_replica(model: Model) -> int:
        # LightX2V's per-replica GPU count is a DEPLOY input: GPUStack cannot read
        # the engine-image profiles.yaml, so a multi-GPU profile (e.g. wan int8
        # 4-card) must be deployed with gpu_selector.gpus_per_replica (or pinned
        # gpu_ids). The catalog entry for such profiles is responsible for setting
        # it; if it is missing we fall back to 1 and warn, because silently
        # under-allocating would make the launcher run the wrong (1-card) profile.
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
        logger.warning(
            "LightX2V model %s has no gpus_per_replica or gpu_ids set; defaulting "
            "to 1 GPU/replica. Multi-GPU profiles (e.g. wan int8 4-card) must set "
            "gpus_per_replica at deploy time or they will run the wrong profile.",
            getattr(model, "readable_source", None) or getattr(model, "name", "?"),
        )
        return 1

    @staticmethod
    def _gpu_is_whole(free_vram: int, total_vram: int) -> bool:
        """A GPU is usable for a whole-GPU LightX2V profile only if nearly all of
        its VRAM is free (rejects GPUs already holding another allocation)."""
        return total_vram > 0 and free_vram >= total_vram * _WHOLE_GPU_MIN_FREE_RATIO

    async def select_candidates(
        self, workers: List[Worker]
    ) -> List[ModelInstanceScheduleCandidate]:
        # Profile fixes the footprint; skip model-weight estimation. The whole
        # free VRAM of each chosen GPU is booked in the candidate below.
        self._vram_claim = 0
        self._ram_claim = _MIN_RAM_CLAIM

        if self._selected_gpu_workers:
            # User pinned specific GPUs. Reserve those GPUs' whole free VRAM
            # (same as the auto path) rather than the inherited manual path,
            # which would book 0 bytes here (vram_claim=0) and leave the pinned
            # GPUs over-subscribable.
            candidates = self._find_manual_candidates(workers)
        else:
            candidates = self._find_profile_candidates(workers)

        if candidates:
            self._set_messages()
            return candidates

        self._add_no_candidates_message()
        self._set_messages()
        return []

    def _find_profile_candidates(
        self, workers: List[Worker]
    ) -> List[ModelInstanceScheduleCandidate]:
        """
        Reserve exactly ``gpus_per_replica`` free GPUs on a single worker,
        booking each chosen GPU's whole free VRAM.
        """
        need = self._gpus_per_replica
        candidates: List[ModelInstanceScheduleCandidate] = []
        largest_free_count = 0

        workers_by_gpu_type = group_workers_by_gpu_type(workers)
        for gpu_type, workers_of_type in workers_by_gpu_type.items():
            for worker in workers_of_type:
                if not worker.status or not worker.status.gpu_devices:
                    continue

                allocatable = get_worker_allocatable_resource(
                    self._model_instances, worker, gpu_type
                )
                if allocatable.ram < self._ram_claim:
                    continue

                vram_totals = self._get_worker_vram_totals(worker, gpu_type)
                free = [
                    (device.index, allocatable.vram[device.index])
                    for device in worker.status.gpu_devices
                    if self._gpu_is_whole(
                        allocatable.vram.get(device.index, 0),
                        vram_totals.get(device.index, 0),
                    )
                ]
                largest_free_count = max(largest_free_count, len(free))
                if len(free) < need:
                    continue

                chosen = free[:need]
                gpu_indexes = [index for index, _ in chosen]
                vram_distribution = {index: vram for index, vram in chosen}

                if need == 1:
                    candidates.append(
                        self._create_single_gpu_candidate(
                            worker, gpu_indexes, vram_distribution, gpu_type
                        )
                    )
                else:
                    candidates.append(
                        self._create_multi_gpu_candidate(
                            worker, gpu_indexes, vram_distribution, gpu_type
                        )
                    )

        if not candidates:
            self._event_collector.add(
                EventLevelEnum.INFO,
                EVENT_ACTION_PROFILE_FIT,
                str(
                    ListMessageBuilder(
                        f"LightX2V profile needs {need} free GPU(s) on a single worker; "
                        f"the largest available worker has {largest_free_count} free GPU(s)."
                    )
                ),
            )
        return candidates

    def _find_manual_candidates(
        self, workers: List[Worker]
    ) -> List[ModelInstanceScheduleCandidate]:
        """
        Honor the user-pinned GPUs, reserving each chosen GPU's whole free VRAM
        (consistent with the auto path). Restricts placement to the pinned GPU
        indexes so manual selection never books 0 bytes / over-subscribes.
        """
        need = self._gpus_per_replica
        workers_by_name = {w.name: w for w in workers}
        candidates: List[ModelInstanceScheduleCandidate] = []

        for (
            gpu_type,
            indexes_by_worker,
        ) in self._selected_gpu_indexes_by_gpu_type_and_worker.items():
            for worker_name, pinned_indexes in indexes_by_worker.items():
                worker = workers_by_name.get(worker_name)
                if not worker or not worker.status or not worker.status.gpu_devices:
                    continue

                allocatable = get_worker_allocatable_resource(
                    self._model_instances, worker, gpu_type
                )
                if allocatable.ram < self._ram_claim:
                    continue

                vram_totals = self._get_worker_vram_totals(worker, gpu_type)
                free = [
                    index
                    for index in pinned_indexes
                    if self._gpu_is_whole(
                        allocatable.vram.get(index, 0), vram_totals.get(index, 0)
                    )
                ]
                if len(free) < need:
                    continue

                chosen = free[:need]
                vram_distribution = {index: allocatable.vram[index] for index in chosen}

                if need == 1:
                    candidates.append(
                        self._create_single_gpu_candidate(
                            worker, chosen, vram_distribution, gpu_type
                        )
                    )
                else:
                    candidates.append(
                        self._create_multi_gpu_candidate(
                            worker, chosen, vram_distribution, gpu_type
                        )
                    )

        return candidates
