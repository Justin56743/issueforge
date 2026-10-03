import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from issueforge.agents.engine import IssueforgeAgentEngine
from issueforge.core.database import init_db, save_task
from issueforge.core.models import (
    PipelineRun,
    PlatformType,
    ReviewSummary,
    Task,
    TaskStatus,
    TaskType,
)
from issueforge.git.github_client import GitHubClient
from issueforge.git.gitlab_client import GitLabClient


@pytest.mark.asyncio
async def test_github_client_close_issue():
    gh = GitHubClient(token="gh-test-token")
    mock_resp = MagicMock(status_code=200, json=lambda: {"state": "closed", "number": 42})

    with patch("httpx.AsyncClient.patch", new=AsyncMock(return_value=mock_resp)) as mock_patch:
        res = await gh.close_issue("owner", "repo", 42)
        assert res is True
        mock_patch.assert_called_once()
        args, kwargs = mock_patch.call_args
        assert "/repos/owner/repo/issues/42" in args[0]
        assert kwargs["json"] == {"state": "closed"}


@pytest.mark.asyncio
async def test_gitlab_client_close_issue():
    gl = GitLabClient(token="gl-test-token")
    mock_resp = MagicMock(status_code=200, json=lambda: {"state": "closed", "iid": 101})

    with patch("httpx.AsyncClient.put", new=AsyncMock(return_value=mock_resp)) as mock_put:
        res = await gl.close_issue("group/my-project", 101)
        assert res is True
        mock_put.assert_called_once()
        args, kwargs = mock_put.call_args
        assert "/issues/101" in args[0]
        assert kwargs["json"]["state_event"] == "close"


@pytest.mark.asyncio
async def test_engine_push_leaves_github_issue_for_closes_keyword(tmp_path):
    await init_db()
    engine = IssueforgeAgentEngine()

    task = Task(
        id="test-gh-close-task",
        title="Fix bug in auth service",
        description="Fix the JWT expiration bug",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/octocat/hello-world.git",
        repo_name="octocat/hello-world",
        working_branch="forge/patch-auth",
        issue_number=88,
        active_run_id="run-1",
        runs=[PipelineRun(run_id="run-1", attempt_number=1, status=TaskStatus.PUSHING)],
        status=TaskStatus.PUSHING,
        review_summary=ReviewSummary(
            summary="Auth service fix",
            risk_assessment="Low risk",
            test_verification="All tests pass",
            suggested_commit_message="fix: resolve JWT expiration",
            suggested_pr_title="fix: resolve JWT expiration",
            suggested_pr_body="Resolved token expiration edge cases.",
            suggested_labels=["bug", "security"]
        )
    )
    await save_task(task)

    with patch("issueforge.git.repo_manager.GitRepoManager.commit_changes", new=AsyncMock(return_value=True)), \
         patch("issueforge.agents.engine.MergeConflictResolver.execute_resolution_pipeline", new=AsyncMock(return_value=(True, "clean"))), \
         patch("issueforge.git.repo_manager.GitRepoManager.push_working_branch", new=AsyncMock(return_value=True)), \
         patch.object(engine.github_client, "create_pull_request", new=AsyncMock(return_value={"html_url": "https://github.com/octocat/hello-world/pull/1", "number": 1})) as mock_create_pr, \
         patch.object(engine.github_client, "update_issue_labels", new=AsyncMock(return_value=True)) as mock_update_labels, \
         patch.object(engine.github_client, "close_issue", new=AsyncMock(return_value=True)) as mock_close_issue, \
         patch.object(engine, "post_completion_comment", new=AsyncMock(return_value=True)):

        completed_task = await engine.push_and_create_pr(task.id)

        assert completed_task.status == TaskStatus.COMPLETED
        # Verify Closes #88 was added to the PR body
        mock_create_pr.assert_called_once()
        pr_call_kwargs = mock_create_pr.call_args[1]
        assert "Closes #88" in pr_call_kwargs["body"]
        # Verify labels updated
        mock_update_labels.assert_called_once_with("octocat", "hello-world", 88, ["bug", "security"])
        # The issue closes when the PR merges (via "Closes #88"), not when the branch is pushed.
        mock_close_issue.assert_not_called()


@pytest.mark.asyncio
async def test_engine_push_leaves_gitlab_issue_for_closes_keyword(tmp_path):
    await init_db()
    engine = IssueforgeAgentEngine()

    task = Task(
        id="test-gl-close-task",
        title="Add analytics endpoint",
        description="Telemetry endpoint for dashboard",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITLAB,
        repo_url="https://gitlab.com/acme/backend.git",
        repo_name="acme/backend",
        working_branch="forge/feature-analytics",
        issue_number=142,
        active_run_id="run-1",
        runs=[PipelineRun(run_id="run-1", attempt_number=1, status=TaskStatus.PUSHING)],
        status=TaskStatus.PUSHING,
        review_summary=ReviewSummary(
            summary="Analytics endpoint",
            risk_assessment="Low risk",
            test_verification="All tests pass",
            suggested_commit_message="feat: add telemetry endpoints",
            suggested_pr_title="feat: add telemetry endpoints",
            suggested_pr_body="Adds prometheus metric exporters.",
            suggested_labels=["feature"]
        )
    )
    await save_task(task)

    with patch("issueforge.git.repo_manager.GitRepoManager.commit_changes", new=AsyncMock(return_value=True)), \
         patch("issueforge.agents.engine.MergeConflictResolver.execute_resolution_pipeline", new=AsyncMock(return_value=(True, "clean"))), \
         patch("issueforge.git.repo_manager.GitRepoManager.push_working_branch", new=AsyncMock(return_value=True)), \
         patch.object(engine.gitlab_client, "create_merge_request", new=AsyncMock(return_value={"web_url": "https://gitlab.com/acme/backend/-/merge_requests/5", "iid": 5})) as mock_create_mr, \
         patch.object(engine.gitlab_client, "update_issue_metadata", new=AsyncMock(return_value={"id": 142})) as mock_update_meta, \
         patch.object(engine.gitlab_client, "close_issue", new=AsyncMock(return_value=True)) as mock_close_issue, \
         patch.object(engine, "post_completion_comment", new=AsyncMock(return_value=True)):

        completed_task = await engine.push_and_create_pr(task.id)

        assert completed_task.status == TaskStatus.COMPLETED
        # Verify Closes #142 was appended to MR description
        mock_create_mr.assert_called_once()
        mr_call_kwargs = mock_create_mr.call_args[1]
        assert "Closes #142" in mr_call_kwargs["description"]
        # Verify issue metadata/labels updated
        mock_update_meta.assert_called_once_with("acme/backend", 142, labels=["feature"])
        mock_close_issue.assert_not_called()
