import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from issueforge.git.gitlab_client import GitLabClient


@pytest.mark.asyncio
async def test_gitlab_client_create_issue():
    gl = GitLabClient(token="gl-mock-token")
    mock_response = MagicMock(status_code=201, json=lambda: {
        "id": 555,
        "iid": 12,
        "title": "Refactor Codebase",
        "description": "Codebase cleanup",
        "state": "opened",
        "web_url": "https://gitlab.com/org/repo/-/issues/12"
    })

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)) as mock_post:
        result = await gl.create_issue(
            project_path="org/repo",
            title="Refactor Codebase",
            description="Codebase cleanup",
            labels=["refactor", "cleanup"],
            assignee_ids=[40084584]
        )

        assert result is not None
        assert result["iid"] == 12
        assert result["title"] == "Refactor Codebase"

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "/api/v4/projects/org%2Frepo/issues" in args[0]
        assert kwargs["json"]["title"] == "Refactor Codebase"
        assert kwargs["json"]["labels"] == "refactor,cleanup"
        assert kwargs["json"]["assignee_ids"] == [40084584]
