"""``ModelFilePublic`` must not reject a stored download row.

``ModelFileBase`` inherits ``ModelSource.check_huggingface_fields``; a row whose
``source`` disagrees with the populated id/path columns (hand-edited, or a column
nulled during an external cleanup) failed to serialize, taking down
``GET /v1/model-files`` for every row instead of just being displayed.
"""

import pytest
from pydantic import ValidationError

from gpustack.schemas.model_files import ModelFile, ModelFileCreate, ModelFilePublic

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
    assert _decorator_count(ModelFilePublic) == 0


def test_public_field_set_matches_the_table():
    public = set(ModelFilePublic.model_fields)
    table = set(ModelFile.model_fields)
    # cluster_id / deleted_at / source_index are internal bookkeeping.
    assert table - public == {"cluster_id", "deleted_at", "source_index"}
    assert public - table == set()


def test_dirty_row_serializes():
    public = ModelFilePublic.model_validate(
        dict(
            ROW_META,
            source="huggingface",
            huggingface_repo_id=None,
            state="not-a-state",
        )
    )
    assert public.source == "huggingface"
    assert public.state == "not-a-state"


def test_write_side_still_rejects_a_source_without_its_fields():
    with pytest.raises(ValidationError):
        ModelFileCreate.model_validate(
            {"source": "huggingface", "worker_id": 1, "local_dir": "/x"}
        )
