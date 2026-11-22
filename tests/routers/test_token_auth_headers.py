"""ext-auth must not hand external providers a GPUStack credential.

``/token-auth`` answers Higress's ext-auth call, and the plugin copies every listed
upstream header onto the request (``allowed_upstream_headers`` in the plugin spec).
A provider-backed route has no local worker to authenticate to, so it resolves to no
registration token — but a placeholder (``static_token_not_found``) used to be
returned anyway, and the AI proxy does not override it when the provider has no
apiTokens of its own. External providers therefore received a bogus
``Authorization: Bearer ...`` credential and some answered 400/401.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

from gpustack.routes import token
from gpustack.routes.token import token_auth_headers


def test_provider_route_carries_no_authorization_header():
    headers = token_auth_headers(
        consumer="ak.user", registration_token=None, cache_token="jwt"
    )
    # 关键断言:没有 Authorization,既不是占位串,也不是空值
    assert "Authorization" not in headers
    # 其余 ext-auth 契约头照旧
    assert headers["X-Mse-Consumer"] == "ak.user"
    assert headers["cookie"] == "dummy=dummy"
    assert len(headers) == 3


def test_local_route_still_carries_the_registration_token():
    headers = token_auth_headers(
        consumer="ak.user", registration_token="reg-token", cache_token="jwt"
    )
    assert headers["Authorization"] == "Bearer reg-token"


def test_placeholder_token_is_gone_from_the_service():
    """The placeholder must not come back: it is a credential-looking value."""
    import inspect

    from gpustack.server import services

    source = inspect.getsource(services.ModelRouteService.get_model_auth_info_by_name)
    # 注释里提到这个字符串是允许的(解释历史),但不许再有"给它赋值"的默认值。
    assert '= "static_token_not_found"' not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cached_token", "authorization"),
    [(None, None), ("reg-token", "Bearer reg-token"), ("static_token_not_found", None)],
)
async def test_server_auth_cache_matches_cold_path(
    monkeypatch, cached_token, authorization
):
    legacy_cache = cached_token == "static_token_not_found"
    jwt_manager = Mock()
    jwt_manager.decode_jwt_data.return_value = {
        "model": "model",
        "consumer": "none",
        "token": cached_token,
    }
    jwt_manager.create_token.return_value = "refreshed"
    monkeypatch.setattr(
        token,
        "authenticate_request",
        AsyncMock(side_effect=token.credentials_exception()),
    )
    monkeypatch.setattr(token, "gateway_token_auth", Mock())
    get_auth = AsyncMock(return_value=(token.AccessPolicyEnum.PUBLIC, None))
    monkeypatch.setattr(
        token,
        "ModelRouteService",
        Mock(return_value=SimpleNamespace(get_model_auth_info_by_name=get_auth)),
    )
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-higress-llm-model", b"model"),
                (token.AUTH_CACHE_HEADER.encode(), b"cached"),
            ],
            "app": SimpleNamespace(state=SimpleNamespace(jwt_manager=jwt_manager)),
        }
    )

    response = await token.server_auth(request, None)

    assert response.headers.get("Authorization") == authorization
    assert response.headers["X-Mse-Consumer"] == "none"
    assert response.headers["cookie"] == "dummy=dummy"
    assert response.headers[token.AUTH_CACHE_HEADER] == (
        "refreshed" if legacy_cache else "cached"
    )
    if legacy_cache:
        get_auth.assert_awaited_once_with("model")
    else:
        get_auth.assert_not_awaited()
