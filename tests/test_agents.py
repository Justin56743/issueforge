import pytest
from issueforge.agents.coder import CoderAgent
from issueforge.agents.llm import extract_json
from issueforge.agents.reviewer import ReviewerAgent
from issueforge.agents.tester import TesterAgent
from issueforge.core.models import Task, TaskType, PlatformType
from issueforge.core.sandbox import NativeSandbox


def create_mock_task():
    return Task(
        id="test-agent-task",
        title="Add user authentication",
        description="Implement auth",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        working_branch="forge/issue-1"
    )


def test_tester_test_command_detection():
    sandbox = NativeSandbox("test-test-runner")
    sandbox.setup()
    task = create_mock_task()
    tester = TesterAgent(sandbox=sandbox, task=task)

    # 1. Python project
    sandbox.write_file("pyproject.toml", "[tool.poetry]\nname = 'test'")
    assert "pytest" in tester.detect_test_command()

    # 2. Node project
    sandbox.cleanup()
    sandbox.setup()
    sandbox.write_file("package.json", '{"name": "test"}')
    assert tester.detect_test_command() == "npm test"

    # 3. Rust project
    sandbox.cleanup()
    sandbox.setup()
    sandbox.write_file("Cargo.toml", '[package]\nname = "test"')
    assert tester.detect_test_command() == "cargo test"

    # 4. Go project
    sandbox.cleanup()
    sandbox.setup()
    sandbox.write_file("go.mod", "module test")
    assert "go test" in tester.detect_test_command()

    sandbox.cleanup()


def test_coder_json_extraction():
    sandbox = NativeSandbox("test-coder-json")
    task = create_mock_task()
    coder = CoderAgent(sandbox=sandbox, task=task)

    raw_markdown = """Here are the file modifications:
```json
{
  "explanation": "Added healthcheck endpoint",
  "files": [
    {
      "path": "app/main.py",
      "action": "write",
      "content": "from fastapi import FastAPI\\napp = FastAPI()"
    }
  ]
}
```
Hope this helps!"""

    parsed = extract_json(raw_markdown)
    assert parsed is not None
    assert parsed["explanation"] == "Added healthcheck endpoint"
    assert len(parsed["files"]) == 1
    assert parsed["files"][0]["path"] == "app/main.py"


def test_reviewer_json_extraction():
    task = create_mock_task()
    reviewer = ReviewerAgent(task=task)

    raw_json = """{
  "summary": "Implemented JWT validation middleware.",
  "risk_assessment": "Low",
  "test_verification": "All 8 tests passing.",
  "files_changed": ["auth.py"],
  "suggested_commit_message": "feat(auth): add JWT validation",
  "suggested_pr_title": "feat: add JWT validation",
  "suggested_pr_body": "## Summary\\nAdded JWT validation"
}"""
    parsed = extract_json(raw_json)
    assert parsed is not None
    assert parsed["risk_assessment"] == "Low"
    assert parsed["suggested_commit_message"] == "feat(auth): add JWT validation"


@pytest.mark.asyncio
async def test_llm_model_fallback():
    from unittest.mock import AsyncMock, patch
    from types import SimpleNamespace
    from issueforge.agents.llm import call_llm_with_fallback

    mock_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Fallback response"))]
    )

    call_count = 0

    async def mock_acompletion(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        model = kwargs.get("model")
        if model == "gemini/gemini-unavailable":
            raise RuntimeError("Model unavailable or deprecated (404)")
        return mock_response

    with patch("issueforge.agents.llm.litellm.acompletion", side_effect=mock_acompletion):
        response = await call_llm_with_fallback(
            primary_model="gemini/gemini-unavailable",
            messages=[{"role": "user", "content": "Hello"}],
            task_id="test-fallback-task"
        )
        assert response.choices[0].message.content == "Fallback response"
        assert call_count >= 2


@pytest.mark.asyncio
async def test_tester_no_tests_syntax_verification():
    sandbox = NativeSandbox("test-tester-no-tests")
    sandbox.setup()
    sandbox.write_file("main.py", "def add(a, b):\n    return a + b\n")

    task = create_mock_task()
    tester = TesterAgent(sandbox=sandbox, task=task)

    assert tester.detect_test_command() is None

    result = await tester.execute()
    assert result.passed is True
    assert result.total_tests == 0
    assert result.failed_tests == 0
    assert "Workspace Integrity Check" in result.test_runner

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_tester_missing_test_dir_graceful_recovery():
    sandbox = NativeSandbox("test-tester-missing-tests-dir")
    sandbox.setup()
    sandbox.write_file("app.py", "x = 42\n")

    task = create_mock_task()
    tester = TesterAgent(sandbox=sandbox, task=task)

    # Directly run a command that outputs "Start directory is not importable: 'tests'"
    result = await tester.execute(
        custom_command="python3 -m unittest discover -s nonexistent_tests -p 'test_*.py'"
    )
    assert result.passed is True
    assert result.total_tests == 0
    assert result.failed_tests == 0

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_engine_halts_when_agent_fails():
    from unittest.mock import AsyncMock, patch
    from issueforge.agents.engine import IssueforgeAgentEngine
    from issueforge.core.database import save_task
    from issueforge.core.models import TaskStatus, TestResult

    task = create_mock_task()
    task.id = "test-halt-pipeline"
    # Skip the HITL branch gate: this test exercises the downstream failure path.
    task.target_branch_confirmed = True
    await save_task(task)

    engine = IssueforgeAgentEngine()

    # Mock git_mgr, planner, coder, and make tester fail
    with patch("issueforge.agents.engine.GitRepoManager") as mock_git_cls:
        mock_git = mock_git_cls.return_value
        mock_git.clone_repository = AsyncMock(return_value=True)
        mock_git.create_working_branch = AsyncMock()
        mock_git.get_remote_branches = AsyncMock(return_value=["main", "develop"])
        mock_git.get_diff = AsyncMock(return_value=("1 file changed", "diff --git...", []))

        with patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.triage_task", new=AsyncMock(return_value=(False, None))):
            with patch("issueforge.agents.planner.PlannerAgent.execute", new_callable=AsyncMock) as mock_plan:
                mock_plan.return_value = "Step 1: Do something"

                with patch("issueforge.agents.coder.CoderAgent.execute", new_callable=AsyncMock) as mock_code:
                    mock_code.return_value = ["file.py"]

                    # Mock TesterAgent to always return a failing TestResult
                    failing_test = TestResult(
                        passed=False,
                        total_tests=5,
                        passed_tests=3,
                        failed_tests=2,
                        test_runner="pytest",
                        failure_details="AssertionError: 2 tests failed"
                    )
                    with patch("issueforge.agents.tester.TesterAgent.execute", new_callable=AsyncMock) as mock_test:
                        mock_test.return_value = failing_test

                        res_task = await engine.execute_task_pipeline(task.id, max_test_retries=1)

                        # Assert the pipeline strictly halted and set status to FAILED, NOT AWAITING_CONFIRMATION
                        assert res_task.status == TaskStatus.FAILED
                        assert "Stopping pipeline" in res_task.error_message
                        assert res_task.status != TaskStatus.AWAITING_CONFIRMATION



async def test_engine_pauses_at_the_branch_gate():
    """An unconfirmed target branch must stop the pipeline before any code is written."""
    from unittest.mock import AsyncMock, patch

    from issueforge.agents.engine import IssueforgeAgentEngine
    from issueforge.core.database import save_task
    from issueforge.core.models import TaskStatus

    task = create_mock_task()
    task.id = "test-branch-gate"
    task.target_branch_confirmed = False
    await save_task(task)

    with patch("issueforge.agents.engine.GitRepoManager") as mock_git_cls:
        mock_git = mock_git_cls.return_value
        mock_git.clone_repository = AsyncMock(return_value=True)
        mock_git.create_working_branch = AsyncMock()
        mock_git.get_remote_branches = AsyncMock(return_value=["main", "develop", "staging"])
        with patch("issueforge.agents.planner.PlannerAgent.execute", new_callable=AsyncMock) as mock_plan:
            res = await IssueforgeAgentEngine().execute_task_pipeline(task.id)

    assert res.status == TaskStatus.AWAITING_BRANCH_SELECTION
    assert set(res.target_branch_candidates) >= {"main", "develop", "staging"}
    # Nothing downstream may run before the operator answers.
    mock_plan.assert_not_called()


async def test_confirming_a_branch_resumes_the_paused_run():
    from unittest.mock import AsyncMock, patch

    from issueforge.core.database import get_task, save_task
    from issueforge.core.models import TaskStatus
    from issueforge.core.queue import task_queue

    task = create_mock_task()
    task.id = "test-branch-resume"
    task.status = TaskStatus.AWAITING_BRANCH_SELECTION
    task.target_branch_candidates = ["main", "develop"]
    await save_task(task)

    with patch.object(task_queue, "start_execution_background") as mock_start:
        updated = await task_queue.set_target_branch(task.id, "develop")

    assert updated.selected_target_branch == "develop"
    assert updated.target_branch_confirmed is True
    assert updated.status == TaskStatus.APPROVED
    mock_start.assert_called_once_with(task.id)

    reloaded = await get_task(task.id)
    assert reloaded.target_branch_confirmed is True


async def test_setting_a_branch_on_a_finished_task_does_not_relaunch_it():
    from unittest.mock import patch

    from issueforge.core.database import save_task
    from issueforge.core.models import TaskStatus
    from issueforge.core.queue import task_queue

    task = create_mock_task()
    task.id = "test-branch-no-relaunch"
    task.status = TaskStatus.AWAITING_CONFIRMATION
    await save_task(task)

    with patch.object(task_queue, "start_execution_background") as mock_start:
        updated = await task_queue.set_target_branch(task.id, "staging")

    assert updated.status == TaskStatus.AWAITING_CONFIRMATION
    assert updated.target_branch_confirmed is True
    mock_start.assert_not_called()


async def test_engine_sends_triage_question_to_telegram():
    """A clarification question must reach Telegram, not only the dashboard."""
    from unittest.mock import AsyncMock, patch

    from issueforge.agents.engine import IssueforgeAgentEngine
    from issueforge.core.database import save_task
    from issueforge.core.models import TaskQuestion, TaskStatus

    task = create_mock_task()
    task.id = "test-triage-question-telegram"
    task.target_branch_confirmed = True
    await save_task(task)

    question = TaskQuestion(
        id="q-triage-1",
        task_id=task.id,
        question="Which auth backend should this use?",
        options=["oauth", "session"],
    )

    with patch("issueforge.agents.engine.GitRepoManager") as mock_git_cls:
        mock_git = mock_git_cls.return_value
        mock_git.clone_repository = AsyncMock(return_value=True)
        mock_git.create_working_branch = AsyncMock()
        mock_git.get_remote_branches = AsyncMock(return_value=["main"])

        with patch(
            "issueforge.agents.orchestrator.SupervisoryOrchestrator.triage_task",
            new=AsyncMock(return_value=(True, question)),
        ):
            with patch("issueforge.bot.telegram_bot.telegram_manager") as mock_tg:
                mock_tg.send_question_alert = AsyncMock()
                with patch("issueforge.agents.planner.PlannerAgent.execute", new_callable=AsyncMock) as mock_plan:
                    res = await IssueforgeAgentEngine().execute_task_pipeline(task.id)

    assert res.status == TaskStatus.AWAITING_INPUT
    mock_tg.send_question_alert.assert_awaited_once()
    sent_task, sent_question = mock_tg.send_question_alert.await_args.args
    assert sent_task.id == task.id
    assert sent_question.question == question.question
    # The pipeline must not continue past the question.
    mock_plan.assert_not_called()


def test_extract_json_handles_every_shape_the_agents_see():
    from issueforge.agents.llm import extract_json

    # Bare JSON
    assert extract_json('{"a": 1}') == {"a": 1}
    # Fenced with a language tag, as Gemini returns it
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    # Fenced without a tag
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}
    # Prose on both sides, which is the common failure case
    assert extract_json('Sure, here it is:\n{"a": 1}\nHope that helps.') == {"a": 1}
    # Nested braces must not truncate at the first closing brace
    assert extract_json('{"a": {"b": 2}}') == {"a": {"b": 2}}
    # Unparseable input returns None rather than raising
    assert extract_json("not json at all") is None
    assert extract_json("") is None


async def test_resuming_after_the_branch_gate_reuses_the_paused_run():
    """Confirming a branch continues run-1; it must not leave run-1 stuck at the gate
    and start run-2. A cancelled pause, by contrast, is over: retry starts a new run."""
    from unittest.mock import AsyncMock, patch

    from issueforge.agents.engine import IssueforgeAgentEngine
    from issueforge.core.database import get_task, save_task
    from issueforge.core.models import ReviewSummary, TaskStatus, TestResult
    from issueforge.core.queue import task_queue

    task = create_mock_task()
    task.id = "test-branch-resume-same-run"
    task.target_branch_confirmed = False
    await save_task(task)
    review = ReviewSummary(
        summary="s", risk_assessment="Low", test_verification="ok",
        suggested_commit_message="feat: x", suggested_pr_title="x", suggested_pr_body="x",
    )

    with patch("issueforge.agents.engine.GitRepoManager") as mock_git_cls, \
         patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.triage_task", new=AsyncMock(return_value=(False, None))), \
         patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.record_task_learning", new=AsyncMock()), \
         patch("issueforge.agents.planner.PlannerAgent.execute", new=AsyncMock(return_value="- [ ] Do it")), \
         patch("issueforge.agents.coder.CoderAgent.execute", new=AsyncMock(return_value=["a.py"])), \
         patch("issueforge.agents.tester.TesterAgent.execute", new=AsyncMock(return_value=TestResult(passed=True, total_tests=1, passed_tests=1))), \
         patch("issueforge.agents.reviewer.ReviewerAgent.execute", new=AsyncMock(return_value=review)):
        mock_git = mock_git_cls.return_value
        mock_git.clone_repository = AsyncMock(return_value=True)
        mock_git.create_working_branch = AsyncMock()
        mock_git.get_remote_branches = AsyncMock(return_value=["main", "develop"])
        mock_git.get_changed_file_paths = AsyncMock(return_value=["a.py"])
        mock_git.get_diff = AsyncMock(return_value=("1 file changed", "diff --git a/a.py b/a.py", []))

        engine = IssueforgeAgentEngine()
        paused = await engine.execute_task_pipeline(task.id)
        assert paused.status == TaskStatus.AWAITING_BRANCH_SELECTION

        with patch.object(task_queue, "start_execution_background"):
            await task_queue.set_target_branch(task.id, "develop")
        done = await engine.execute_task_pipeline(task.id)

        assert done.status == TaskStatus.AWAITING_CONFIRMATION
        assert [r.run_id for r in done.runs] == ["run-1"]
        assert done.runs[0].status == TaskStatus.AWAITING_CONFIRMATION

        # Cancelling a task that is paused at the gate closes that run.
        second = create_mock_task()
        second.id = "test-branch-cancel-paused"
        await save_task(second)
        await engine.execute_task_pipeline(second.id)
        cancelled = await task_queue.cancel_task(second.id)
        assert cancelled.runs[0].status == TaskStatus.CANCELLED
        await engine.execute_task_pipeline(second.id)
        assert [r.run_id for r in (await get_task(second.id)).runs] == ["run-1", "run-2"]


async def test_unittest_failure_is_not_read_as_ok_because_of_a_substring():
    """'BROKEN' contains 'OK'; the old substring check passed a failing suite."""
    sandbox = NativeSandbox("test-tester-ok-substring")
    sandbox.setup()
    tester = TesterAgent(sandbox=sandbox, task=create_mock_task())
    result = await tester.execute(
        custom_command="printf 'Ran 2 tests in 0.1s\\nBROKEN pipe\\n\\nFAILED (failures=1)\\n' >&2; exit 1"
    )
    assert result.passed is False
    assert result.failed_tests == 1
    sandbox.cleanup()


def test_makefile_with_only_a_check_target_is_not_run_as_make_test():
    sandbox = NativeSandbox("test-tester-make-check")
    sandbox.setup()
    sandbox.write_file("Makefile", "check:\n\techo checking\n")
    assert TesterAgent(sandbox=sandbox, task=create_mock_task()).detect_test_command() != "make test"
    sandbox.write_file("Makefile", "build:\n\techo b\ntest:\n\techo t\n")
    assert TesterAgent(sandbox=sandbox, task=create_mock_task()).detect_test_command() == "make test"
    sandbox.cleanup()


async def test_python_suite_runs_inside_a_dependency_venv_outside_the_workspace():
    """Tests ran on the bare host interpreter, so any project with dependencies failed on
    imports and burned every repair loop."""
    sandbox = NativeSandbox("test-tester-venv")
    sandbox.setup()
    sandbox.write_file("requirements.txt", "")
    sandbox.write_file(
        "test_env.py",
        "import os, sys, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_venv(self):\n"
        "        self.assertEqual(os.path.realpath(sys.prefix), os.path.realpath(os.environ['VIRTUAL_ENV']))\n",
    )
    tester = TesterAgent(sandbox=sandbox, task=create_mock_task())
    result = await tester.execute(custom_command="python3 -m unittest discover -p 'test_*.py'")

    assert result.passed is True, result.stderr
    assert (sandbox.meta_dir / "venv").is_dir()
    assert not (sandbox.workspace_path / "venv").exists()
    sandbox.cleanup()
