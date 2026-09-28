import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

from issueforge.config import settings
from issueforge.core.database import get_task, init_db, save_task, save_question
from issueforge.core.models import (
    PlatformType,
    QuestionStatus,
    Task,
    TaskQuestion,
    TaskStatus,
    TaskType,
)
from issueforge.core.queue import task_queue
from issueforge.core.sandbox import NativeSandbox
from issueforge.git.github_client import GitHubClient
from issueforge.git.gitlab_client import GitLabClient
from issueforge.git.repo_manager import GitRepoManager
from issueforge.main import app


@pytest.mark.asyncio
async def test_git_repo_manager_remote_branches(tmp_path):
    sandbox = NativeSandbox("test-remote-branches")
    sandbox.setup()

    # Initialize local git repo
    await sandbox.run_command("git init")
    await sandbox.run_command('git config user.name "Test"')
    await sandbox.run_command('git config user.email "test@test.com"')
    sandbox.write_file("README.md", "# Test")
    await sandbox.run_command("git add .")
    await sandbox.run_command('git commit -m "init"')

    mgr = GitRepoManager(sandbox=sandbox, repo_url="https://github.com/org/repo.git", base_branch="main")
    
    # Mock git branch -r response
    with patch.object(
        sandbox,
        "run_command",
        new=AsyncMock(
            side_effect=[
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="  origin/HEAD -> origin/main\n  origin/main\n  origin/develop\n  origin/staging\n")
            ]
        )
    ):
        branches = await mgr.get_remote_branches()
        assert "main" in branches
        assert "develop" in branches
        assert "staging" in branches
        assert "HEAD" not in branches

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_gitlab_and_github_client_extensions():
    # Test GitLabClient
    gl = GitLabClient(token="gl-mock-token")
    mock_resp_notes = MagicMock(status_code=200, json=lambda: [{"id": 1, "body": "Discussion note", "system": False}])
    mock_resp_put = MagicMock(status_code=200, json=lambda: {"id": 10, "labels": ["Priority::High"]})
    mock_resp_branches = MagicMock(status_code=200, json=lambda: [{"name": "main"}, {"name": "develop"}])

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=[mock_resp_notes, mock_resp_branches])):
        with patch("httpx.AsyncClient.put", new=AsyncMock(return_value=mock_resp_put)):
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp_notes)):
                notes = await gl.list_issue_notes("group/repo", 10)
                assert len(notes) == 1
                assert notes[0]["body"] == "Discussion note"

                posted = await gl.post_issue_note("group/repo", 10, "Working on it")
                assert posted is True

                updated = await gl.update_issue_metadata("group/repo", 10, labels=["Priority::High"])
                assert updated is not None

                branches = await gl.get_project_branches("group/repo")
                assert "develop" in branches

    # Test GitHubClient
    gh = GitHubClient(token="gh-mock-token")
    mock_gh_comments = MagicMock(status_code=200, json=lambda: [{"id": 1, "body": "GH comment", "user": {"login": "alice"}}])
    mock_gh_put = MagicMock(status_code=200, json=lambda: [{"name": "bug"}])
    mock_gh_branches = MagicMock(status_code=200, json=lambda: [{"name": "main"}, {"name": "feat/login"}])

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=[mock_gh_comments, mock_gh_branches])):
        with patch("httpx.AsyncClient.put", new=AsyncMock(return_value=mock_gh_put)):
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=MagicMock(status_code=201))):
                comments = await gh.list_issue_comments("org", "repo", 5)
                assert len(comments) == 1
                assert comments[0]["body"] == "GH comment"

                posted = await gh.post_issue_comment("org", "repo", 5, "Hello")
                assert posted is True

                updated = await gh.update_issue_labels("org", "repo", 5, ["bug"])
                assert updated is True

                branches = await gh.get_repo_branches("org", "repo")
                assert "feat/login" in branches


@pytest.mark.asyncio
async def test_rest_api_target_branch_and_questions():
    await init_db()

    task = Task(
        id="test-api-branch-q",
        title="Fix payment gateway webhook",
        description="Handle signature mismatch.",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITLAB,
        repo_url="https://gitlab.com/org/repo.git",
        repo_name="org/repo",
        base_branch="main",
        working_branch="forge/branch-q-1",
        status=TaskStatus.AWAITING_CONFIRMATION,
        target_branch_candidates=["main", "staging", "develop"],
        selected_target_branch="main"
    )
    await save_task(task)

    q = TaskQuestion(
        id="q-api-1",
        task_id="test-api-branch-q",
        question="Should we retry on 5xx errors?",
        options=["Yes, up to 3 times", "No retry"],
        status=QuestionStatus.PENDING
    )
    await save_question(q)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Update target branch
        res_branch = await client.post(
            "/api/tasks/test-api-branch-q/target-branch",
            json={"branch": "staging"}
        )
        assert res_branch.status_code == 200
        data = res_branch.json()
        assert data["task"]["selected_target_branch"] == "staging"

        # 2. Fetch questions
        res_q = await client.get("/api/tasks/test-api-branch-q/questions")
        assert res_q.status_code == 200
        q_list = res_q.json()
        assert len(q_list) == 1
        assert q_list[0]["id"] == "q-api-1"

        # 3. Answer question
        res_ans = await client.post(
            "/api/tasks/test-api-branch-q/questions/q-api-1/answer",
            json={"answer": "Yes, up to 3 times", "selected_option": "Yes, up to 3 times"}
        )
        assert res_ans.status_code == 200
        assert res_ans.json()["status"] == "answered"

        # 4. List learnings
        res_learn = await client.get("/api/learnings")
        assert res_learn.status_code == 200
