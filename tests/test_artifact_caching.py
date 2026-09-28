import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from issueforge.agents.engine import IssueforgeAgentEngine
from issueforge.core.database import init_db, save_task, get_task
from issueforge.core.models import (
    PipelineRun,
    PlatformType,
    ReviewSummary,
    Task,
    TaskStatus,
    TaskType,
    TestResult,
)
from issueforge.core.task_dossier import TaskDossierManager


@pytest.mark.asyncio
async def test_artifact_caching_skips_redundant_planning_on_retry(tmp_path):
    """Verify that retrying a task that failed at TESTER reuses PLAN.md and skips planner LLM."""
    await init_db()

    task_id = "test-caching-task"
    existing_plan = "# Architecture Plan\n1. Add auth\n2. Add routes\n- [ ] Subtask 1\n- [ ] Subtask 2"

    task = Task(

        # Skip the HITL branch gate; this test exercises plan reuse.

        target_branch_confirmed=True,
        id=task_id,
        title="Cache Test Task",
        description="Testing artifact caching",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        working_branch="forge/test-cache",
        status=TaskStatus.FAILED,
        plan=existing_plan,
        runs=[

            PipelineRun(
                run_id="run-1",
                attempt_number=1,
                status=TaskStatus.FAILED,
                failure_stage="TESTER",
                error_message="1 test failed in auth_test.py",
                test_summary="Tests: Failed (4/5)"
            )
        ]
    )
    await save_task(task)

    # Put a PLAN.md in run-1's sandbox dossier
    with patch.object(TaskDossierManager, "get_task_dir", return_value=tmp_path / task_id):
        run1_sandbox = tmp_path / task_id / "sandboxes" / "run-1"
        run1_sandbox.mkdir(parents=True, exist_ok=True)
        (run1_sandbox / "PLAN.md").write_text(existing_plan, encoding="utf-8")

        engine = IssueforgeAgentEngine()

        mock_planner = AsyncMock()
        mock_coder = AsyncMock(return_value=["src/auth.py"])
        mock_tester = AsyncMock(return_value=TestResult(passed=True, total_tests=5, passed_tests=5, test_runner="pytest"))
        mock_reviewer = AsyncMock(return_value=ReviewSummary(

            summary="Fixed test",
            risk_assessment="Low",
            test_verification="All 5 tests passing",
            files_changed=["src/auth.py"],
            suggested_commit_message="fix: auth",
            suggested_pr_title="fix: auth",
            suggested_pr_body="Fixes failing test in auth"
        ))


        with patch("issueforge.git.repo_manager.GitRepoManager.clone_repository", new=AsyncMock(return_value=True)), \
             patch("issueforge.git.repo_manager.GitRepoManager.get_remote_branches", new=AsyncMock(return_value=["main"])), \
             patch("issueforge.git.repo_manager.GitRepoManager.create_working_branch", new=AsyncMock(return_value=True)), \
             patch("issueforge.git.repo_manager.GitRepoManager.get_diff", new=AsyncMock(return_value=("1 file changed", "diff --git a/src/auth.py b/src/auth.py", []))), \
             patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.triage_task", new=AsyncMock(return_value=(False, None))), \
             patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.run_planner", mock_planner), \
             patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.run_coder", mock_coder), \
             patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.run_tester", mock_tester), \
             patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.run_reviewer", mock_reviewer), \
             patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.record_task_learning", new=AsyncMock()), \
             patch("issueforge.bot.telegram_bot.telegram_manager.send_review_confirmation", new=AsyncMock()):

            completed_task = await engine.execute_task_pipeline(task_id)

            # Assert run_planner was NEVER called because of artifact cache hit!
            assert mock_planner.call_count == 0

            # Assert run_coder was called with differential repair test feedback
            assert mock_coder.call_count == 1
            call_kwargs = mock_coder.call_args[1]
            assert "Previous Run #1 Test Failure" in call_kwargs.get("test_feedback", "")
            assert "1 test failed in auth_test.py" in call_kwargs.get("test_feedback", "")

            # Assert task transitioned to AWAITING_CONFIRMATION
            assert completed_task.status == TaskStatus.AWAITING_CONFIRMATION
            assert len(completed_task.runs) == 2
            assert completed_task.runs[1].run_id == "run-2"
