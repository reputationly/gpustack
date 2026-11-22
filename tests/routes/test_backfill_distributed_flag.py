"""The NULL backfill must reproduce the rule the API used to apply on read.

``ModelPublic`` used to derive ``distributed_inference_across_workers`` while
serializing. That derivation is gone (it let the API claim "on" for a row the
scheduler read as NULL/false), and the value is now reported as stored — so any
legacy NULL has to be filled with **exactly** what the API used to show, or this
fix would silently change what operators see for existing models.

The migration keeps its own copy of the rule (no app imports, by design), so the
cases below are what keeps the two copies honest.
"""

import importlib.util
import os
from types import SimpleNamespace

MIGRATION = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "gpustack",
    "migrations",
    "versions",
    "2026_11_22_1400-d4b8e2a16c37_backfill_distributed_flag.py",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "backfill_distributed_flag", os.path.abspath(MIGRATION)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def _row(**overrides):
    base = dict(
        backend=None,
        # Python-side value form; the DB form is covered separately below.
        source="huggingface",
        huggingface_filename=None,
        model_scope_file_path=None,
        local_path=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_explicit_backend_wins():
    assert migration._derived_backend(_row(backend="vLLM")) == "vLLM"
    assert migration._derived_backend(_row(backend="Custom")) == "Custom"
    assert migration._derived_backend(_row(backend="SGLang")) == "SGLang"


def test_missing_backend_defaults_to_vllm():
    # get_backend(): no backend + not gguf -> vLLM, which is distributed-capable.
    assert migration._derived_backend(_row()) == "vLLM"
    assert "vLLM" in migration._DISTRIBUTED_BACKENDS


def test_gguf_without_backend_is_custom():
    assert (
        migration._derived_backend(_row(huggingface_filename="model-Q4_K_M.gguf"))
        == "Custom"
    )
    assert (
        migration._derived_backend(
            _row(source="model_scope", model_scope_file_path="m.gguf")
        )
        == "Custom"
    )
    assert (
        migration._derived_backend(_row(source="local_path", local_path="/x/m.gguf"))
        == "Custom"
    )
    assert "Custom" not in migration._DISTRIBUTED_BACKENDS


def test_non_gguf_local_and_scope_sources_fall_back_to_vllm():
    assert (
        migration._derived_backend(_row(source="local_path", local_path="/x/model"))
        == "vLLM"
    )
    assert (
        migration._derived_backend(
            _row(source="model_scope", model_scope_file_path="org/m/safetensors")
        )
        == "vLLM"
    )


def test_only_the_three_distributed_backends_are_true():
    assert migration._DISTRIBUTED_BACKENDS == ("vLLM", "MindIE", "SGLang")
    for backend in ("Custom", "LightX2V", "IndexTTS", "VoxBox"):
        assert migration._derived_backend(_row(backend=backend)) == backend


def test_source_is_understood_in_both_spellings():
    """A live PostgreSQL reports the enum NAME (``HUGGING_FACE``), not the value.

    Verified against the production databases while writing this migration: the
    ``sourceenum`` column hands back ``HUGGING_FACE`` / ``LOCAL_PATH``, so comparing
    against the Python values would have silently skipped every gguf row and
    backfilled it as distributed=false→true the wrong way round.
    """
    assert migration._source_name("HUGGING_FACE") == "HUGGING_FACE"
    assert migration._source_name("huggingface") == "HUGGING_FACE"
    assert migration._source_name("MODEL_SCOPE") == "MODEL_SCOPE"
    assert migration._source_name("model_scope") == "MODEL_SCOPE"
    assert migration._source_name("LOCAL_PATH") == "LOCAL_PATH"
    assert migration._source_name("local_path") == "LOCAL_PATH"
    assert migration._source_name(None) == ""

    for spelling in ("HUGGING_FACE", "huggingface"):
        assert (
            migration._derived_backend(
                _row(source=spelling, huggingface_filename="m-Q4_K_M.gguf")
            )
            == "Custom"
        )
        assert migration._derived_backend(_row(source=spelling)) == "vLLM"
