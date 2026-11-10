"""The fork's common_parameters backfill for built-in backends.

Seeding built-in backends is create-only, so a row that existed before a
built-in definition gained ``common_parameters`` would never receive them.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gpustack.schemas.inference_backend import (
    BackendSourceEnum,
    InferenceBackend,
)
from gpustack.server import controllers
from gpustack.server.controllers import InferenceBackendController

BUILT_IN_PARAMS = ["--profile", "--task"]


def _built_in():
    return InferenceBackend(
        backend_name="LightX2V", common_parameters=list(BUILT_IN_PARAMS)
    )


def _existing(common_parameters):
    row = MagicMock(spec=InferenceBackend)
    row.backend_name = "LightX2V"
    row.backend_source = BackendSourceEnum.BUILT_IN
    row.enabled = True
    row.common_parameters = common_parameters
    row.update = AsyncMock()
    return row


async def _run(monkeypatch, existing):
    built_in = _built_in()
    create = AsyncMock()
    monkeypatch.setattr(controllers, "get_built_in_backend", lambda: [built_in])
    monkeypatch.setattr(
        InferenceBackend, "one_by_fields", AsyncMock(return_value=existing)
    )
    monkeypatch.setattr(InferenceBackend, "create", create)
    controller = InferenceBackendController.__new__(InferenceBackendController)
    await controller._init_built_in_backends(MagicMock())
    return built_in, create


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [None, []])
async def test_empty_common_parameters_are_backfilled(monkeypatch, empty):
    row = _existing(empty)
    _, create = await _run(monkeypatch, row)

    create.assert_not_awaited()
    row.update.assert_awaited_once()
    assert row.update.await_args.args[1] == {"common_parameters": BUILT_IN_PARAMS}


@pytest.mark.asyncio
async def test_operator_customised_common_parameters_are_kept(monkeypatch):
    row = _existing(["--my-own-flag"])
    await _run(monkeypatch, row)

    row.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_row_is_created_with_the_built_in_definition(monkeypatch):
    built_in, create = await _run(monkeypatch, None)

    create.assert_awaited_once()
    created = create.await_args.args[1]
    assert created is built_in
    assert created.common_parameters == BUILT_IN_PARAMS
    assert created.backend_source == BackendSourceEnum.BUILT_IN
