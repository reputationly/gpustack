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


# --------------------------------------------------------------------------- #
# Providers without credentials must be usable end to end, not just creatable.
# --------------------------------------------------------------------------- #
def test_provider_auth_headers_skips_empty_credentials():
    """An empty token means "no auth header", not ``Bearer `` with a trailing space.

    Some servers reject the malformed header outright, before looking at the
    request — which would make a credential-less provider look broken in a way that
    points at the wrong thing.
    """
    from gpustack.routes.model_provider import provider_auth_headers
    from gpustack.schemas.model_provider import ModelProviderTypeEnum

    class _Config:
        def __init__(self, provider_type):
            self.type = provider_type
            self.claudeVersion = None

    openai = ModelProviderTypeEnum.OPENAI
    claude = ModelProviderTypeEnum.CLAUDE

    assert provider_auth_headers(_Config(openai), "sk-x") == {
        "Authorization": "Bearer sk-x"
    }
    assert provider_auth_headers(_Config(openai), None) == {}
    assert provider_auth_headers(_Config(openai), "") == {}

    headers = provider_auth_headers(_Config(claude), "sk-a")
    assert headers["X-API-Key"] == "sk-a"
    assert headers["anthropic-version"]

    # No credential → only the version header remains; nothing claims to be a key.
    assert "X-API-Key" not in provider_auth_headers(_Config(claude), None)


# --------------------------------------------------------------------------- #
# The /get-models route must be wired to the query handler (regression: a helper
# inserted next to it once stole the decorator, so the endpoint demanded query
# params and the UI's JSON body got a 422 — the credential-less provider feature
# was unreachable from the console).
# --------------------------------------------------------------------------- #
def _provider_app():
    from fastapi import FastAPI

    from gpustack.routes.model_provider import router as provider_router

    app = FastAPI()
    app.include_router(provider_router, prefix="/v1/model-providers")
    return app


def test_get_models_route_is_registered_on_the_query_handler():
    """The path takes a JSON body; a handler with query params means mis-wiring."""
    from gpustack.routes.model_provider import router as provider_router

    endpoints = {
        route.path: route.endpoint.__name__
        for route in provider_router.routes
        if getattr(route, "path", "").endswith("get-models")
    }
    assert endpoints["/get-models"] == "get_models_from_provider"

    operation = _provider_app().openapi()["paths"]["/v1/model-providers/get-models"][
        "post"
    ]
    assert "requestBody" in operation
    assert operation.get("parameters") in (None, [])


def test_get_models_route_calls_the_provider_without_credentials(monkeypatch):
    """A provider with no token must reach the upstream with no auth header."""
    from fastapi.testclient import TestClient

    from gpustack.routes import model_provider as module

    captured = {}

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "hy4-flash"}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", _Client)
    client = TestClient(_provider_app())
    response = client.post(
        "/v1/model-providers/get-models",
        json={
            "config": {"type": "openai", "openaiCustomUrl": "http://10.0.0.5:8000/v1"}
        },
    )

    assert response.status_code == 200, response.text
    # 无凭据 → 不发 Authorization(而不是发 "Bearer ")
    assert captured["headers"] == {}


# --------------------------------------------------------------------------- #
# Preview calls must use the credentials the saved provider would use.
# --------------------------------------------------------------------------- #
def test_resolve_api_token_distinguishes_cleared_from_untouched():
    """``""`` = call without credentials; omitted = reuse the stored token."""
    from gpustack.routes.model_provider import resolve_api_token
    from gpustack.schemas.model_provider import ProviderModelsInput

    cleared = ProviderModelsInput.model_validate({"api_token": ""})
    assert "api_token" in cleared.model_fields_set
    assert resolve_api_token(cleared, "sk-stored") is None

    untouched = ProviderModelsInput.model_validate({})
    assert resolve_api_token(untouched, "sk-stored") == "sk-stored"
    assert resolve_api_token(untouched, None) is None

    explicit = ProviderModelsInput.model_validate({"api_token": "sk-typed"})
    assert resolve_api_token(explicit, "sk-stored") == "sk-typed"


def test_effective_api_tokens_is_what_validation_must_count():
    """A PUT that omits api_tokens keeps the row's tokens — validate that count."""
    from gpustack.routes.model_provider import effective_api_tokens
    from gpustack.schemas.model_provider import ModelProviderUpdate

    omitted = ModelProviderUpdate.model_validate(
        {"name": "p", "config": {"type": "openai"}}
    )
    assert len(effective_api_tokens(omitted, ["a", "b"])) == 2

    cleared = ModelProviderUpdate.model_validate(
        {"name": "p", "config": {"type": "openai"}, "api_tokens": []}
    )
    assert effective_api_tokens(cleared, ["a", "b"]) == []

    replaced = ModelProviderUpdate.model_validate(
        {"name": "p", "config": {"type": "openai"}, "api_tokens": [{"input": "sk-new"}]}
    )
    assert len(effective_api_tokens(replaced, ["a", "b"])) == 1


def test_validate_provider_uses_the_effective_token_count():
    """Dropping the llm model is only legal if the stored tokens drop to one too."""
    from gpustack.api.exceptions import InvalidException
    from gpustack.routes.model_provider import validate_provider
    from gpustack.schemas.model_provider import ModelProviderUpdate

    body = ModelProviderUpdate.model_validate(
        {
            "name": "p",
            "config": {"type": "openai"},
            "models": [{"name": "m1", "category": "embedding"}],
        }
    )
    # 省略 api_tokens + 库里有两个 → 仍然有 failover,必须要求 llm 模型
    with pytest.raises(InvalidException) as exc:
        validate_provider(body, effective_token_count=2)
    assert "llm model" in str(exc.value) or "llm model" in exc.value.message

    # 同一份请求,但实际只留一个 token → 不需要 llm 模型
    validate_provider(body, effective_token_count=1)
