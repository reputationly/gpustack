"""Fork backends: selector dispatch and whole-GPU placement on the v2.3.0 API."""

from unittest.mock import patch

import pytest

from gpustack.policies.base import Allocated
from gpustack.policies.resource_view import ResourceView
from gpustack.scheduler.scheduler import (
    ACEStepResourceFitSelector,
    BerniniResourceFitSelector,
    BreezeTTSResourceFitSelector,
    IndexTTSResourceFitSelector,
    LightX2VResourceFitSelector,
    VLLMOmniResourceFitSelector,
    YuE2ResourceFitSelector,
    build_candidate_selector,
)
from gpustack.schemas.models import BackendEnum, GPUSelector, SourceEnum
from tests.fixtures.workers.fixtures import (
    linux_cpu_1,
    linux_nvidia_4_4080_16gx4,
    linux_nvidia_22_H100_80gx8,
)
from tests.utils.mock import mock_async_session
from tests.utils.model import new_model

FORK_SELECTORS = [
    (BackendEnum.LIGHTX2V, LightX2VResourceFitSelector),
    (BackendEnum.BERNINI, BerniniResourceFitSelector),
    (BackendEnum.INDEXTTS, IndexTTSResourceFitSelector),
    (BackendEnum.BREEZE_TTS, BreezeTTSResourceFitSelector),
    (BackendEnum.YUE2, YuE2ResourceFitSelector),
    (BackendEnum.ACESTEP, ACEStepResourceFitSelector),
    (BackendEnum.VLLM_OMNI, VLLMOmniResourceFitSelector),
]


def _model(backend, gpus_per_replica=None):
    model = new_model(1, "fork-model", 1, huggingface_repo_id="fork/model")
    model.source = SourceEnum.LOCAL_PATH
    model.huggingface_repo_id = None
    model.local_path = "/models/fork-model"
    model.backend = backend
    if gpus_per_replica:
        model.gpu_selector = GPUSelector(gpus_per_replica=gpus_per_replica)
    return model


async def _select(selector, workers):
    with (
        patch("gpustack.schemas.workers.Worker.all", return_value=workers),
        patch(
            "gpustack.policies.worker_filters.backend_framework_filter.async_session",
            return_value=mock_async_session(),
        ),
    ):
        return await selector.select_candidates(workers)


@pytest.mark.parametrize("backend, selector_cls", FORK_SELECTORS)
def test_dispatch_returns_the_fork_selector(config, backend, selector_cls):
    view = ResourceView([])
    selector = build_candidate_selector(config, _model(backend), [], resource_view=view)
    assert type(selector) is selector_cls
    assert selector._resource_view is view


@pytest.mark.parametrize("backend, selector_cls", FORK_SELECTORS)
@pytest.mark.asyncio
async def test_places_one_whole_gpu(config, backend, selector_cls):
    selector = build_candidate_selector(
        config, _model(backend), [], resource_view=ResourceView([])
    )
    worker = linux_nvidia_22_H100_80gx8()
    candidates = await _select(selector, [worker])
    assert candidates
    assert all(len(c.gpu_indexes) == 1 for c in candidates)
    assert all(c.worker.id == worker.id for c in candidates)


@pytest.mark.parametrize("backend, selector_cls", FORK_SELECTORS)
@pytest.mark.asyncio
async def test_no_candidate_on_cpu_only_worker(config, backend, selector_cls):
    selector = build_candidate_selector(
        config, _model(backend), [], resource_view=ResourceView([])
    )
    assert await _select(selector, [linux_cpu_1()]) == []


@pytest.mark.parametrize(
    "backend",
    [BackendEnum.LIGHTX2V, BackendEnum.BERNINI, BackendEnum.VLLM_OMNI],
)
@pytest.mark.asyncio
async def test_multi_gpu_replica(config, backend):
    selector = build_candidate_selector(
        config, _model(backend, gpus_per_replica=2), [], resource_view=ResourceView([])
    )
    candidates = await _select(selector, [linux_nvidia_22_H100_80gx8()])
    assert candidates
    assert all(len(c.gpu_indexes) == 2 for c in candidates)

    # Four cards cannot hold an 8-card replica.
    selector = build_candidate_selector(
        config, _model(backend, gpus_per_replica=8), [], resource_view=ResourceView([])
    )
    assert await _select(selector, [linux_nvidia_4_4080_16gx4()]) == []


@pytest.mark.parametrize(
    "backend, gpus_per_replica",
    [(BackendEnum.ACESTEP, None), (BackendEnum.VLLM_OMNI, 2)],
)
@pytest.mark.asyncio
async def test_absolute_vram_floor_rejects_small_cards(
    config, backend, gpus_per_replica
):
    selector = build_candidate_selector(
        config,
        _model(backend, gpus_per_replica=gpus_per_replica),
        [],
        resource_view=ResourceView([]),
    )
    assert await _select(selector, [linux_nvidia_4_4080_16gx4()]) == []


@pytest.mark.asyncio
async def test_reservations_in_the_resource_view_are_honoured(config):
    """A GPU reserved through ResourceView is not whole, so it is skipped."""
    worker = linux_nvidia_22_H100_80gx8()
    reserved = Allocated(ram=0, vram={0: 40 * 1024**3, 1: 40 * 1024**3})
    view = ResourceView([], {worker.id: reserved})
    selector = build_candidate_selector(
        config, _model(BackendEnum.LIGHTX2V), [], resource_view=view
    )
    candidates = await _select(selector, [worker])
    used = {i for c in candidates for i in c.gpu_indexes}
    assert used and used.isdisjoint({0, 1})
