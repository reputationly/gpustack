import pytest
from gpustack.routes.model_provider import determine_model_category
from gpustack.schemas.model_provider import ModelProviderTypeEnum
from gpustack.schemas.models import CategoryEnum

doubao_llm_data = {
    "created": 1767587883,
    "domain": "VLM",
    "features": {
        "batch": {"batch_chat": False, "batch_job": False},
        "cache": {"prefix_cache": False, "session_cache": False},
        "structured_outputs": {"json_object": True, "json_schema": True},
        "tools": {"function_calling": True},
    },
    "id": "doubao-seed-1-8-251228",
    "modalities": {
        "input_modalities": ["text", "image", "video"],
        "output_modalities": ["text"],
    },
    "name": "doubao-seed-1-8",
    "object": "model",
    "task_type": ["VisualQuestionAnswering", "TextGeneration"],
    "token_limits": {
        "context_window": 262144,
        "max_input_token_length": 229376,
        "max_output_token_length": 65536,
        "max_reasoning_token_length": 32768,
    },
    "version": "251228",
}

doubao_llm_data2 = {
    "created": 1736337657,
    "domain": "LLM",
    "features": {
        "cache": {
            "prefix_cache": True,
            "session_cache": True,
        },
        "structured_outputs": {
            "json_object": False,
            "json_schema": False,
        },
        "tools": {
            "function_calling": True,
        },
    },
    "id": "doubao-1-5-lite-32k-250115",
    "modalities": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "name": "doubao-1-5-lite-32k",
    "object": "model",
    "task_type": ["TextGeneration"],
    "token_limits": {"context_window": 32768, "max_output_token_length": 12288},
    "version": "250115",
}

doubao_embedding_data = {
    "created": 1715588483,
    "domain": "Embedding",
    "features": {},
    "id": "doubao-embedding-text-240515",
    "modalities": {"input_modalities": ["text"]},
    "name": "doubao-embedding",
    "object": "model",
    "status": "Retiring",
    "task_type": ["TextEmbedding"],
    "token_limits": {},
    "version": "text-240515",
}

qwen_llm_data = {
    "id": "qwen3-max-2026-01-23",
    "object": "model",
    "created": 1769481796,
    "owned_by": "system",
}

qwen_image_data = {
    "id": "qwen-image-edit-max",
    "object": "model",
    "created": 1768570977,
    "owned_by": "system",
}

qwen_llm_split_name_data = {
    "id": "siliconflow/deepseek-v3.2",
    "object": "model",
    "created": 1769611475,
    "owned_by": "system",
}


@pytest.mark.parametrize(
    "provider_type,model,expected",
    [
        # actual data from doubao
        (
            ModelProviderTypeEnum.DOUBAO,
            doubao_llm_data,
            [CategoryEnum.LLM.value],
        ),
        (
            ModelProviderTypeEnum.DOUBAO,
            doubao_llm_data2,
            [CategoryEnum.LLM.value],
        ),
        (
            ModelProviderTypeEnum.DOUBAO,
            doubao_embedding_data,
            [CategoryEnum.EMBEDDING.value],
        ),
        # actual data from qwen
        (
            ModelProviderTypeEnum.QWEN,
            qwen_image_data,
            [CategoryEnum.IMAGE.value],
        ),
        (
            ModelProviderTypeEnum.QWEN,
            qwen_llm_data,
            [CategoryEnum.LLM.value],
        ),
        (
            ModelProviderTypeEnum.QWEN,
            qwen_llm_split_name_data,
            [CategoryEnum.LLM.value],
        ),
        # actual data from deepseek
        (
            ModelProviderTypeEnum.DEEPSEEK,
            {
                "id": "deepseek-chat",
                "object": "model",
                "owned_by": "deepseek",
            },
            [CategoryEnum.LLM.value],
        ),
        (
            ModelProviderTypeEnum.DEEPSEEK,
            {
                "id": "deepseek-reasoner",
                "object": "model",
                "owned_by": "deepseek",
            },
            [CategoryEnum.LLM.value],
        ),
    ],
)
def test_determine_model_category(provider_type, model, expected):
    assert determine_model_category(provider_type, model) == expected


# --------------------------------------------------------------------------- #
# Provider credentials: read side must tolerate zero tokens, write side keeps
# its own rule, and an omitted field must never erase tokens.
#
# Regression context: a provider row with `api_tokens = []` (cleared in the UI,
# written by an old PUT that omitted the field, or fixed by hand in SQL) made
# `GET /v1/model-providers` answer 500 for the whole page —
# "api_tokens must be a non-empty list" — because the write-side validator lived
# on `ModelProviderUpdate`, which is also the base of `ModelProviderPublic`.
# --------------------------------------------------------------------------- #
def _public_payload(**overrides):
    payload = {
        "id": 1,
        "name": "p",
        "config": {"type": "openai"},
        "owner_principal_id": 1,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "api_tokens": [],
    }
    payload.update(overrides)
    return payload


def test_public_provider_with_no_tokens_is_serializable():
    from gpustack.schemas.model_provider import ModelProviderPublic

    assert ModelProviderPublic.model_validate(_public_payload()).api_tokens == []
    # A row whose api_tokens key is absent (model_dump with exclude) must work too.
    payload = _public_payload()
    payload.pop("api_tokens")
    assert ModelProviderPublic.model_validate(payload).api_tokens == []


def test_convert_to_public_class_masks_tokens_and_survives_empty():
    from gpustack.schemas.model_provider import ModelProvider

    empty = ModelProvider._convert_to_public_class(_public_payload())
    assert empty.api_tokens == []

    masked = ModelProvider._convert_to_public_class(
        _public_payload(api_tokens=["sk-secret"])
    )
    assert len(masked.api_tokens) == 1
    # Only the hash is exposed; the secret must never come back out.
    assert masked.api_tokens[0].input is None
    assert (
        masked.api_tokens[0].hash
        == __import__("hashlib").sha256(b"sk-secret").hexdigest()
    )


def test_create_accepts_a_provider_without_credentials():
    """Zero tokens is a valid provider, not a validation error.

    Production case: ``hy4-flash-local`` points at a local OpenAI-compatible
    endpoint that needs no auth. Requiring a token here meant such a provider could
    only be created by writing the row with a script — or by storing a dummy token,
    which the gateway then sends upstream as a real Authorization header.
    """
    from gpustack.schemas.model_provider import ModelProviderCreate

    for payload in (
        {"name": "p", "config": {"type": "openai"}, "api_tokens": []},
        {"name": "p", "config": {"type": "openai"}},  # 连字段都不传
    ):
        created = ModelProviderCreate.model_validate(payload)
        assert created.api_tokens == []


def test_create_still_validates_what_the_caller_does_send():
    from pydantic import ValidationError

    from gpustack.schemas.model_provider import ModelProviderCreate

    # 给了 token 就必须是合法的:空白会被拒
    with pytest.raises(ValidationError):
        ModelProviderCreate.model_validate(
            {
                "name": "p",
                "config": {"type": "openai"},
                "api_tokens": [{"input": "   "}],
            }
        )
    # 也不能同时给 input 和 hash
    with pytest.raises(ValidationError):
        ModelProviderCreate.model_validate(
            {
                "name": "p",
                "config": {"type": "openai"},
                "api_tokens": [{"input": "sk-x", "hash": "abc"}],
            }
        )

    ok = ModelProviderCreate.model_validate(
        {"name": "p", "config": {"type": "openai"}, "api_tokens": [{"input": "sk-x"}]}
    )
    assert len(ok.api_tokens) == 1

    # 克隆:空列表表示"从源 provider 复制",route 层用 existing_tokens 解析
    clone = ModelProviderCreate.model_validate(
        {
            "name": "p",
            "config": {"type": "openai"},
            "api_tokens": [],
            "clone_from_id": 7,
        }
    )
    assert clone.clone_from_id == 7 and clone.api_tokens == []


def test_update_distinguishes_omitted_tokens_from_cleared_tokens():
    # The PUT route applies tokens only when the field was actually sent.
    # `api_tokens` defaults to [], so `is not None` is always true — using that
    # as the guard is what silently wiped credentials on unrelated updates.
    from gpustack.routes.model_provider import submitted_api_tokens
    from gpustack.schemas.model_provider import ModelProviderUpdate

    omitted = ModelProviderUpdate.model_validate(
        {"name": "p", "config": {"type": "openai"}}
    )
    assert submitted_api_tokens(omitted) is None, "省略字段不能等于清空"

    cleared = ModelProviderUpdate.model_validate(
        {"name": "p", "config": {"type": "openai"}, "api_tokens": []}
    )
    assert submitted_api_tokens(cleared) == [], "显式 [] 才表示清空"

    replaced = ModelProviderUpdate.model_validate(
        {
            "name": "p",
            "config": {"type": "openai"},
            "api_tokens": [{"input": "sk-new"}],
        }
    )
    assert len(submitted_api_tokens(replaced)) == 1


def test_public_model_never_reapplies_write_rules():
    """A hand-edited row must still render: the read model carries fields only.

    Same defect class as the empty-token crash — the write rules (timeout > 0,
    proxy_timeout requires proxy_url) were enforced through inheritance on the
    public model, so a row that legitimately exists but violates an input rule
    took the whole list endpoint down instead of being displayed.
    """
    from gpustack.schemas.model_provider import ModelProviderPublic, ModelProviderUpdate

    assert ModelProviderPublic.model_validate(_public_payload(timeout=0)).timeout == 0
    assert (
        ModelProviderPublic.model_validate(
            _public_payload(proxy_timeout=5, proxy_url=None)
        ).proxy_timeout
        == 5
    )

    # ...while the write path still refuses to CREATE/UPDATE such a provider.
    from pydantic import ValidationError

    for bad in ({"timeout": 0}, {"proxy_timeout": 5}):
        with pytest.raises(ValidationError):
            ModelProviderUpdate.model_validate(
                {"name": "p", "config": {"type": "openai"}, **bad}
            )
