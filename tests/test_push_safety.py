"""The confirm → push path: it must never strand a task, push code the operator did
not approve, or push twice."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from issueforge.agents.engine import IssueforgeAgentEngine
from issueforge.agents.merge_resolver import MergeConflictResolver
from issueforge.core.database import get_task, save_task
from issueforge.core.models import PipelineRun, PlatformType, Task, TaskStatus
from issueforge.core.queue import task_queue


def _task(task_id: str, status: TaskStatus = TaskStatus.AWAITING_CONFIRMATION) -> Task:
    return Task(
        id=task_id,
        title="t",
        description="d",
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        working_branch="forge/t",
        status=status,
        active_run_id="run-1",
        runs=[PipelineRun(run_id="run-1", status=status)],
    )


async def test_a_crash_mid_push_fails_the_task_instead_of_leaving_it_pushing():
    task = _task("test-push-crash")
    await save_task(task)
    with patch("issueforge.git.repo_manager.GitRepoManager.commit_changes",
               new=AsyncMock(side_effect=RuntimeError("disk full"))):
        result = await IssueforgeAgentEngine().push_and_create_pr(task.id)

    assert result.status == TaskStatus.FAILED
    assert "disk full" in result.error_message
    stored = await get_task(task.id)
    assert stored.status == TaskStatus.FAILED
    assert stored.runs[0].status == TaskStatus.FAILED


async def test_llm_resolved_merge_is_sent_back_for_confirmation_not_pushed():
    task = _task("test-push-reconfirm")
    await save_task(task)

    async def resolved_a_conflict(self, target):
        self.resolved_files = ["app.py"]
        return True, "Resolved 1 conflicted file(s)."

    push = AsyncMock(return_value=True)
    with patch("issueforge.git.repo_manager.GitRepoManager.commit_changes", new=AsyncMock(return_value=True)), \
         patch.object(MergeConflictResolver, "execute_resolution_pipeline", resolved_a_conflict), \
         patch("issueforge.git.repo_manager.GitRepoManager.push_working_branch", new=push):
        result = await IssueforgeAgentEngine().push_and_create_pr(task.id)

    assert result.status == TaskStatus.AWAITING_CONFIRMATION
    push.assert_not_called()


async def test_merge_guard_failure_marks_the_run_failed():
    task = _task("test-push-merge-fail")
    await save_task(task)
    with patch("issueforge.git.repo_manager.GitRepoManager.commit_changes", new=AsyncMock(return_value=True)), \
         patch.object(MergeConflictResolver, "execute_resolution_pipeline",
                      new=AsyncMock(return_value=(False, "could not run"))):
        result = await IssueforgeAgentEngine().push_and_create_pr(task.id)

    assert result.status == TaskStatus.FAILED
    assert result.runs[0].status == TaskStatus.FAILED
    assert result.runs[0].failure_stage == "MERGE_CONFLICT"


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PENDING_APPROVAL])
async def test_confirm_refuses_a_task_that_is_not_awaiting_confirmation(status):
    task = _task(f"test-confirm-guard-{status.value.lower()}", status=status)
    await save_task(task)
    with patch("issueforge.agents.engine.IssueforgeAgentEngine.push_and_create_pr", new=AsyncMock()) as push:
        with pytest.raises(ValueError):
            await task_queue.confirm_and_push(task.id)
    push.assert_not_called()


async def test_double_confirm_pushes_once():
    task = _task("test-confirm-twice")
    await save_task(task)
    release = asyncio.Event()

    async def slow_push(task_id, target_branch=None):
        await release.wait()

    with patch("issueforge.agents.engine.IssueforgeAgentEngine.push_and_create_pr",
               new=AsyncMock(side_effect=slow_push)) as push:
        await task_queue.confirm_and_push(task.id)
        with pytest.raises(ValueError):
            await task_queue.confirm_and_push(task.id)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    push.assert_awaited_once()


async def test_approve_refuses_a_task_that_already_ran():
    task = _task("test-approve-guard", status=TaskStatus.COMPLETED)
    await save_task(task)
    with patch.object(task_queue, "start_execution_background") as start:
        with pytest.raises(ValueError):
            await task_queue.approve_task(task.id)
    start.assert_not_called()
