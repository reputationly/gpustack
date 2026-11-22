"""``ClusterPublic`` must not inherit the write-side validation.

Same defect class as ``api_tokens must be a non-empty list`` (providers) and
``Invalid category`` (model routes): a validator that belongs to the *input*
model was reached through inheritance by the model used to serialize a stored
row, so one row that violates an input rule answered 500 for the whole list.

For clusters the rule in question is ``validate_server_url`` on
``ClusterUpdate``: a ``server_url`` stored without a scheme (``10.0.0.5:8080``,
written by hand in SQL) made ``GET /v1/clusters`` fail — and because the list is
what the UI needs to render its edit form, the bad row could not be fixed from
the UI at all.

Pinned here:
1. the read model carries no validators;
2. its field set matches the table columns plus the response-only counters, so the
   deliberate duplication cannot drift;
3. a dirty row serializes;
4. the write path still rejects exactly what it rejected before.
"""

import pytest

from gpustack.schemas.clusters import (
    Cluster,
    ClusterCreate,
    ClusterPublic,
    ClusterUpdate,
)

ROW_META = {
    "id": 1,
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
}

# Columns the API never exposes, and response-only counters that are not columns.
TABLE_ONLY = {"hashed_suffix", "registration_token"}
PUBLIC_ONLY = {"workers", "ready_workers", "gpus", "models"}


def _decorator_count(model) -> int:
    decorators = model.__pydantic_decorators__
    return (
        len(decorators.validators)
        + len(decorators.field_validators)
        + len(decorators.model_validators)
        + len(decorators.root_validators)
    )


def test_public_model_has_no_validators():
    # Structural guarantee: nothing may reach the read path, directly or by
    # inheritance. A spot check on server_url would miss the next validator.
    assert _decorator_count(ClusterPublic) == 0


def test_public_field_set_matches_the_table():
    public = set(ClusterPublic.model_fields)
    table = set(Cluster.model_fields)
    assert table - public == TABLE_ONLY
    assert public - table == PUBLIC_ONLY


def _dirty_payload(**overrides):
    payload = dict(
        ROW_META,
        name="c1",
        # 写侧要求 http(s)://host[:port],这里刻意给一个手改库会出现的形状
        server_url="10.0.0.5:8080",
        state="unknown-state",
        provider="unknown-provider",
        owner_principal_id=None,
    )
    payload.update(overrides)
    return payload


def test_dirty_cluster_row_still_serializes():
    public = ClusterPublic.model_validate(_dirty_payload())
    assert public.server_url == "10.0.0.5:8080"
    # Unknown enum values are reported as-is instead of raising.
    assert public.state == "unknown-state"
    assert public.provider == "unknown-provider"


def test_valid_row_serializes_to_the_same_json():
    from gpustack.schemas.clusters import ClusterProvider, ClusterStateEnum

    for value, expected in (
        (ClusterStateEnum.READY, "ready"),
        ("ready", "ready"),
    ):
        # mode="json" is the wire format FastAPI emits; both the enum member and
        # the plain string must land on the same JSON value the UI consumes.
        dumped = ClusterPublic.model_validate(
            _dirty_payload(state=value, provider=ClusterProvider.Docker)
        ).model_dump(mode="json")
        assert dumped["state"] == expected
        assert dumped["provider"] == "Docker"


def test_write_model_still_validates_server_url():
    from pydantic import ValidationError

    for bad in ("10.0.0.5:8080", "gpustack.local", "://nope"):
        with pytest.raises(ValidationError):
            ClusterUpdate.model_validate({"name": "c1", "server_url": bad})

    # A proper URL passes and is normalised (trailing slash dropped).
    assert (
        ClusterUpdate.model_validate(
            {"name": "c1", "server_url": "http://10.0.0.5:8080/"}
        ).server_url
        == "http://10.0.0.5:8080"
    )

    # Same rule on the create body, which is what actually persists the column.
    with pytest.raises(ValidationError):
        ClusterCreate.model_validate({"name": "c1", "server_url": "10.0.0.5:8080"})
