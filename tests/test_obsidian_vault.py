import json
from pathlib import Path

import pytest
import yaml

from issueforge.config import Settings, settings
from issueforge.core.database import (
    delete_task,
    get_graph_backlinks,
    get_task_graph_edges,
    replace_task_graph_edges,
    save_task,
)
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
from issueforge.vault.linker import VaultLinkIndexer, reset_fingerprint_cache, sync_task_graph, wikify
from issueforge.vault.manager import VaultManager


def _make_task(task_id: str = "test-vault-001", **overrides) -> Task:
    defaults = dict(
        id=task_id,
        title="Add Prometheus Metrics Exporter",
        description="Expose Jetson GPU metrics.",
        repo_url="https://gitlab.com/acme/telemetry",
        repo_name="acme/telemetry",
        working_branch="forge/issue-metrics",
        base_branch="main",
        selected_target_branch="develop",
        target_branch_candidates=["main", "develop", "staging"],
        issue_number=142,
        priority="Priority::High",
        status=TaskStatus.PLANNING,
        active_run_id="run-2",
        platform=PlatformType.GITLAB,
        task_type=TaskType.ISSUE,
        collaborators=TaskCollaborators(
            author="dev_lead", assignees=["@ops_eng"], participants=["reviewer_x"]
        ),
        subtasks=[
            TaskSubtask(id="st-1", title="Setup Prometheus client", completed=True),
            TaskSubtask(id="st-2", title="Add Jetson GPU metrics exporter"),
        ],
        runs=[
            PipelineRun(run_id="run-1", attempt_number=1, status=TaskStatus.FAILED, failure_stage="TESTER"),
            PipelineRun(run_id="run-2", attempt_number=2, status=TaskStatus.COMPLETED),
        ],
    )
    defaults.update(overrides)
    return Task(**defaults)


# --------------------------------------------------------------------- config

def test_tasks_root_derives_from_vault_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.delenv("FORGE_TASKS_ROOT", raising=False)
    monkeypatch.delenv("FORGE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("FORGE_DB_PATH", raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.forge_tasks_root == tmp_path / "vault" / "tasks"
    assert fresh.forge_workspace_root == tmp_path / "vault" / "workspaces"
    assert fresh.forge_db_path == tmp_path / "vault" / "issueforge.db"


def test_explicit_tasks_root_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("FORGE_TASKS_ROOT", str(tmp_path / "elsewhere"))
    fresh = Settings(_env_file=None)
    assert fresh.forge_tasks_root == tmp_path / "elsewhere"
    # The vault root is untouched, so .obsidian/ still lives in the vault.
    assert fresh.forge_vault_root == tmp_path / "vault"


def test_default_vault_root_is_home_issueforge(monkeypatch):
    for var in ("FORGE_VAULT_ROOT", "FORGE_TASKS_ROOT", "FORGE_WORKSPACE_ROOT", "FORGE_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.forge_vault_root == Path.home() / ".issueforge"
    assert fresh.forge_tasks_root == Path.home() / ".issueforge" / "tasks"


def test_ensure_directories_expands_and_creates(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "forge_vault_root", tmp_path / "v2")
    monkeypatch.setattr(settings, "forge_tasks_root", tmp_path / "v2" / "tasks")
    settings.ensure_directories()
    assert settings.forge_vault_root.is_absolute() and settings.forge_vault_root.exists()
    assert settings.forge_tasks_root.exists()
    # A literal "~" directory is the classic resolve()-before-expanduser() bug.
    assert not Path("~").exists()


# --------------------------------------------------------------------- vault manager

def test_vault_initialization_creates_tree(tmp_path):
    vm = VaultManager(tmp_path / "vault")
    vm.initialize_vault()
    for d in (vm.obsidian_dir, vm.tasks_dir, vm.knowledge_dir, vm.graph_dir):
        assert d.is_dir()
    assert (vm.obsidian_dir / "app.json").exists()
    assert (vm.obsidian_dir / "graph.json").exists()


def test_app_json_schema(tmp_path):
    vm = VaultManager(tmp_path / "vault")
    vm.initialize_vault()
    cfg = json.loads((vm.obsidian_dir / "app.json").read_text())
    assert cfg["legacyEditor"] is False
    assert cfg["livePreview"] is True
    assert cfg["showLineNumber"] is True
    assert cfg["tabSize"] == 4


def test_graph_json_color_groups_and_forces(tmp_path):
    vm = VaultManager(tmp_path / "vault")
    vm.initialize_vault()
    cfg = json.loads((vm.obsidian_dir / "graph.json").read_text())
    by_query = {g["query"]: g["color"]["rgb"] for g in cfg["colorGroups"]}
    assert by_query["tag:#issueforge/task"] == 4317676
    assert by_query["tag:#issueforge/knowledge"] == 16744448
    assert by_query["tag:#issueforge/ast"] == 2201331
    assert by_query["tag:#priority/critical"] == 16711680
    assert cfg["forces"]["nodeStrength"] == -230
    # Issue-* / Branch-* nodes have no backing file; they must still render.
    assert cfg["hideUnresolved"] is False


def test_initialize_vault_preserves_user_config(tmp_path):
    vm = VaultManager(tmp_path / "vault")
    vm.initialize_vault()
    app_json = vm.obsidian_dir / "app.json"
    app_json.write_text('{"tabSize": 99}')
    vm.initialize_vault()
    assert json.loads(app_json.read_text())["tabSize"] == 99


def test_vault_manager_expands_tilde_before_resolve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vm = VaultManager(Path("~/.issueforge-vault-test"))
    assert vm.vault_root == (Path.home() / ".issueforge-vault-test").resolve()
    assert not (tmp_path / "~").exists()


# --------------------------------------------------------------------- linker

def test_wikilink_extraction_variants():
    text = "[[Alpha]] [[Beta|Label]] [[Gam#Head]] [[Del#S|A]] [[ Sp ]] [single] [[]]"
    assert VaultLinkIndexer.extract_links_from_text(text) == {"Alpha", "Beta", "Gam", "Del", "Sp"}


def test_extract_links_missing_file_returns_empty_set(tmp_path):
    assert VaultLinkIndexer.extract_links(tmp_path / "nope.md") == set()


def test_wikify_strips_unsafe_characters():
    # "/" inside [[...]] would mean a vault path in Obsidian.
    assert "/" not in wikify("feature/new-thing")
    assert ":" not in wikify("Priority::High")


async def test_task_graph_edge_sync_replaces_previous():
    await replace_task_graph_edges("t-graph-1", ["Repo-a", "Branch-a-main"])
    assert await get_task_graph_edges("t-graph-1") == ["Branch-a-main", "Repo-a"]
    await replace_task_graph_edges("t-graph-1", ["Repo-a", "Issue-a-7"])
    assert await get_task_graph_edges("t-graph-1") == ["Issue-a-7", "Repo-a"]


async def test_task_graph_edges_deduplicate():
    await replace_task_graph_edges("t-graph-2", ["Repo-a", "Repo-a", " Repo-a ", ""])
    assert await get_task_graph_edges("t-graph-2") == ["Repo-a"]


async def test_graph_backlinks():
    await replace_task_graph_edges("t-back-1", ["Repo-shared"])
    await replace_task_graph_edges("t-back-2", ["Repo-shared"])
    assert set(await get_graph_backlinks("Repo-shared")) == {"t-back-1", "t-back-2"}


async def test_delete_task_removes_graph_edges():
    task = _make_task("test-del-edges")
    await save_task(task)
    await replace_task_graph_edges(task.id, ["Repo-a", "Branch-a-main"])
    await delete_task(task.id)
    assert await get_task_graph_edges(task.id) == []


async def test_sync_task_graph_indexes_readme_and_debounces():
    reset_fingerprint_cache()
    task = _make_task("test-sync-graph")
    await save_task(task)
    await TaskDossierManager.sync_dossier_async(task)
    links = await get_task_graph_edges(task.id)
    assert "Repo-acme-telemetry" in links
    assert "Issue-gitlab-acme-telemetry-142" in links
    # The task's own canvas is namespaced so it does not become a shared graph node.
    assert f"{task.id}/task_dag.canvas" in links
    assert "task_dag.canvas" not in links

    # Unchanged links must not re-write rows; force=True must.
    assert await sync_task_graph(task) == sorted(links)
    assert await sync_task_graph(task, force=True) == sorted(links)


# --------------------------------------------------------------------- dossier README

def test_readme_frontmatter_is_valid_yaml():
    task = _make_task("test-fm-001")
    TaskDossierManager.sync_dossier(task)
    text = TaskDossierManager.get_task_readme(task.id)
    assert text.startswith("---")
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["task_id"] == "test-fm-001"
    assert fm["status"] == "PLANNING"
    assert fm["active_run"] == "run-2"
    assert fm["selected_target_branch"] == "develop"
    assert fm["candidate_branches"] == ["main", "develop", "staging"]
    assert "issueforge/task" in fm["tags"]
    assert "priority/high" in fm["tags"]
    assert "status/planning" in fm["tags"]


def test_readme_frontmatter_survives_hostile_title():
    nasty = 'He said "yes": `code` #hash $$\\text{x}$$ - [ ] item'
    task = _make_task("test-fm-002", title=nasty)
    TaskDossierManager.sync_dossier(task)
    text = TaskDossierManager.get_task_readme(task.id)
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["title"] == nasty


def test_readme_wikilinks():
    task = _make_task("test-links-001")
    TaskDossierManager.sync_dossier(task)
    text = TaskDossierManager.get_task_readme(task.id)
    for node in (
        "[[Repo-acme-telemetry]]",
        "[[Branch-acme-telemetry-develop]]",
        "[[Issue-gitlab-acme-telemetry-142]]",
        "[[Person-dev_lead]]",
        "[[task_dag.canvas]]",
    ):
        assert node in text, node
    # No self-link: README.md inside tasks/{id}/ can never resolve [[{id}]].
    assert f"[[{task.id}]]" not in text


def test_readme_escapes_user_supplied_wikilinks():
    task = _make_task("test-escape-001", description="see [[evil-node]] here")
    TaskDossierManager.sync_dossier(task)
    text = TaskDossierManager.get_task_readme(task.id)
    assert "[[evil-node]]" not in text
    assert "evil-node" not in VaultLinkIndexer.extract_links_from_text(text)


def test_readme_stays_backward_compatible():
    """Guards the exact substrings tests/test_task_dossier.py and the /folders UI rely on."""
    task = _make_task("test-compat-001")
    TaskDossierManager.sync_dossier(task)
    text = TaskDossierManager.get_task_readme(task.id)
    for expected in (
        "Task Dossier: Add Prometheus Metrics Exporter",
        "dev_lead",
        "[x] Setup Prometheus client",
        "[ ] Add Jetson GPU metrics exporter",
        "| `run-1` | 1 | ❌ FAILED | TESTER |",
        "| `run-2` | 2 | ✅ COMPLETED |",
    ):
        assert expected in text, expected


def test_sync_dossier_writes_plan_and_canvas():
    task = _make_task("test-plan-001", plan="## Step 1\nDo the thing.")
    task_dir = TaskDossierManager.sync_dossier(task)
    assert (task_dir / "PLAN.md").exists()
    assert (task_dir / "task_dag.canvas").exists()
    assert "[[PLAN]]" in TaskDossierManager.get_task_readme(task.id)


def test_sync_dossier_without_plan_writes_no_plan_file():
    task = _make_task("test-plan-002", plan=None)
    task_dir = TaskDossierManager.sync_dossier(task)
    assert not (task_dir / "PLAN.md").exists()
    assert "[[PLAN]]" not in TaskDossierManager.get_task_readme(task.id)
