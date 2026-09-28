import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from issueforge.config import settings
from issueforge.core.database import init_db, save_task
from issueforge.core.models import PlatformType, Task, TaskStatus, TaskType
from issueforge.core.task_dossier import TaskDossierManager
from issueforge.main import app

client = TestClient(app)


@pytest.fixture
async def setup_test_task_and_files(tmp_path):
    await init_db()
    task = Task(
        id="test-file-tree-task",
        title="File Tree Task",
        description="Testing File Tree & Editor",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITLAB,
        repo_url="https://gitlab.com/org/repo.git",
        repo_name="org/repo",
        working_branch="forge/test-files",
        status=TaskStatus.CODING
    )
    await save_task(task)


    with patch.object(settings, "forge_tasks_root", tmp_path):
        sandbox_dir = tmp_path / task.id / "sandboxes" / "run-1"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        (sandbox_dir / "src").mkdir()
        (sandbox_dir / "src" / "main.py").write_text("print('hello world')", encoding="utf-8")
        (sandbox_dir / "README.md").write_text("# Project Docs", encoding="utf-8")

        yield task, tmp_path


@pytest.mark.asyncio
async def test_get_run_file_tree(setup_test_task_and_files):
    task, tmp_path = setup_test_task_and_files

    with patch.object(settings, "forge_tasks_root", tmp_path):
        res = client.get(f"/api/tasks/{task.id}/runs/run-1/files/tree")
        assert res.status_code == 200
        data = res.json()
        assert data["task_id"] == task.id
        assert data["run_id"] == "run-1"

        tree = data["tree"]
        file_paths = [f["path"] for f in tree]
        assert "README.md" in file_paths
        assert "src/main.py" in file_paths

        # Check metadata
        main_file = next(f for f in tree if f["path"] == "src/main.py")
        assert main_file["name"] == "main.py"
        assert main_file["extension"] == "py"
        assert main_file["size"] > 0


@pytest.mark.asyncio
async def test_get_and_save_sandbox_file_content(setup_test_task_and_files):
    task, tmp_path = setup_test_task_and_files

    with patch.object(settings, "forge_tasks_root", tmp_path):
        # 1. Fetch file content
        res = client.get(f"/api/tasks/{task.id}/runs/run-1/files/content?path=src/main.py")
        assert res.status_code == 200
        data = res.json()
        assert data["content"] == "print('hello world')"

        # 2. Save modified file content
        new_content = "print('hello from web studio editor')"
        post_res = client.post(
            f"/api/tasks/{task.id}/runs/run-1/files/content",
            json={"path": "src/main.py", "content": new_content}
        )
        assert post_res.status_code == 200
        assert post_res.json()["success"] is True

        # Verify modification on disk
        saved_file = tmp_path / task.id / "sandboxes" / "run-1" / "src" / "main.py"
        assert saved_file.read_text(encoding="utf-8") == new_content


@pytest.mark.asyncio
async def test_file_traversal_protection(setup_test_task_and_files):
    task, tmp_path = setup_test_task_and_files

    with patch.object(settings, "forge_tasks_root", tmp_path):
        # Directory traversal attempt
        res = client.get(f"/api/tasks/{task.id}/runs/run-1/files/content?path=../../etc/passwd")
        assert res.status_code in (403, 404)

        save_res = client.post(
            f"/api/tasks/{task.id}/runs/run-1/files/content",
            json={"path": "../../etc/malicious.sh", "content": "malicious"}
        )
        assert save_res.status_code in (403, 404, 500)
