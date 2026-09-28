import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient

from issueforge.config import settings
from issueforge.core.database import init_db, list_tasks
from issueforge.core.models import PlatformType, TaskStatus, TaskType
from issueforge.core.poller import PollerService, poller_service
from issueforge.main import app


@pytest.mark.asyncio
async def test_poller_gitlab_sync_and_deduplication():
    await init_db()

    # Configure dummy token
    settings.gitlab_token = "glpat-test-dummy-token"
    settings.auto_mark_todo_done = True

    repo_url = f"https://gitlab.com/testgroup/my-backend-{uuid.uuid4().hex[:6]}.git"
    web_url = repo_url.replace(".git", "")

    mock_todos = [
        {
            "id": 101,
            "project": {
                "id": 42,
                "web_url": web_url,
                "http_url_to_repo": repo_url,
                "default_branch": "main",
            },
            "author": {"username": "tech_lead"},
            "action_name": "assigned",
            "target_type": "Issue",
            "target": {
                "id": 991,
                "iid": 12,
                "title": "Optimize SQL queries for dashboard",
                "description": "Slow query times on task listing.",
            },
            "body": "Optimize SQL queries for dashboard",
            "state": "pending",
        },
        {
            "id": 102,
            "project": {
                "id": 42,
                "web_url": web_url,
                "http_url_to_repo": repo_url,
                "default_branch": "main",
            },
            "author": {"username": "qa_eng"},
            "action_name": "assigned",
            "target_type": "MergeRequest",
            "target": {
                "id": 992,
                "iid": 34,
                "title": "Fix memory leak in background worker",
                "description": "Please review and merge.",
            },
            "body": "Fix memory leak in background worker",
            "state": "pending",
        },
    ]

    service = PollerService()

    with patch.object(service.gitlab_client, "get_pending_todos", AsyncMock(return_value=mock_todos)), \
         patch.object(service.gitlab_client, "mark_todo_as_done", AsyncMock(return_value=True)) as mock_mark:

        # 1st sync: should ingest 2 tasks
        tasks = await service.sync_gitlab_todos()
        assert len(tasks) == 2

        task_issue = next(t for t in tasks if t.task_type == TaskType.ISSUE)
        assert task_issue.issue_number == 12
        assert task_issue.platform == PlatformType.GITLAB
        assert "Optimize SQL queries" in task_issue.title

        task_mr = next(t for t in tasks if t.task_type == TaskType.PULL_REQUEST)
        assert task_mr.pr_number == 34
        assert task_mr.platform == PlatformType.GITLAB

        # Verify auto-mark todo as done was triggered
        assert mock_mark.call_count == 2
        mock_mark.assert_any_call(101)
        mock_mark.assert_any_call(102)

        # 2nd sync with same todos: deduplication should prevent re-ingestion
        tasks_second = await service.sync_gitlab_todos()
        assert len(tasks_second) == 0


@pytest.mark.asyncio
async def test_poller_github_sync():
    await init_db()

    settings.github_token = "ghp_testdummygithubtoken"
    repo_url = f"https://github.com/myorg/api-service-{uuid.uuid4().hex[:6]}.git"

    mock_issues = [
        {
            "id": 555,
            "number": 88,
            "title": "Add caching layer for user profile endpoint",
            "body": "Redis cache with 5-minute TTL.",
            "repository": {
                "clone_url": repo_url,
                "default_branch": "develop",
            },
            "user": {"login": "dev_boss"},
        }
    ]

    service = PollerService()

    with patch.object(service.github_client, "get_user_assigned_issues", AsyncMock(return_value=mock_issues)):
        tasks = await service.sync_github_notifications()
        assert len(tasks) == 1
        assert tasks[0].issue_number == 88
        assert tasks[0].platform == PlatformType.GITHUB
        assert tasks[0].base_branch == "develop"

        # Deduplication check
        tasks_dup = await service.sync_github_notifications()
        assert len(tasks_dup) == 0


@pytest.mark.asyncio
async def test_poller_api_endpoints():
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check GET /api/sync/status
        resp_status = await client.get("/api/sync/status")
        assert resp_status.status_code == 200
        data_status = resp_status.json()
        assert "poll_enabled" in data_status
        assert "poll_interval_hours" in data_status

        # Check POST /api/sync with mocked sync_all
        with patch.object(poller_service, "sync_all", AsyncMock(return_value={
            "success": True,
            "gitlab_count": 2,
            "github_count": 1,
            "total_new": 3,
            "synced_at": datetime.now(timezone.utc).isoformat()
        })):
            resp_sync = await client.post("/api/sync")
            assert resp_sync.status_code == 200
            data_sync = resp_sync.json()
            assert data_sync["success"] is True
            assert data_sync["total_new"] == 3


@pytest.mark.asyncio
async def test_queue_deduplication_and_normalization():
    await init_db()
    from issueforge.core.queue import task_queue
    from issueforge.core.database import delete_task

    test_num = 99991
    url_with_git = f"https://gitlab.com/test-org/repo-{uuid.uuid4().hex[:4]}.git"
    url_without_git = url_with_git.replace(".git", "")

    # 1. Enqueue task with url ending in .git
    task1 = await task_queue.create_and_enqueue_task(
        title="Test Dedup Item",
        description="Testing deduplication",
        repo_url=url_with_git,
        platform=PlatformType.GITLAB,
        issue_number=test_num
    )

    # 2. Attempt to enqueue the exact same issue but without .git
    task2 = await task_queue.create_and_enqueue_task(
        title="Test Dedup Item Duplicate",
        description="Duplicate description",
        repo_url=url_without_git,
        platform=PlatformType.GITLAB,
        issue_number=test_num
    )

    # Assert that the deduplication guard caught it and returned the existing task
    assert task1.id == task2.id

    # Clean up
    await delete_task(task1.id)


@pytest.mark.asyncio
async def test_poller_concurrent_sync_lock():
    service = PollerService()
    # Acquire the lock to simulate an in-progress sync
    await service._sync_lock.acquire()
    try:
        res = await service.sync_all()
        assert res["success"] is True
        assert "already in progress" in res.get("message", "")
        assert res["total_new"] == 0
    finally:
        service._sync_lock.release()
