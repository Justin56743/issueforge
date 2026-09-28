import hashlib
import hmac
import json
import pytest
from httpx import ASGITransport, AsyncClient

from issueforge.config import settings
from issueforge.core.database import init_db
from issueforge.core.models import TaskStatus
from issueforge.git.github_client import GitHubClient
from issueforge.git.gitlab_client import GitLabClient
from issueforge.main import app


def test_github_signature_verification():
    settings.github_webhook_secret = "my-secret-webhook-key"
    payload = b'{"action": "opened"}'

    # Compute valid signature
    mac = hmac.new(b"my-secret-webhook-key", msg=payload, digestmod=hashlib.sha256)
    valid_header = f"sha256={mac.hexdigest()}"

    assert GitHubClient.verify_webhook_signature(payload, valid_header)
    assert not GitHubClient.verify_webhook_signature(payload, "sha256=invalid")
    assert not GitHubClient.verify_webhook_signature(payload, None)

    # Disable secret -> always valid
    settings.github_webhook_secret = None
    assert GitHubClient.verify_webhook_signature(payload, None)


def test_gitlab_token_verification():
    settings.gitlab_webhook_secret = "gl-secret-token"
    assert GitLabClient.verify_webhook_token("gl-secret-token")
    assert not GitLabClient.verify_webhook_token("wrong-token")

    settings.gitlab_webhook_secret = None
    assert GitLabClient.verify_webhook_token(None)


@pytest.mark.asyncio
async def test_github_issue_webhook_endpoint():
    await init_db()
    settings.github_webhook_secret = None  # Skip HMAC check in test

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "action": "opened",
            "issue": {
                "number": 42,
                "title": "Fix database connection leak",
                "body": "Connections are not released on exception."
            },
            "repository": {
                "clone_url": "https://github.com/acme/backend.git",
                "default_branch": "main"
            },
            "sender": {
                "login": "octocat"
            }
        }
        resp = await client.post(
            "/api/webhooks/github",
            json=payload,
            headers={"X-GitHub-Event": "issues"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ingested"
        assert "issue-42" in data["task_id"]


@pytest.mark.asyncio
async def test_gitlab_issue_webhook_endpoint():
    await init_db()
    settings.gitlab_webhook_secret = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "object_kind": "issue",
            "object_attributes": {
                "iid": 88,
                "title": "Add Prometheus metrics endpoint",
                "description": "Expose /metrics for monitoring.",
                "action": "open"
            },
            "project": {
                "git_http_url": "https://gitlab.com/acme/infra.git",
                "default_branch": "main"
            },
            "user": {
                "username": "developer"
            }
        }
        resp = await client.post(
            "/api/webhooks/gitlab",
            json=payload,
            headers={"X-Gitlab-Event": "Issue Hook"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ingested"
        assert "issue-88" in data["task_id"]
