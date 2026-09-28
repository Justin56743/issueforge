import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from issueforge.agents.agy_runner import AgySessionRunner
from issueforge.core.database import (
    get_task,
    get_task_events,
    init_db,
    save_event,
    save_task,
)
from issueforge.core.models import (
    AgentRole,
    EventType,
    PlatformType,
    Task,
    TaskEvent,
    TaskStatus,
    TaskType,
)
from issueforge.core.queue import task_queue
from issueforge.core.sandbox import NativeSandbox
from issueforge.main import app

client = TestClient(app)


@pytest.mark.asyncio
async def test_run_id_database_filtering(tmp_path):
    """Verify task_events save and filter accurately by run_id."""
    await init_db()
    task_id = "test-run-filter-task"

    task = Task(
        id=task_id,
        title="Test Task",
        description="Filter task",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        working_branch="forge/test",
    )
    await save_task(task)

    # Save events with different run_ids
    await save_event(TaskEvent(
        task_id=task_id,
        event_type=EventType.STEP,
        message="Run 1 Step 1",
        run_id="run-1"
    ))
    await save_event(TaskEvent(
        task_id=task_id,
        event_type=EventType.LOG,
        message="Run 1 Step 2",
        run_id="run-1"
    ))
    await save_event(TaskEvent(
        task_id=task_id,
        event_type=EventType.STEP,
        message="Run 2 Step 1",
        run_id="run-2"
    ))

    # Retrieve all events
    all_events = await get_task_events(task_id)
    assert len(all_events) == 3

    # Retrieve only run-1
    run1_events = await get_task_events(task_id, run_id="run-1")
    assert len(run1_events) == 2
    assert all(e.run_id == "run-1" for e in run1_events)
    assert run1_events[0].message == "Run 1 Step 1"

    # Retrieve only run-2
    run2_events = await get_task_events(task_id, run_id="run-2")
    assert len(run2_events) == 1
    assert run2_events[0].run_id == "run-2"
    assert run2_events[0].message == "Run 2 Step 1"


@pytest.mark.asyncio
async def test_agy_session_runner_stream_json_parsing(tmp_path):
    """Verify AgySessionRunner parses stream-json events from agy CLI accurately."""
    sandbox = NativeSandbox("test-agy-task", run_id="run-1")
    runner = AgySessionRunner(
        task_id="test-agy-task",
        run_id="run-1",
        sandbox_dir=sandbox.workspace_path,
        role=AgentRole.CODER
    )

    # Simulated stream-json lines in the actual agy CLI schema
    tool_event = {
        "event": "step_update",
        "step_update": {
            "step_type": "tool",
            "state": "ACTIVE",
            "tool_name": "write_to_file",
            "tool_info": {
                "parameters": {
                    "TargetFile": str(sandbox.workspace_path / "ARCHITECTURE.md"),
                    "CodeContent": "# Architecture Guide"
                }
            }
        }
    }

    completion_event = {
        "event": "step_update",
        "step_update": {
            "step_type": "assistant_response",
            "state": "COMPLETED",
            "content": "Successfully implemented documentation as specified in PLAN.md."
        }
    }

    mock_stream_lines = [
        json.dumps(tool_event).encode("utf-8") + b"\n",
        json.dumps(completion_event).encode("utf-8") + b"\n",
        b""
    ]

    mock_process = AsyncMock()
    mock_process.stdout.readline = AsyncMock(side_effect=mock_stream_lines)
    mock_process.stderr.readline = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=0)
    mock_process.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_process):
        with patch.object(runner, "get_agy_path", return_value="/usr/local/bin/agy"):
            success, response = await runner.run_prompt(
                system_instructions="You are coder agent.",
                task_prompt="Implement documentation"
            )

            assert success is True
            assert "Successfully implemented" in response


@pytest.mark.asyncio
async def test_task_queue_retry_creates_new_run():
    """Verify retrying a failed task resets status and increments runs."""
    await init_db()
    task = Task(
        id="test-retry-task-queue",
        title="Test Retry Task",
        description="Retry test",
        repo_url="https://gitlab.com/owner/repo.git",
        repo_name="owner/repo",
        working_branch="forge/test-retry-task-queue",
        status=TaskStatus.FAILED,
        error_message="Previous coder failure",
        runs=[]
    )
    await save_task(task)

    with patch.object(task_queue, "start_execution_background") as mock_exec:
        updated = await task_queue.retry_task(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.APPROVED
        assert updated.error_message is None
        mock_exec.assert_called_once_with(task.id)


def test_api_events_run_id_filtering_and_retry_endpoint():
    """Verify REST API /api/tasks/{task_id}/events?run_id=... and /api/tasks/{task_id}/retry."""
    task_id = "test-api-retry-task"
    loop = asyncio.get_event_loop()
    loop.run_until_complete(save_task(Task(
        id=task_id,
        title="API Retry Test",
        description="Testing retry endpoint",
        repo_url="https://gitlab.com/owner/repo.git",
        repo_name="owner/repo",
        working_branch="forge/test-api-retry-task",
        status=TaskStatus.FAILED
    )))

    loop.run_until_complete(save_event(TaskEvent(
        task_id=task_id,
        event_type=EventType.STEP,
        message="Event Run 1",
        run_id="run-1"
    )))
    loop.run_until_complete(save_event(TaskEvent(
        task_id=task_id,
        event_type=EventType.STEP,
        message="Event Run 2",
        run_id="run-2"
    )))

    # Test GET /api/tasks/{task_id}/events with run_id
    res1 = client.get(f"/api/tasks/{task_id}/events?run_id=run-1")
    assert res1.status_code == 200
    events_run1 = res1.json()
    assert len(events_run1) == 1
    assert events_run1[0]["message"] == "Event Run 1"

    res2 = client.get(f"/api/tasks/{task_id}/events?run_id=run-2")
    assert res2.status_code == 200
    events_run2 = res2.json()
    assert len(events_run2) == 1
    assert events_run2[0]["message"] == "Event Run 2"

    # Test POST /api/tasks/{task_id}/retry
    with patch.object(task_queue, "start_execution_background"):
        retry_res = client.post(f"/api/tasks/{task_id}/retry", json={})
        assert retry_res.status_code == 200
        data = retry_res.json()
        assert data["status"] == "retrying"
        assert data["task"]["status"] == "APPROVED"


def test_agy_session_runner_model_normalization():
    """Verify that LiteLLM model identifiers and variants are correctly normalized for agy CLI."""
    # 1. Models with litellm provider prefixes
    assert AgySessionRunner.normalize_model_name("gemini/gemini-3.7-flash") == "gemini-3.7-flash-high"
    assert AgySessionRunner.normalize_model_name("gemini/gemini-3.8-flash") == "gemini-3.8-flash-high"
    assert AgySessionRunner.normalize_model_name("gemini/gemini-3.6-flash", default_effort="low") == "gemini-3.6-flash-low"
    
    # 2. Shorthand or raw models
    assert AgySessionRunner.normalize_model_name("gemini-3.8-flash") == "gemini-3.8-flash-high"
    assert AgySessionRunner.normalize_model_name("gemini-3.7-flash-medium") == "gemini-3.7-flash-medium"
    assert AgySessionRunner.normalize_model_name("gemini-3.6-flash-high") == "gemini-3.6-flash-high"

    # 3. Claude and other non-gemini models
    assert AgySessionRunner.normalize_model_name("claude-sonnet-4-6") == "claude-sonnet-4-6"

    # 4. Binary discovery
    agy_path = AgySessionRunner.get_agy_path()
    # The point of this assertion is that no *foreign* developer's home is hardcoded.
    # It cannot be "no username at all": get_agy_path() builds a candidate from
    # Path.home(), so on a machine owned by that user the path legitimately contains it.
    assert "solar1" not in agy_path
    assert agy_path.startswith(str(Path.home())) or not agy_path.startswith("/home/")
    assert agy_path.endswith("agy")

