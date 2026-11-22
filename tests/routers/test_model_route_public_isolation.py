"""Route READ models must not inherit the write-side validation.

Same defect class as ``api_tokens must be a non-empty list`` on providers: a
validator that belongs to *input* was reached through inheritance from the model
used to *serialize* a stored row. Any row that exists but breaks an input rule —
hand-inserted with SQL, written by an older release, left by a migration — made
the whole list endpoint answer 500 instead of describing the row.

What is pinned here:

1. the read models carry **no validators at all** (structural check, not a spot
   check on one field);
2. their field sets match the table columns, so the deliberate duplication cannot
   silently drift when a column is added;
3. dirty rows serialize (the reported failure mode);
4. the write models keep rejecting exactly what they rejected before.
"""

import pytest

from gpustack.schemas.model_routes import (
    ModelRoute,
    ModelRouteCreate,
    ModelRoutePublic,
    ModelRouteTarget,
    ModelRouteTargetCreate,
    ModelRouteTargetPublic,
)

ROW_META = {
    "id": 1,
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
}


def _decorator_counts(model):
    decorators = model.__pydantic_decorators__
    return (
        len(decorators.validators)
        + len(decorators.field_validators)
        + len(decorators.model_validators)
        + len(decorators.root_validators)
    )


def test_public_models_have_no_validators():
    # The guarantee is structural: no @field_validator / @model_validator may
    # reach the read path, directly or by inheritance.
    assert _decorator_counts(ModelRouteTargetPublic) == 0
    assert _decorator_counts(ModelRoutePublic) == 0


def test_public_field_sets_match_the_table_columns():
    # The read models declare their fields explicitly (that is what buys the
    # isolation). This is the drift guard for the duplication: add a column in
    # one place and this fails.
    assert set(ModelRouteTargetPublic.model_fields) == set(
        ModelRouteTarget.model_fields
    )
    assert set(ModelRoutePublic.model_fields) - set(ModelRoute.model_fields) == {
        "effective_name"  # response-only enrichment, never a column
    }
    assert set(ModelRoute.model_fields) - set(ModelRoutePublic.model_fields) == set()


def _dirty_target_payload(**overrides):
    payload = dict(
        ROW_META,
        name="target-1",
        route_name="target-1",
        route_id=1,
        # 两个都填:输入侧明令禁止(provider/model 二选一),库里却可能存在
        provider_id=2,
        model_id=3,
        overridden_model_name="bad name!",
        weight=-5,
        state="unknown-state",
    )
    payload.update(overrides)
    return payload


def test_dirty_target_row_still_serializes():
    public = ModelRouteTargetPublic.model_validate(_dirty_target_payload())
    assert public.provider_id == 2 and public.model_id == 3
    assert public.overridden_model_name == "bad name!"
    assert public.weight == -5
    # Unknown enum value is reported as-is instead of raising.
    assert public.state == "unknown-state"


def test_dirty_route_row_still_serializes():
    public = ModelRoutePublic.model_validate(
        dict(
            ROW_META,
            name="org/route",  # 写侧禁止 '/',读侧必须能显示
            categories=["bogus-category"],
            access_policy="weird-policy",
            targets=-1,
            ready_targets=0,
            owner_principal_id=1,
        )
    )
    assert public.categories == ["bogus-category"]
    assert public.access_policy == "weird-policy"
    assert public.name == "org/route"


def test_valid_rows_serialize_to_the_same_json():
    # The permissive union must not change the wire format: the enum member and a
    # plain string both dump to the same JSON value the UI already consumes.
    public = ModelRouteTargetPublic.model_validate(
        _dirty_target_payload(state="active", weight=0, overridden_model_name="gpt-4o")
    )
    assert public.state == "active"
    assert public.weight == 0

    from gpustack.schemas.model_routes import TargetStateEnum

    for value in (TargetStateEnum.ACTIVE, "active"):
        dumped = ModelRouteTargetPublic.model_validate(
            _dirty_target_payload(state=value)
        ).model_dump()
        assert dumped["state"] == "active"


def test_write_models_still_reject_the_same_things():
    from pydantic import ValidationError

    # 二选一:both set / neither set
    for bad in (
        {"provider_id": 1, "model_id": 2},
        {},
        {"provider_id": 1},  # provider_id 还要求 overridden_model_name
    ):
        with pytest.raises(ValidationError):
            ModelRouteTargetCreate.model_validate(bad)

    with pytest.raises(ValidationError):
        ModelRouteTargetCreate.model_validate(
            {"model_id": 1, "overridden_model_name": "no-colon"}
        )

    # 路由:name 规则与 category 白名单仍在写侧生效
    with pytest.raises(ValidationError):
        ModelRouteCreate.model_validate({"name": "org/route"})
    with pytest.raises(ValidationError):
        ModelRouteCreate.model_validate({"name": "ok", "categories": ["bogus"]})
