"""``WorkerPublic`` must not reject a stored worker row.

``WorkerBase.validate_proxy_address`` demands a full URL. A row holding
``proxy_address = "1.2.3.4:8000"`` (no scheme — what a hand-edit or an older
script leaves behind) failed to *serialize*, so ``GET /v1/workers`` answered 500
for the whole fleet list instead of showing the row that needs fixing.

The rule stays on the write side (``WorkerBase``); only the read model is free of
it.
"""

import pytest
from pydantic import ValidationError

from gpustack.schemas.workers import (
    Worker,
    WorkerBase,
    WorkerPublic,
    WorkerRegistrationPublic,
)

ROW_META = {
    "id": 1,
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
}


def _decorator_count(model) -> int:
    d = model.__pydantic_decorators__
    return (
        len(d.validators)
        + len(d.field_validators)
        + len(d.model_validators)
        + len(d.root_validators)
    )


def test_public_model_has_no_validators():
    assert _decorator_count(WorkerPublic) == 0
    # The registration view inherits it; keep it clean too.
    assert _decorator_count(WorkerRegistrationPublic) == 0


def test_public_field_set_matches_the_table():
    public = set(WorkerPublic.model_fields)
    table = set(Worker.model_fields)
    # token is a secret (never exposed), deleted_at is not part of the contract.
    assert table - public == {"deleted_at", "token"}
    # Response-only additions.
    assert public - table == {"me", "provision_progress"}


def _payload(**overrides):
    payload = dict(
        ROW_META,
        name="w1",
        hostname="w1",
        ip="1.2.3.4",
        ifname="eth0",
        port=10150,
        worker_uuid="uuid-1",
        # 写侧要求 http://host:port,这里给出手改库会出现的形状
        proxy_address="1.2.3.4:8000",
        state="not-a-state",
        proxy_mode="not-a-mode",
        provider="not-a-provider",
    )
    payload.update(overrides)
    return payload


def test_dirty_worker_row_serializes():
    public = WorkerPublic.model_validate(_payload())
    assert public.proxy_address == "1.2.3.4:8000"
    assert public.state == "not-a-state"
    assert public.proxy_mode == "not-a-mode"
    assert public.provider == "not-a-provider"


def test_valid_row_keeps_the_wire_format():
    from gpustack.schemas.clusters import ClusterProvider
    from gpustack.schemas.workers import WorkerStateEnum

    dumped = WorkerPublic.model_validate(
        _payload(
            proxy_address="http://1.2.3.4:8000",
            state=WorkerStateEnum.READY,
            provider=ClusterProvider.Docker,
        )
    ).model_dump(mode="json")
    assert dumped["state"] == "ready"
    assert dumped["provider"] == "Docker"
    assert dumped["proxy_address"] == "http://1.2.3.4:8000"


def test_write_side_still_rejects_a_non_url_proxy_address():
    # The rule lives on WorkerBase and stays there.
    assert _decorator_count(WorkerBase) > 0
    with pytest.raises(ValidationError):
        WorkerBase.model_validate(
            {
                "name": "w1",
                "hostname": "w1",
                "ip": "1.2.3.4",
                "ifname": "eth0",
                "port": 10150,
                "worker_uuid": "uuid-1",
                "proxy_address": "1.2.3.4:8000",
            }
        )
