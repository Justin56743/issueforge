import json
import pytest
from pathlib import Path

from issueforge.core.models import (
    PipelineRun,
    PlatformType,
    Task,
    TaskCollaborators,
    TaskStatus,
    TaskSubtask,
    TaskType,
)
from issueforge.core.task_dossier import TaskDossierManager
from issueforge.core.sandbox import NativeSandbox


@pytest.mark.asyncio
async def test_task_dossier_creation_and_sync():
    task = Task(
        id="test-dossier-001",
        title="Add Prometheus Metrics Exporter",
        description="Expose /metrics endpoint with system telemetry for Jetson Orin.",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITLAB,
        repo_url="https://gitlab.com/acme/telemetry.git",
        repo_name="acme/telemetry",
        base_branch="main",
        working_branch="forge/issue-metrics",
        status=TaskStatus.PLANNING,
        collaborators=TaskCollaborators(
            author="dev_lead",
            assignees=["engineer_1"],
            participants=["@dev_lead", "@reviewer_1"]
        ),
        subtasks=[
            TaskSubtask(id="st-1", title="Setup Prometheus client", completed=True),
            TaskSubtask(id="st-2", title="Add Jetson GPU metrics exporter", completed=False),
        ],
        runs=[
            PipelineRun(
                run_id="run-1",
                attempt_number=1,
                status=TaskStatus.FAILED,
                failure_stage="TESTER",
                error_message="Test runner exited with code 1",
                duration_seconds=12.4
            ),
            PipelineRun(
                run_id="run-2",
                attempt_number=2,
                status=TaskStatus.COMPLETED,
                duration_seconds=22.1,
                test_summary="Tests: Passed (5/5) using `pytest`"
            )
        ],
        active_run_id="run-2"
    )

    task_dir = TaskDossierManager.sync_dossier(task)
    assert task_dir.exists()
    assert (task_dir / "sandboxes").exists()
    assert (task_dir / "metadata").exists()

    # Check task_summary.json
    summary_file = task_dir / "task_summary.json"
    assert summary_file.exists()
    summary_data = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary_data["id"] == "test-dossier-001"
    assert summary_data["collaborators"]["author"] == "dev_lead"
    assert len(summary_data["subtasks"]) == 2
    assert len(summary_data["runs"]) == 2
    assert summary_data["runs"][0]["failure_stage"] == "TESTER"
    assert summary_data["runs"][1]["status"] == "COMPLETED"

    # Check README.md
    readme_file = task_dir / "README.md"
    assert readme_file.exists()
    readme_content = readme_file.read_text(encoding="utf-8")
    assert "Task Dossier: Add Prometheus Metrics Exporter" in readme_content
    assert "dev_lead" in readme_content
    assert "[x] Setup Prometheus client" in readme_content
    assert "[ ] Add Jetson GPU metrics exporter" in readme_content
    assert "| `run-1` | 1 | ❌ FAILED | TESTER |" in readme_content
    assert "| `run-2` | 2 | ✅ COMPLETED |" in readme_content

    # Check NativeSandbox with run_id
    sb_run1 = NativeSandbox(task.id, run_id="run-1")
    sb_run1.setup()
    sb_run1.write_file("output.txt", "Run 1 output")

    sb_run2 = NativeSandbox(task.id, run_id="run-2")
    sb_run2.setup()
    sb_run2.write_file("output.txt", "Run 2 output")

    assert sb_run1.read_file("output.txt") == "Run 1 output"
    assert sb_run2.read_file("output.txt") == "Run 2 output"
    assert sb_run1.workspace_path != sb_run2.workspace_path

    # Clean up
    sb_run1.delete_workspace()
    sb_run2.delete_workspace()


@pytest.mark.asyncio
async def test_task_dossier_api_endpoints():
    from httpx import AsyncClient, ASGITransport
    from issueforge.main import app
    from issueforge.core.database import save_task, init_db

    await init_db()

    task = Task(
        id="test-dossier-api-002",
        title="Implement Telemetry Collector",
        description="Collecting hardware metrics for Jetson Nano Orin.",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/example/orin.git",
        repo_name="example/orin",
        base_branch="main",
        working_branch="forge/orin-telemetry",
        status=TaskStatus.COMPLETED,
        collaborators=TaskCollaborators(
            author="lead_dev",
            assignees=["dev1"],
            participants=["@lead_dev", "@qa"]
        ),
        subtasks=[
            TaskSubtask(id="st-1", title="Write collector", completed=True)
        ],
        runs=[
            PipelineRun(
                run_id="run-1",
                attempt_number=1,
                status=TaskStatus.FAILED,
                failure_stage="CODER",
                error_message="SyntaxError in code generation",
                duration_seconds=15.0
            ),
            PipelineRun(
                run_id="run-2",
                attempt_number=2,
                status=TaskStatus.COMPLETED,
                duration_seconds=20.0,
                test_summary="Passed all tests"
            )
        ],
        active_run_id="run-2"
    )

    await save_task(task)
    TaskDossierManager.sync_dossier(task)

    # Create dummy sandbox run
    sb = NativeSandbox(task.id, run_id="run-2")
    sb.setup()
    sb.write_file("metrics.py", "print('metrics')")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Folders HTML page
        res_html = await client.get("/folders")
        assert res_html.status_code == 200
        assert "Task Dossiers &amp; Project Folders" in res_html.text or "Task Dossiers & Project Folders" in res_html.text
        assert "Implement Telemetry Collector" in res_html.text

        # 2. Folders JSON API
        res_folders = await client.get("/api/folders")
        assert res_folders.status_code == 200
        folders_data = res_folders.json()
        assert any(d["id"] == "test-dossier-api-002" for d in folders_data)

        # 3. Task Dossier Readme API
        res_readme = await client.get(f"/api/tasks/{task.id}/dossier/readme")
        assert res_readme.status_code == 200
        assert "Task Dossier: Implement Telemetry Collector" in res_readme.text

        # 4. Task Runs API
        res_runs = await client.get(f"/api/tasks/{task.id}/runs")
        assert res_runs.status_code == 200
        runs_data = res_runs.json()
        assert runs_data["task_id"] == task.id
        assert len(runs_data["runs"]) == 2
        assert runs_data["runs"][0]["run_id"] == "run-1"
        assert runs_data["runs"][1]["run_id"] == "run-2"

        # 5. Sandbox Stats API with run_id
        res_sb = await client.get(f"/api/sandboxes/{task.id}?run_id=run-2")
        assert res_sb.status_code == 200
        sb_data = res_sb.json()
        assert sb_data["task_id"] == task.id
        assert sb_data["run_id"] == "run-2"
        assert sb_data["file_count"] >= 1

    # Cleanup
    sb.delete_workspace()

