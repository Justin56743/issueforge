from unittest.mock import patch

import pytest
from typer.testing import CliRunner
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_loopback_binding_needs_no_token():
    from issueforge.main import assert_safe_binding

    assert_safe_binding("127.0.0.1", None)
    assert_safe_binding("localhost", None)
    assert_safe_binding("::1", None)


def test_public_binding_without_a_token_refuses_to_start():
    from issueforge.main import assert_safe_binding

    with pytest.raises(SystemExit) as exc:
        assert_safe_binding("0.0.0.0", None)
    assert "FORGE_AUTH_TOKEN" in str(exc.value)


def test_public_binding_with_a_token_is_allowed():
    from issueforge.main import assert_safe_binding

    assert_safe_binding("0.0.0.0", "a-long-random-token")


def test_default_host_is_loopback():
    # The field default, not the live singleton: a developer's .env may set FORGE_HOST.
    from issueforge.config import Settings

    assert Settings.model_fields["forge_host"].default in ("127.0.0.1", "localhost")


def test_start_warns_about_agit_leftovers(tmp_path, monkeypatch):
    from issueforge.main import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("issueforge.main.Path.home", lambda: tmp_path)
    (tmp_path / ".env").write_text("AGIT_HOST=0.0.0.0\n")
    with patch("issueforge.main.uvicorn.run"):
        result = CliRunner().invoke(cli, ["start", "--host", "127.0.0.1", "--port", "8000", "--no-reload"])
    assert result.exit_code == 0
    assert "AGIT_HOST" in result.output and "Upgrading from Agit" in result.output


def test_start_binds_given_host_and_refuses_public_without_token():
    from issueforge.main import cli

    runner = CliRunner()

    # Refusal first: a successful start installs an auto-token, which would then satisfy the gate.
    with patch("issueforge.main.uvicorn.run") as mock_run:
        result = runner.invoke(cli, ["start", "--host", "0.0.0.0"])
        assert result.exit_code != 0
        mock_run.assert_not_called()

    with patch("issueforge.main.uvicorn.run") as mock_run:
        result = runner.invoke(cli, ["start", "--host", "127.0.0.1", "--port", "8000", "--no-reload"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            "issueforge.main:app", host="127.0.0.1", port=8000, reload=False
        )


def test_load_or_create_token_is_owner_only_and_stable(tmp_path):
    from issueforge.web.auth import load_or_create_token

    path = tmp_path / "vault" / "auth_token"
    token = load_or_create_token(path)
    assert len(token) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_or_create_token(path) == token


def test_start_without_a_token_creates_one_and_prints_the_login_url():
    import os

    from issueforge.config import settings
    from issueforge.main import cli

    with patch("issueforge.main.uvicorn.run"):
        result = CliRunner().invoke(cli, ["start", "--host", "127.0.0.1", "--port", "8000", "--no-reload"])
    assert result.exit_code == 0
    token = settings.forge_auth_token
    assert token and os.environ["FORGE_AUTH_TOKEN"] == token
    assert (settings.forge_vault_root / "auth_token").read_text().strip() == token
    assert f"http://127.0.0.1:8000/?token={token}" in result.output


def _client_with_token(monkeypatch, token):
    from issueforge.config import settings
    from issueforge.main import create_app

    monkeypatch.setattr(settings, "forge_auth_token", token)
    return TestClient(create_app())


def test_no_token_configured_means_no_gate(monkeypatch):
    client = _client_with_token(monkeypatch, None)
    assert client.get("/api/tasks").status_code == 200


def test_request_without_token_is_rejected(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    assert client.get("/api/tasks").status_code == 401


def test_bearer_header_is_accepted(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    res = client.get("/api/tasks", headers={"Authorization": "Bearer secret-token"})
    assert res.status_code == 200


def test_query_parameter_is_accepted_and_sets_a_cookie(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    res = client.get("/?token=secret-token")
    assert res.status_code == 200
    assert client.cookies.get("forge_token") == "secret-token"


def test_wrong_token_is_rejected(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    assert client.get("/api/tasks?token=wrong").status_code == 401


def test_webhook_with_secret_configured_is_gated_by_its_own_signature_check(monkeypatch):
    from issueforge.config import settings

    client = _client_with_token(monkeypatch, "secret-token")
    monkeypatch.setattr(settings, "github_webhook_secret", "hook-secret")
    res = client.post("/api/webhooks/github", json={}, headers={"X-GitHub-Event": "issues"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid webhook signature."
    assert "Unauthorized." not in res.text


def test_webhook_without_secret_configured_is_gated_by_the_token(monkeypatch):
    from issueforge.config import settings

    client = _client_with_token(monkeypatch, "secret-token")
    monkeypatch.setattr(settings, "github_webhook_secret", None)
    res = client.post("/api/webhooks/github", json={}, headers={"X-GitHub-Event": "issues"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Unauthorized."


def test_websocket_with_hostile_origin_is_refused_even_with_a_valid_token(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/ws/tasks/some-id/terminal?token=secret-token",
            headers={"Origin": "https://evil.example"},
        ):
            pass
    assert exc.value.code == 1008


def test_sandbox_shell_route_is_gone(monkeypatch):
    """No UI reached it, yet it opened a login shell for anyone holding the token."""
    client = _client_with_token(monkeypatch, "secret-token")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/tasks/some-id/sandbox-shell?token=secret-token"):
            pass


def test_terminal_websocket_without_token_is_refused(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/tasks/some-id/terminal"):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("headers", [{"Origin": "http://testserver"}, {}])
def test_terminal_websocket_with_token_and_same_or_no_origin_is_accepted(monkeypatch, headers):
    # The terminal route only attaches to a scrollback buffer; no process is spawned. get_task is
    # stubbed because an aiosqlite call from TestClient's WS portal loop hangs its teardown.
    from unittest.mock import AsyncMock

    client = _client_with_token(monkeypatch, "secret-token")
    monkeypatch.setattr("issueforge.web.api.get_task", AsyncMock(return_value=None))
    with client.websocket_connect("/ws/tasks/some-id/terminal?token=secret-token", headers=headers) as ws:
        ws.send_text('{"type": "resize", "cols": 80, "rows": 24}')


def test_non_ascii_token_is_rejected_cleanly(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    res = client.get("/api/tasks?token=%C3%A9")
    assert res.status_code == 401
    assert res.json()["detail"] == "Unauthorized."


def test_gitlab_webhook_token_compare(monkeypatch):
    from issueforge.config import settings
    from issueforge.git.gitlab_client import GitLabClient

    monkeypatch.setattr(settings, "gitlab_webhook_secret", "hook-secret")
    assert GitLabClient.verify_webhook_token("hook-secret")
    assert not GitLabClient.verify_webhook_token("wrong")
    assert not GitLabClient.verify_webhook_token(None)
