"""Phase 0 security fixes: path ids, dashboard XSS sinks, shipped config, shutdown."""
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from issueforge.config import settings
from issueforge.core.database import save_question, save_task
from issueforge.core.models import PlatformType, Task, TaskQuestion, UnsafeIdError, safe_path_id
from issueforge.main import app

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(app)


@pytest.mark.parametrize("bad", ["..", ".", "", "a/b", "../x", "/etc", "a\\b", "x" * 200])
def test_unsafe_ids_are_rejected(bad):
    with pytest.raises(UnsafeIdError):
        safe_path_id(bad)


@pytest.mark.parametrize("good", ["issue-12-a1b2c3", "run-1", "test-confirm-guard-pending_approval", "op-1"])
def test_generated_ids_are_accepted(good):
    assert safe_path_id(good) == good


def test_deleting_dot_dot_no_longer_wipes_the_vault():
    """`DELETE /api/tasks/%2E%2E` resolved to the vault root and rmtree'd it."""
    marker = settings.forge_vault_root / "keep.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("still here")

    res = client.delete("/api/tasks/%2E%2E")

    assert res.status_code == 400
    assert marker.read_text() == "still here"


def test_every_markdown_render_is_sanitized():
    js = (ROOT / "issueforge/web/static/js/dashboard.js").read_text()
    # marked.parse appears exactly once: inside renderMarkdown, wrapped by DOMPurify.
    assert js.count("marked.parse(") == 1
    assert "DOMPurify.sanitize(marked.parse(" in js
    base = (ROOT / "issueforge/web/templates/base.html").read_text()
    assert base.index("vendor/purify.min.js") < base.index("js/dashboard.js")
    assert (ROOT / "issueforge/web/static/vendor/purify.min.js").stat().st_size > 10_000


def test_inline_handlers_encode_apostrophes():
    js = (ROOT / "issueforge/web/static/js/dashboard.js").read_text()
    assert not re.search(r"onclick=\"[^\"]*'\$\{encodeURIComponent\(", js)


async def test_llm_written_question_options_cannot_break_out_of_the_handler():
    payload = "x'); alert(document.cookie); ('"
    task = Task(
        id="test-xss-question", title="t", description="d", platform=PlatformType.GITHUB,
        repo_url="https://github.com/o/r.git", repo_name="o/r", working_branch="forge/x",
    )
    await save_task(task)
    await save_question(TaskQuestion(id="q-1", task_id=task.id, question="Q?", options=[payload]))

    html = client.get("/tasks/test-xss-question").text

    handler = re.search(r"onclick='submitQuestionAnswer\(([^']*)\)'", html)
    assert handler, "question option button not rendered"
    # The whole payload stays inside the single-quoted attribute: its apostrophes are
    # \u0027 escapes, so the attribute regex captures through to alert(...).
    assert "alert(document.cookie)" in handler.group(1)


async def test_codespace_rejects_a_run_that_is_not_a_plain_id():
    task = Task(
        id="test-codespace-run", title="t", description="d", platform=PlatformType.GITHUB,
        repo_url="https://github.com/o/r.git", repo_name="o/r", working_branch="forge/x",
    )
    await save_task(task)
    assert client.get('/tasks/test-codespace-run/codespace?run=x"%3Balert(1)%2F%2F').status_code == 400
    assert client.get("/tasks/test-codespace-run/codespace?run=run-1").status_code == 200


def test_env_example_ships_no_usable_secrets():
    """Copied as-is, placeholder webhook secrets exempted the webhooks from the token."""
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    for key in ("GITHUB_WEBHOOK_SECRET", "GITLAB_WEBHOOK_SECRET", "GITHUB_TOKEN", "GITLAB_TOKEN",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS"):
        assert values[key] == "", key


async def test_telegram_polling_leaves_signal_handling_to_uvicorn(monkeypatch):
    from issueforge.bot.telegram_bot import TelegramBotManager

    manager = TelegramBotManager()
    manager.bot = MagicMock()
    manager.bot.get_me = AsyncMock(return_value=MagicMock(username="b", id=1))
    manager.dp = MagicMock()
    manager.dp.start_polling = AsyncMock()
    monkeypatch.setattr(settings, "telegram_allowed_user_ids_raw", "1")

    with patch.object(TelegramBotManager, "initialize", return_value=True):
        await manager.start()

    manager.dp.start_polling.assert_called_once()
    assert manager.dp.start_polling.call_args.kwargs["handle_signals"] is False
