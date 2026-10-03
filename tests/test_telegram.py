import pytest
from issueforge.bot.messages import (
    build_new_task_keyboard,
    build_review_confirmation_keyboard,
    format_new_task_message,
    format_review_confirmation_message,
    format_task_completed_message,
)
from issueforge.core.models import (
    PlatformType,
    ReviewSummary,
    Task,
    TaskStatus,
    TaskType,
)


def test_telegram_message_formatting():
    task = Task(
        id="issue-55-abc123",
        title="Fix Null Pointer Exception in payment worker",
        description="Payment worker crashes when stripe customer ID is null.",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/acme/pay.git",
        repo_name="acme/pay",
        working_branch="forge/issue-55",
        status=TaskStatus.PENDING_APPROVAL
    )

    alert_text = format_new_task_message(task)
    assert "acme/pay" in alert_text
    assert "Fix Null Pointer Exception" in alert_text
    assert "issue-55-abc123" in alert_text

    keyboard = build_new_task_keyboard(task.id)
    assert len(keyboard.inline_keyboard) == 2
    assert keyboard.inline_keyboard[0][0].callback_data == "approve:issue-55-abc123"
    assert keyboard.inline_keyboard[0][1].callback_data == "reject:issue-55-abc123"

    # Test review message formatting
    task.review_summary = ReviewSummary(
        summary="Added null guard before Stripe API invocation.",
        risk_assessment="Low",
        test_verification="15 unit tests passed.",
        files_changed=["worker.py"],
        suggested_commit_message="fix(worker): null guard customer ID",
        suggested_pr_title="fix: guard null customer ID",
        suggested_pr_body="Guards against null customer ID."
    )
    task.diff_stat = " worker.py | 4 ++--\n 1 file changed, 2 insertions(+), 2 deletions(-)"
    task.test_summary = "Tests: Passed (15/15) using `pytest`"

    review_text = format_review_confirmation_message(task)
    assert "Ready for Confirmation" in review_text
    assert "Low" in review_text
    assert "15 unit tests passed" in review_text

    confirm_keyboard = build_review_confirmation_keyboard(task.id)
    assert confirm_keyboard.inline_keyboard[0][0].callback_data == "confirm:issue-55-abc123"
    assert confirm_keyboard.inline_keyboard[0][1].callback_data == "pick_branch:issue-55-abc123"
    assert confirm_keyboard.inline_keyboard[1][0].callback_data == "revise:issue-55-abc123"
    assert confirm_keyboard.inline_keyboard[1][1].callback_data == "reject:issue-55-abc123"


@pytest.mark.asyncio
async def test_orchestrator_operator_instruction_interpretation():
    from issueforge.agents.orchestrator import SupervisoryOrchestrator

    # Test 1: Task creation prompt
    res = await SupervisoryOrchestrator.interpret_operator_instruction("Please implement a new login page with OAuth support targeting staging")
    assert res["intent"] == "CREATE_AND_RUN_TASK"
    assert "login" in res["task_title"].lower() or "login" in res["task_description"].lower()

    # Test 2: Task action (approve)
    res_act = await SupervisoryOrchestrator.interpret_operator_instruction("Approve and start issue-55-abc123")
    assert res_act["intent"] == "TASK_ACTION"
    assert res_act["task_action"] == "APPROVE"

    # Test 3: Status query
    res_stat = await SupervisoryOrchestrator.interpret_operator_instruction("What is running in the sandbox right now?")
    assert res_stat["intent"] == "STATUS_QUERY"


@pytest.mark.asyncio
async def test_handle_user_text_reply_orchestrator():
    from unittest.mock import AsyncMock, MagicMock, patch
    from issueforge.bot.handlers import handle_user_text_reply
    from issueforge.core.database import init_db

    await init_db()

    mock_msg = MagicMock()
    mock_msg.from_user.id = 12345
    mock_msg.from_user.username = "lead_dev"
    mock_msg.text = "Add dark mode to the dashboard and deploy"
    
    typing_msg = MagicMock()
    typing_msg.edit_text = AsyncMock()
    mock_msg.answer = AsyncMock(return_value=typing_msg)

    from issueforge.core.database import save_task
    from issueforge.core.models import PlatformType, Task

    await save_task(Task(
        id="issue-1-recent", title="earlier", description="d", platform=PlatformType.GITHUB,
        repo_url="https://github.com/acme/web.git", repo_name="acme/web", working_branch="forge/x",
    ))

    with patch("issueforge.bot.handlers.SupervisoryOrchestrator.interpret_operator_instruction",
               new=AsyncMock(return_value={"intent": "CREATE_AND_RUN_TASK", "task_title": "Add dark mode"})), \
         patch("issueforge.bot.handlers.task_queue.create_and_enqueue_task", AsyncMock()) as mock_create:
        mock_task = MagicMock()
        mock_task.id = "task-telegram-123"
        mock_task.title = "Add dark mode"
        mock_task.repo_name = "acme/web"
        mock_task.selected_target_branch = "main"
        mock_create.return_value = mock_task

        await handle_user_text_reply(mock_msg)

        assert mock_create.called
        assert mock_create.call_args.kwargs["auto_approve"] is True
        # The repo comes from the most recent task, and a branch the operator never named
        # is not pre-confirmed, so the run still stops at the branch gate.
        assert mock_create.call_args.kwargs["repo_url"] == "https://github.com/acme/web.git"
        assert mock_create.call_args.kwargs["selected_target_branch"] is None
        assert typing_msg.edit_text.called
        call_text = typing_msg.edit_text.call_args[0][0]
        assert "Supervisory Orchestrator: Task Initiated & Taking Over!" in call_text
        assert "task-telegram-123" in call_text


async def test_takeover_with_no_known_repository_creates_nothing():
    """It used to fall back to a hardcoded personal repository and auto-approve a run."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from issueforge.bot.handlers import handle_user_text_reply

    mock_msg = MagicMock()
    mock_msg.from_user.id = 1
    mock_msg.text = "Add dark mode"
    typing_msg = MagicMock()
    typing_msg.edit_text = AsyncMock()
    mock_msg.answer = AsyncMock(return_value=typing_msg)

    with patch("issueforge.bot.handlers.SupervisoryOrchestrator.interpret_operator_instruction",
               new=AsyncMock(return_value={"intent": "CREATE_AND_RUN_TASK"})), \
         patch("issueforge.bot.handlers.task_queue.create_and_enqueue_task", AsyncMock()) as mock_create:
        await handle_user_text_reply(mock_msg)

    mock_create.assert_not_called()
    assert "which repository" in typing_msg.edit_text.call_args[0][0]


@pytest.mark.parametrize("text,intent", [
    ("add a skill matrix page", "CREATE_AND_RUN_TASK"),
    ("make the restart button blue", "CREATE_AND_RUN_TASK"),
    ("cancel issue-4", "TASK_ACTION"),
])
async def test_fallback_intent_parser_matches_whole_words(text, intent):
    """With the LLM unavailable, "skill" contained "kill" and cancelled the latest task."""
    from unittest.mock import AsyncMock, patch
    from issueforge.agents.orchestrator import SupervisoryOrchestrator

    with patch("issueforge.agents.orchestrator.AgySessionRunner.run_prompt", new=AsyncMock(return_value=(False, ""))), \
         patch("issueforge.agents.orchestrator.call_llm_with_fallback", new=AsyncMock(side_effect=RuntimeError("down"))):
        decision = await SupervisoryOrchestrator.interpret_operator_instruction(text)

    assert decision["intent"] == intent




@pytest.mark.parametrize("allowlist,user_id,expected", [("", 42, False), ("42", 42, True), ("42", 7, False)])
async def test_auth_middleware_fails_closed(monkeypatch, allowlist, user_id, expected):
    from types import SimpleNamespace

    from issueforge.bot.telegram_bot import AuthMiddleware
    from issueforge.config import settings

    monkeypatch.setattr(settings, "telegram_allowed_user_ids_raw", allowlist)
    called = []

    async def handler(event, data):
        called.append(True)

    await AuthMiddleware()(handler, object(), {"event_from_user": SimpleNamespace(id=user_id)})
    assert bool(called) is expected
