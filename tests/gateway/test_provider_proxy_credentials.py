"""Provider credentials must come from the dedicated column, never from `config`.

`config` is user-editable (the console merges its "advanced YAML" into it), and the
AI-proxy plugin config used to be assembled as ``{**config, "apiTokens": <column>}`` —
so a ``config.apiTokens`` key silently won over the real credential list. Clearing a
key in the console then left the gateway still using the stale key from YAML, and a
credential-less provider kept presenting one it no longer owned.
"""

from types import SimpleNamespace

import pytest

from gpustack.gateway.utils import provider_proxy_plugin_spec
from gpustack.schemas.model_provider import ModelProviderTypeEnum, OpenAIConfig


def _provider(*, provider_id: int, api_tokens, config_extras=None, models=None):
    config = {
        "type": "openai",
        "openaiCustomUrl": "http://10.0.0.5:8000/v1",
        **(config_extras or {}),
    }
    return SimpleNamespace(
        id=provider_id,
        name=f"p{provider_id}",
        api_tokens=list(api_tokens),
        models=models or [],
        config=OpenAIConfig.model_validate(config),
        proxy_url=None,
    )


def test_config_cannot_override_api_tokens():
    provider = _provider(
        provider_id=1,
        api_tokens=["sk-db"],
        config_extras={"apiTokens": ["sk-smuggled-from-yaml"]},
    )
    spec, _ = provider_proxy_plugin_spec(provider)
    assert spec[0]["apiTokens"] == ["sk-db"]


def test_credential_less_provider_reaches_the_gateway_with_no_tokens():
    # 空列 + 高级 YAML 里塞了 token → 仍然下发空列表(否则"表单清空 key"等于没清)
    provider = _provider(
        provider_id=2,
        api_tokens=[],
        config_extras={"apiTokens": ["sk-stale"]},
    )
    spec, _ = provider_proxy_plugin_spec(provider)
    assert spec[0]["apiTokens"] == []


# ``type`` 是判别器,config 里改不动(pydantic 直接拒),所以不在这一组里。
@pytest.mark.parametrize("key", ["id", "failover", "retryOnFailure"])
def test_other_reserved_keys_are_not_taken_from_config(key):
    provider = _provider(
        provider_id=3, api_tokens=["sk-db"], config_extras={key: "junk"}
    )
    spec, _ = provider_proxy_plugin_spec(provider)
    # 保留字段只来自 provider 行本身:id 是 registry 名、type 是枚举值、
    # failover / retryOnFailure 是插件默认值(不是 config 里的 junk)
    assert spec[0]["id"] == "provider-3"
    assert spec[0]["type"] == ModelProviderTypeEnum.OPENAI
    if key in ("failover", "retryOnFailure"):
        assert spec[0][key]["enabled"] is False


def test_normal_config_fields_still_come_through():
    provider = _provider(
        provider_id=4,
        api_tokens=[],
        config_extras={"openaiCustomUrl": "http://10.0.0.9:9000/v1"},
    )
    spec, _ = provider_proxy_plugin_spec(provider)
    assert spec[0]["openaiCustomUrl"] == "http://10.0.0.9:9000/v1"
