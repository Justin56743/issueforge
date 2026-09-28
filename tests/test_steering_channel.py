from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import pytest

from issueforge.agents.orchestrator import SupervisoryOrchestrator
from issueforge.core.database import get_task, save_task
from issueforge.core.models import PlatformType, Task, TaskStatus, TaskType
from issueforge.core.queue import task_queue
from issueforge.main import app

client = TestClient(app)


@pytest.fixture
async def sample_task():
    task = Task(
        id="test-steering-task",
        title="Setup database models",
        description="Create PostgreSQL models",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/db.git",
        repo_name="org/db",
        working_branch="forge/issue-1",
        status=TaskStatus.CODING,
        active_run_id="run-1",
    )
    await save_task(task)
    return task


@pytest.mark.asyncio
async def test_task_queue_steer_task(sample_task):
    directive = "Focus strictly on the users table and omit soft deletes"
    updated = await task_queue.steer_task(sample_task.id, directive)

    assert updated is not None
    assert "[Operator Mid-Flight Steering Directive]" in updated.custom_instructions
    assert directive in updated.custom_instructions

    # Verify persisted in database
    db_task = await get_task(sample_task.id)
    assert db_task is not None
    assert directive in db_task.custom_instructions


def test_api_steer_task(sample_task):
    # 1. Successful steering call
    resp = client.post(
        f"/api/tasks/{sample_task.id}/steer",
        json={"directive": "Ensure password hashing uses bcrypt"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["task_id"] == sample_task.id
    assert "bcrypt" in data["directive"]

    # 2. Empty directive returns 400
    bad_resp = client.post(
        f"/api/tasks/{sample_task.id}/steer",
        json={"directive": "   "}
    )
    assert bad_resp.status_code == 400

    # 3. Nonexistent task returns 404
    missing_resp = client.post(
        "/api/tasks/nonexistent-task-999/steer",
        json={"directive": "Do something"}
    )
    assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_orchestrator_interpret_steering_intent(sample_task):
    # Test heuristic fallback for STEER_TASK intent
    user_input = f"steer {sample_task.id}: please make sure to add created_at index"
    
    with patch("issueforge.agents.orchestrator.call_llm_with_fallback", side_effect=Exception("LLM offline")), \
         patch("issueforge.agents.orchestrator.AgySessionRunner.run_prompt", new=AsyncMock(return_value=(False, ""))):
        decision = await SupervisoryOrchestrator.interpret_operator_instruction(
            instruction=user_input,
            recent_tasks=[sample_task]
        )

        assert decision["intent"] == "STEER_TASK"
        assert decision["referenced_task_id"] == sample_task.id
        assert "created_at index" in decision["steering_directive"]
