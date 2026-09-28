import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from issueforge.config import settings
from issueforge.core.terminal_manager import terminal_manager, TerminalSession
from issueforge.main import app

client = TestClient(app)


def test_terminal_session_write_and_history(tmp_path):
    """Test TerminalSession writing, buffering, and persistence to disk."""
    task_id = "test-term-task-1"
    run_id = "run-1"

    with patch.object(settings, "forge_tasks_root", tmp_path):
        session = TerminalSession(task_id=task_id, run_id=run_id)

        test_ansi = "\x1b[32m[ISSUEFORGE AGENT SESSION] 🤖 CODER\x1b[0m\r\n$ agy --version\r\n"
        session.append_text(test_ansi)

        history = session.get_history()
        assert test_ansi.encode("utf-8") in history

        # Verify disk persistence
        raw_path = tmp_path / task_id / "metadata" / run_id / "terminal.raw"
        assert raw_path.exists()
        disk_content = raw_path.read_bytes()
        assert test_ansi.encode("utf-8") in disk_content


def test_terminal_session_resize_and_input():
    """Test resize and input dispatching without raising errors."""
    session = TerminalSession(task_id="test-task", run_id="run-1")

    # When no real PTY fd is open, set_window_size and write_input return False safely
    assert session.set_window_size(100, 30) is False
    assert session.write_input(b"echo 'hello'\n") is False


def test_terminal_session_manager_registry(tmp_path):
    """Test TerminalSessionManager session creation and caching."""
    with patch.object(settings, "forge_tasks_root", tmp_path):
        s1 = terminal_manager.get_session("task-abc", "run-1")
        s2 = terminal_manager.get_session("task-abc", "run-1")
        assert s1 is s2

        s3 = terminal_manager.get_session("task-abc", "run-2")
        assert s3 is not s1


@pytest.mark.asyncio
async def test_terminal_websocket_endpoint(tmp_path):
    """Test WebSocket /ws/tasks/{task_id}/terminal endpoint connection and bidirectional messaging."""
    task_id = "test-ws-term-task"
    run_id = "run-1"

    with patch.object(settings, "forge_tasks_root", tmp_path):
        # Create session with initial history
        session = terminal_manager.get_session(task_id, run_id)
        session.append_text("\x1b[36mConnected to ISSUEFORGE PTY\x1b[0m\r\n")

        with client.websocket_connect(f"/ws/tasks/{task_id}/terminal?run_id={run_id}") as ws:
            # First message should be the rehydrated history sent as bytes
            initial_bytes = ws.receive_bytes()
            assert b"Connected to ISSUEFORGE PTY" in initial_bytes

            # Test sending a resize command from client
            ws.send_json({"type": "resize", "cols": 90, "rows": 25})

            # Test sending keyboard input
            ws.send_json({"type": "input", "data": "ls\r"})

            # Test broadcasting from session to websocket
            await session.broadcast_text("Live Stream Message\r\n")
            live_bytes = ws.receive_bytes()
            assert b"Live Stream Message" in live_bytes


def test_terminal_raw_rest_endpoint(tmp_path):
    """Test GET /api/tasks/{task_id}/runs/{run_id}/terminal REST endpoint."""
    task_id = "test-rest-term-task"
    run_id = "run-1"

    with patch.object(settings, "forge_tasks_root", tmp_path):
        session = terminal_manager.get_session(task_id, run_id)
        session.append_text("Historical ANSI Output")

        res = client.get(f"/api/tasks/{task_id}/runs/{run_id}/terminal")
        assert res.status_code == 200
        assert "Historical ANSI Output" in res.text


def test_interactive_shell_session_lifecycle(tmp_path):
    """Test InteractiveShellSession start, write, resize, and stop."""
    from issueforge.core.terminal_manager import InteractiveShellSession

    sandbox_cwd = tmp_path / "sandbox-workspace"
    shell = InteractiveShellSession(task_id="test-shell", run_id="run-1", cwd=sandbox_cwd)
    shell.start()

    assert shell._running is True
    assert shell.master_fd is not None

    # Test resize
    assert shell.set_window_size(100, 30) is True

    # Test writing input
    assert shell.write_input(b"echo 'shell ready'\n") is True

    # Stop session
    shell.stop()
    assert shell._running is False
    assert shell.master_fd is None


@pytest.mark.asyncio
async def test_steer_task_api(tmp_path):
    """Test POST /api/tasks/{task_id}/steer endpoint."""
    from issueforge.core.database import init_db, save_task
    from issueforge.core.models import PlatformType, Task, TaskStatus, TaskType

    await init_db()
    task = Task(
        id="test-steer-task",
        title="Steerable Task",
        description="Testing mid-flight steering",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        working_branch="forge/test-steer",
        status=TaskStatus.CODING
    )
    await save_task(task)


    with patch.object(settings, "forge_tasks_root", tmp_path):
        res = client.post("/api/tasks/test-steer-task/steer", json={"directive": "Focus on security headers"})
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["directive"] == "Focus on security headers"

        # Verify empty directive fails with 400
        bad_res = client.post("/api/tasks/test-steer-task/steer", json={"directive": "   "})
        assert bad_res.status_code == 400

