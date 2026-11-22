"""``ModelPublic`` / ``ModelInstancePublic`` must not reject stored rows.

Same defect class as ``api_tokens must be a non-empty list`` (providers):
``ModelSource.check_huggingface_fields`` — a rule for user input — was reached by
inheritance from the models used to *serialize* rows. A row stored with
``source=huggingface`` and an empty ``huggingface_repo_id`` (hand-edited, or a
column nulled by hand) made ``GET /v1/models`` answer 500 for the whole page.

``distributed_inference_across_workers`` is reported **as stored** (A″): it used to
be derived while serializing, which let the API say "on" while the scheduler read
the row itself and treated NULL as off. Derivation lives on the write side now, and
legacy NULLs are backfilled by migration ``d4b8e2a16c37``.
"""

import pytest
from pydantic import ValidationError

from gpustack.schemas.models import (
    Model,
    ModelCreate,
    ModelInstance,
    ModelInstancePublic,
    ModelPublic,
    ModelSource,
    SourceEnum,
)

ROW_META = {
    "id": 1,
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
}


def _decorator_counts(model):
    d = model.__pydantic_decorators__
    return (
        len(d.validators),
        len(d.field_validators),
        len(d.model_validators),
        len(d.root_validators),
    )


def test_instance_read_model_has_no_validators():
    assert _decorator_counts(ModelInstancePublic) == (0, 0, 0, 0)


def test_model_read_model_has_no_validators():
    # A″: no fill either — what the API returns must be what the scheduler reads.
    assert _decorator_counts(ModelPublic) == (0, 0, 0, 0)


def test_read_field_sets_match_the_tables():
    assert set(ModelPublic.model_fields) - set(Model.model_fields) == {
        "has_stale_lora_instances"
    }
    assert set(Model.model_fields) - set(ModelPublic.model_fields) == {"deleted_at"}
    assert set(ModelInstance.model_fields) - set(ModelInstancePublic.model_fields) == {
        "deleted_at"
    }
    assert (
        set(ModelInstancePublic.model_fields) - set(ModelInstance.model_fields) == set()
    )


def _dirty_model_payload(**overrides):
    return dict(
        ROW_META,
        name="m1",
        # 写侧明令禁止:source=huggingface 但 repo id 为空
        source="huggingface",
        huggingface_repo_id=None,
        placement_strategy="not-a-strategy",
        access_policy="not-a-policy",
        **overrides,
    )


def test_dirty_model_row_serializes():
    public = ModelPublic.model_validate(_dirty_model_payload())
    assert public.source == "huggingface"
    assert public.huggingface_repo_id is None
    # Unknown enum values are reported as-is instead of raising.
    assert public.placement_strategy == "not-a-strategy"
    assert public.access_policy == "not-a-policy"


def test_dirty_instance_row_serializes():
    public = ModelInstancePublic.model_validate(
        dict(
            ROW_META,
            name="m1--0",
            model_name="m1",
            source="local_path",
            local_path=None,
            state="not-a-state",
            # 嵌套的 source 也不能在读过��拒错(source=huggingface 但无 repo id)
            draft_model_source={"source": "huggingface"},
        )
    )
    assert public.source == "local_path"
    assert public.state == "not-a-state"
    assert public.draft_model_source.source == "huggingface"


def test_distributed_flag_is_reported_as_stored():
    # NULL is NOT derived any more: the scheduler treats it as off, so the API must
    # not claim it is on. (Legacy NULLs are backfilled by migration d4b8e2a16c37.)
    assert (
        ModelPublic.model_validate(
            _dirty_model_payload()
        ).distributed_inference_across_workers
        is None
    )

    stored = ModelPublic.model_validate(
        _dirty_model_payload(backend="vLLM", distributed_inference_across_workers=False)
    )
    assert stored.distributed_inference_across_workers is False


def test_write_side_still_derives_the_flag():
    # Creation keeps storing a concrete value, which is why reads need no deriving.
    created = ModelCreate.model_validate(
        {"name": "m1", "source": "huggingface", "huggingface_repo_id": "org/model"}
    )
    assert created.distributed_inference_across_workers is True

    custom = ModelCreate.model_validate(
        {
            "name": "m1",
            "source": "huggingface",
            "huggingface_repo_id": "org/model",
            "backend": "Custom",
        }
    )
    assert custom.distributed_inference_across_workers is False


def test_write_side_still_enforces_the_source_rule():
    # The rule did not disappear, it moved to where input is validated.
    for source in (
        SourceEnum.HUGGING_FACE,
        SourceEnum.MODEL_SCOPE,
        SourceEnum.LOCAL_PATH,
    ):
        with pytest.raises(ValidationError):
            ModelSource.model_validate({"source": source})

    with pytest.raises(ValidationError):
        ModelCreate.model_validate({"name": "m1", "source": "huggingface"})
