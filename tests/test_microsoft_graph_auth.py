from pathlib import Path

import pytest

from integrations.microsoft_graph_auth import (
    MicrosoftGraphAuth,
    MicrosoftGraphAuthError,
)


class FakeCache:
    def __init__(self) -> None:
        self.has_state_changed = False
        self.loaded = None

    def deserialize(self, value: str) -> None:
        self.loaded = value

    def serialize(self) -> str:
        return "serialized-cache"


class FakeApp:
    def __init__(
        self,
        *,
        silent_result=None,
        flow_result=None,
        device_result=None,
        accounts=None,
        **kwargs,
    ) -> None:
        self.silent_result = silent_result
        self.flow_result = flow_result or {
            "user_code": "fixture-code",
            "message": "Use o código de teste.",
        }
        self.device_result = device_result
        self.accounts = (
            [{"home_account_id": "cached-account"}]
            if accounts is None
            else accounts
        )
        self.silent_calls = 0
        self.device_flow_calls = 0
        self.device_token_calls = 0

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        self.silent_calls += 1
        return self.silent_result

    def initiate_device_flow(self, scopes):
        self.device_flow_calls += 1
        return self.flow_result

    def acquire_token_by_device_flow(self, flow):
        self.device_token_calls += 1
        return self.device_result


def build_auth(tmp_path: Path, app: FakeApp, cache: FakeCache, output=lambda _: None):
    return MicrosoftGraphAuth(
        client_id="client-id",
        authority="https://login.microsoftonline.com/tenant-id",
        scopes="User.Read Files.Read",
        token_cache_file=tmp_path / "ms_graph_token_cache.json",
        output=output,
        app_factory=lambda **kwargs: app,
        cache_factory=lambda: cache,
    )


def test_uses_silent_cached_token_without_device_flow(tmp_path: Path) -> None:
    cache_file = tmp_path / "ms_graph_token_cache.json"
    cache_file.write_text("existing-cache", encoding="utf-8")
    cache = FakeCache()
    app = FakeApp(silent_result={"access_token": "cached-token"})

    token = build_auth(tmp_path, app, cache).acquire_access_token()

    assert token == "cached-token"
    assert cache.loaded == "existing-cache"
    assert app.silent_calls == 1
    assert app.device_flow_calls == 0


def test_uses_device_flow_when_silent_token_is_unavailable(tmp_path: Path) -> None:
    cache = FakeCache()
    cache.has_state_changed = True
    app = FakeApp(silent_result=None, device_result={"access_token": "device-token"})
    messages = []

    token = build_auth(tmp_path, app, cache, messages.append).acquire_access_token()

    assert token == "device-token"
    assert app.silent_calls == 1
    assert app.device_flow_calls == 1
    assert app.device_token_calls == 1
    assert messages == ["Use o código de teste."]
    assert (tmp_path / "ms_graph_token_cache.json").read_text() == "serialized-cache"


def test_reports_error_returned_by_initiate_device_flow(tmp_path: Path) -> None:
    app = FakeApp(
        silent_result=None,
        accounts=[],
        flow_result={
            "error": "invalid_request",
            "error_description": "Device flow não pôde ser iniciado.",
            "correlation_id": "correlation-initiate-123",
        },
    )

    with pytest.raises(MicrosoftGraphAuthError) as captured:
        build_auth(tmp_path, app, FakeCache()).acquire_access_token()

    message = str(captured.value)
    assert "error: invalid_request" in message
    assert "error_description: Device flow não pôde ser iniciado." in message
    assert "correlation_id: correlation-initiate-123" in message
    assert app.device_token_calls == 0


def test_reports_error_returned_by_device_flow_completion(tmp_path: Path) -> None:
    app = FakeApp(
        silent_result=None,
        device_result={
            "error": "authorization_declined",
            "error_description": "O usuário recusou a autenticação.",
            "correlation_id": "correlation-completion-456",
            "refresh_token": "must-not-be-rendered",
        },
    )

    with pytest.raises(MicrosoftGraphAuthError) as captured:
        build_auth(tmp_path, app, FakeCache()).acquire_access_token()

    message = str(captured.value)
    assert "error: authorization_declined" in message
    assert "error_description: O usuário recusou a autenticação." in message
    assert "correlation_id: correlation-completion-456" in message
    assert "must-not-be-rendered" not in message
    assert app.device_token_calls == 1
