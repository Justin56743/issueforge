import pytest
from fastapi.testclient import TestClient

from issueforge.core.database import init_db, save_task
from issueforge.core.models import (
    PipelineRun,
    PlatformType,
    Task,
    TaskStatus,
    TaskType,
)
from issueforge.main import app

client = TestClient(app)


@pytest.fixture
async def setup_codespace_task():
    await init_db()
    task = Task(
        id="test-codespace-task",
        title="Implement Codespace View",
        description="Testing Codespace Standalone Tab",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/codespace-test.git",
        repo_name="org/codespace-test",
        working_branch="forge/codespace-view",
        active_run_id="run-1",
        runs=[
            PipelineRun(run_id="run-1", attempt_number=1, status=TaskStatus.CODING),
            PipelineRun(run_id="run-2", attempt_number=2, status=TaskStatus.TESTING)
        ],
        status=TaskStatus.CODING
    )
    await save_task(task)
    return task


@pytest.mark.asyncio
async def test_get_codespace_view(setup_codespace_task):
    task = setup_codespace_task
    res = client.get(f"/tasks/{task.id}/codespace")
    assert res.status_code == 200
    html = res.text

    # Title and header
    assert "Issueforge Agent Studio" in html
    assert task.title in html
    assert task.id in html

    # Explorer and file tree with search filter
    assert 'id="studioFileTreeList"' in html
    assert 'id="studioFileSearch"' in html
    assert 'id="studioFilePathLabel"' in html
    assert 'id="studioEditorTextarea"' in html
    assert "(Ctrl+S)" in html

    # Tabbed multi-file editor & diff view toggles
    assert 'id="studioTabsList"' in html
    assert 'id="studioModeCodeBtn"' in html
    assert 'id="studioModeDiffBtn"' in html
    assert 'id="studioDiffWrapper"' in html

    # Dual-view: AGY Rich Stream & Live Agent terminal feed
    assert 'id="studioRichStreamContainer"' in html
    assert 'id="richStreamCardsList"' in html
    assert 'id="streamTabRichBtn"' in html
    assert 'id="streamTabTermBtn"' in html
    assert 'id="terminalXtermContainer"' in html
    assert "Live Agent Session (AGY)" in html

    # Co-Pilot mid-flight steering
    assert "Co-Pilot Mid-Flight Steering" in html
    assert 'id="steeringDirectiveInput"' in html


@pytest.mark.asyncio
async def test_get_file_diff_api(setup_codespace_task):
    task = setup_codespace_task
    # Test diff endpoint without file_path
    res = client.get(f"/api/tasks/{task.id}/files/diff")
    assert res.status_code == 200
    data = res.json()
    assert data["task_id"] == task.id
    assert "diff" in data

    # Test diff endpoint with nonexistent task returns 404
    missing_res = client.get("/api/tasks/nonexistent-task-diff-1234/files/diff")
    assert missing_res.status_code == 404


@pytest.mark.asyncio
async def test_get_codespace_view_with_run_param(setup_codespace_task):
    task = setup_codespace_task
    res = client.get(f"/tasks/{task.id}/codespace?run=run-2")
    assert res.status_code == 200
    html = res.text
    assert 'value="run-2" selected' in html or 'window.currentActiveRunId = "run-2"' in html


@pytest.mark.asyncio
async def test_codespace_view_not_found():
    res = client.get("/tasks/nonexistent-task-id-12345/codespace")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_task_detail_page_codespace_launcher_and_clean_stream(setup_codespace_task):
    task = setup_codespace_task
    res = client.get(f"/tasks/{task.id}")
    assert res.status_code == 200
    html = res.text

    # Codespace launcher button present
    assert "Open Agent Codespace ↗" in html
    assert f'/tasks/{task.id}/codespace?run=run-1' in html

    # Interactive Shell and in-page duplicate Studio removed
    assert "tabStreamShell" not in html
    assert "sandboxShellContainer" not in html
    assert "steeringDirectiveInput" not in html
    assert "studioEditorTextarea" not in html


@pytest.mark.asyncio
async def test_file_diff_path_is_not_shell_interpreted(setup_codespace_task, tmp_path):
    from issueforge.core.sandbox import NativeSandbox

    task = setup_codespace_task
    NativeSandbox(task.id, run_id="run-1").workspace_path.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "pwned"

    res = client.get(f"/api/tasks/{task.id}/files/diff", params={"file_path": f"x; touch {marker}"})
    assert res.status_code == 200
    assert not marker.exists()
