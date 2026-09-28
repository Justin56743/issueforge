import pytest
from pathlib import Path
from issueforge.core.sandbox import NativeSandbox, CommandResult


@pytest.mark.asyncio
async def test_sandbox_lifecycle():
    task_id = "test-sandbox-001"
    sandbox = NativeSandbox(task_id)
    
    # Setup
    ws_path = sandbox.setup()
    assert ws_path.exists()
    assert ws_path.is_dir()

    # Write and read file
    test_content = "def hello():\n    return 'world'\n"
    sandbox.write_file("src/hello.py", test_content)
    assert sandbox.file_exists("src/hello.py")
    assert sandbox.read_file("src/hello.py") == test_content

    # List files
    files = sandbox.list_files()
    assert "src/hello.py" in files

    # Command execution
    result = await sandbox.run_command("python3 src/hello.py", emit_events=False)
    assert result.exit_code == 0
    assert result.success

    # Path traversal security guard
    with pytest.raises(ValueError):
        sandbox.resolve_path("../../etc/passwd")

    # Cleanup
    sandbox.cleanup()
    assert not ws_path.exists()


@pytest.mark.asyncio
async def test_sandbox_command_timeout():
    task_id = "test-timeout-002"
    sandbox = NativeSandbox(task_id)
    sandbox.setup()

    result = await sandbox.run_command("sleep 5", timeout=1, emit_events=False)
    assert result.timed_out
    assert not result.success

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_sandbox_streams_history_and_deletion():
    task_id = "test-stream-hist-003"
    sandbox = NativeSandbox(task_id)
    sandbox.setup()

    # Run command and check log & history recording
    res = await sandbox.run_command("echo 'Hello Sandbox Stream'", emit_events=False)
    assert res.success

    log_content = sandbox.get_sandbox_log()
    assert "Hello Sandbox Stream" in log_content
    assert "$ echo 'Hello Sandbox Stream'" in log_content

    history = sandbox.get_execution_history()
    assert len(history) == 1
    assert history[0]["command"] == "echo 'Hello Sandbox Stream'"
    assert history[0]["exit_code"] == 0

    stats = sandbox.get_stats()
    assert stats["exists"] is True
    assert stats["has_log"] is True
    assert stats["command_count"] == 1


    all_sandboxes = NativeSandbox.list_all_sandboxes()
    ids = [sb["task_id"] for sb in all_sandboxes]
    assert task_id in ids

    # Test delete_workspace
    deleted = sandbox.delete_workspace()
    assert deleted is True
    assert not sandbox.workspace_path.exists()
    assert sandbox.get_stats()["exists"] is False


@pytest.mark.asyncio
async def test_sandbox_api_endpoints():
    from httpx import AsyncClient, ASGITransport
    from issueforge.main import app

    task_id = "test-api-sandbox-004"
    sandbox = NativeSandbox(task_id)
    sandbox.setup()
    await sandbox.run_command("echo 'API stream log verification'", emit_events=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /sandboxes HTML page
        resp = await client.get("/sandboxes")
        assert resp.status_code == 200
        assert "Sandbox Workspaces" in resp.text

        # GET /api/sandboxes JSON
        resp = await client.get("/api/sandboxes")
        assert resp.status_code == 200
        data = resp.json()
        assert any(sb["task_id"] == task_id for sb in data)

        # GET /api/sandboxes/{task_id}/log
        resp = await client.get(f"/api/sandboxes/{task_id}/log")
        assert resp.status_code == 200
        assert "API stream log verification" in resp.text

        # DELETE /api/sandboxes/{task_id}
        resp = await client.delete(f"/api/sandboxes/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["deleted"] is True

    assert not sandbox.workspace_path.exists()


@pytest.mark.asyncio
async def test_sandbox_realtime_streaming_and_termination():
    import asyncio
    task_id = "test-stream-term-005"
    sandbox = NativeSandbox(task_id)
    sandbox.setup()

    # 1. Verify multi-line command streams lines into sandbox.log
    cmd = "echo 'Line 1' && echo 'Line 2' && echo 'Line 3'"
    res = await sandbox.run_command(cmd, emit_events=False)
    assert res.success
    log = sandbox.get_sandbox_log()
    assert "Line 1" in log
    assert "Line 2" in log
    assert "Line 3" in log
    assert "Status: SUCCESS" in log

    # 2. Verify process termination capability
    bg_task = asyncio.create_task(sandbox.run_command("sleep 10", emit_events=False))
    # Give it a fraction of a second to spawn the process
    await asyncio.sleep(0.2)
    assert task_id in NativeSandbox._active_processes
    proc = NativeSandbox._active_processes[task_id]
    assert proc.returncode is None

    # Terminate the process
    terminated = NativeSandbox.terminate_task_process(task_id)
    assert terminated is True
    res_term = await bg_task
    assert not res_term.success

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_task_cancellation_and_stop_api():
    from httpx import AsyncClient, ASGITransport
    from issueforge.main import app
    from issueforge.core.queue import task_queue
    from issueforge.core.models import TaskStatus, PlatformType, TaskType

    task = await task_queue.create_and_enqueue_task(
        title="Test Cancel Task",
        description="Verify stop task capability",
        repo_url="https://gitlab.com/test/repo.git",
        task_type=TaskType.MANUAL,
        platform=PlatformType.MANUAL
    )
    assert task.status == TaskStatus.PENDING_APPROVAL

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # POST /api/tasks/{task_id}/stop
        resp = await client.post(f"/api/tasks/{task.id}/stop", json={"reason": "User stopped execution"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["task"]["status"] == "CANCELLED"
        assert data["task"]["error_message"] == "User stopped execution"

        # Check GET /api/tasks/{task_id}
        resp = await client.get(f"/api/tasks/{task.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "CANCELLED"
