"""S95.3 RED — create-post is gated by the core API-token guard (X-API-Key).

No header → 401; bad/inactive token → 401; valid token without the
``loopai:posts:create`` scope → 403; valid + scoped → reaches the handler with
the key's ``g.user_id``. Exercised against the real ``require_api_key`` decorator
with its DB collaborators monkeypatched (a true unit test — no Postgres).
"""
from importlib import import_module
from unittest.mock import MagicMock

import pytest
from flask import Flask

import vbwd.middleware.api_key_auth as api_key_auth

routes_module = import_module("plugins.loopai-adapter.loopai_adapter.routes")
plugin_module = import_module("plugins.loopai-adapter")

CREATE_POST_URL = "/api/v1/loopai-adapter/create-post"


class _FakeKey:
    def __init__(self, user_id, scopes):
        self.user_id = user_id
        self.scopes = scopes
        self.is_active = True


class _FakeApiKeyService:
    """Stand-in for ApiKeyService — token "good"/"good-noscope" are valid."""

    def __init__(self):
        self._keys = {
            "good": _FakeKey("user-42", ["loopai:posts:create"]),
            "good-noscope": _FakeKey("user-42", ["other:scope"]),
        }

    def verify(self, presented):
        return self._keys.get(presented)

    def is_ip_allowed(self, api_key, client_ip):
        return True

    def has_scope(self, api_key, scope):
        return scope in api_key.scopes

    def touch(self, api_key):
        pass


@pytest.fixture
def captured_user():
    return {}


@pytest.fixture
def client(monkeypatch, captured_user):
    monkeypatch.setattr(api_key_auth, "_resolve_api_key_service", _FakeApiKeyService)
    monkeypatch.setattr(api_key_auth, "_load_user", lambda user_id: {"id": user_id})

    ingest_service = MagicMock()

    def _record_ingest(payload, *, user_id):
        captured_user["user_id"] = user_id
        return {"id": "post-1"}

    ingest_service.ingest.side_effect = _record_ingest
    monkeypatch.setattr(
        routes_module, "_content_ingest_service", lambda: ingest_service
    )
    monkeypatch.setattr(routes_module, "_image_service", lambda: MagicMock())
    monkeypatch.setattr(routes_module, "_post_service", lambda: MagicMock())
    monkeypatch.setattr(
        routes_module,
        "_adapter_config",
        lambda: {"default_status": "published", "default_post_type": "post"},
    )

    app = Flask(__name__)
    app.register_blueprint(
        routes_module.loopai_adapter_bp,
        url_prefix=plugin_module.LoopaiAdapterPlugin().get_url_prefix(),
    )
    return app.test_client()


def test_no_api_key_returns_401(client):
    response = client.post(CREATE_POST_URL, json={"title": "T"})
    assert response.status_code == 401


def test_invalid_token_returns_401(client):
    response = client.post(
        CREATE_POST_URL, json={"title": "T"}, headers={"X-API-Key": "nope"}
    )
    assert response.status_code == 401


def test_valid_token_without_scope_returns_403(client):
    response = client.post(
        CREATE_POST_URL, json={"title": "T"}, headers={"X-API-Key": "good-noscope"}
    )
    assert response.status_code == 403


def test_valid_scoped_token_reaches_handler_with_user_id(client, captured_user):
    response = client.post(
        CREATE_POST_URL, json={"title": "T"}, headers={"X-API-Key": "good"}
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert captured_user["user_id"] == "user-42"


def test_plugin_declares_grantable_scope():
    scopes = plugin_module.LoopaiAdapterPlugin().api_scopes
    keys = {scope["key"]: scope for scope in scopes}
    assert "loopai:posts:create" in keys
    assert keys["loopai:posts:create"]["user_grantable"] is True
