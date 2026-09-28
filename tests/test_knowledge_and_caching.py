from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from issueforge.config import settings
from issueforge.core.caching import REUSABLE_FAILURE_STAGES, ArtifactCacheManager
from issueforge.core.database import get_task_graph_edges, list_learnings, save_task
from issueforge.core.models import (
    PipelineRun,
    PlatformType,
    Task,
    TaskLearning,
    TaskStatus,
    TaskType,
    utc_now,
)
from issueforge.core.task_dossier import TaskDossierManager
from issueforge.vault.knowledge_vault import KnowledgeVault, backfill_notes, lesson_node_name


def _task(task_id: str = "test-kv-001", **over) -> Task:
    base = dict(
        id=task_id,
        title="Add Prometheus exporter",
        description="d",
        repo_url="https://gitlab.com/acme/telemetry",
        repo_name="acme/telemetry",
        working_branch=f"forge/{task_id}",
        platform=PlatformType.GITLAB,
        task_type=TaskType.ISSUE,
        issue_number=142,
    )
    base.update(over)
    return Task(**base)


def _learning(task_id: str = "test-kv-001", topic: str = "FastAPI SSE Heartbeat Pattern") -> TaskLearning:
    return TaskLearning(
        id="l-1",
        task_id=task_id,
        topic=topic,
        summary="SSE connections drop without a periodic heartbeat.",
        solution_pattern="Emit a comment frame every 15s to keep proxies from closing the stream.",
        tags=["sse", "fastapi"],
        created_at=utc_now(),
    )


# --------------------------------------------------------------------- knowledge vault

def test_lesson_node_name_is_safe_and_stable():
    assert lesson_node_name("FastAPI SSE Heartbeat") == "Lesson-FastAPI-SSE-Heartbeat"
    assert lesson_node_name("a/b:c|d") == "Lesson-a-b-c-d"
    assert lesson_node_name("") == "Lesson-untitled"
    # Deterministic across calls — the dossier links by this name.
    assert lesson_node_name("Same Topic") == lesson_node_name("Same Topic")


def test_write_lesson_produces_valid_frontmatter():
    task = _task()
    path = KnowledgeVault().write_lesson(_learning(), task=task)
    assert path is not None and path.exists()

    text = path.read_text()
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["title"] == "FastAPI SSE Heartbeat Pattern"
    assert meta["source_task"] == "test-kv-001"
    assert meta["repo"] == "acme/telemetry"
    # The issueforge/knowledge tag is what colours these orange in the Obsidian graph.
    assert "issueforge/knowledge" in meta["tags"]
    assert "topic/sse" in meta["tags"]


def test_lesson_links_the_same_nodes_as_its_task():
    """Lessons must share entity nodes with the tasks that produced them, or the graph
    shows them as orphans."""
    task = _task()
    text = KnowledgeVault().write_lesson(_learning(), task=task).read_text()
    assert "[[Repo-acme-telemetry]]" in text
    assert "[[Issue-gitlab-acme-telemetry-142]]" in text


def test_lesson_text_cannot_inject_graph_nodes():
    learning = _learning()
    learning.summary = "see [[evil-node]] here"
    text = KnowledgeVault().write_lesson(learning, task=_task()).read_text()
    assert "[[evil-node]]" not in text


def test_write_lesson_without_a_task_still_works():
    path = KnowledgeVault().write_lesson(_learning(), task=None)
    assert path is not None and path.exists()


def test_write_lesson_never_raises(monkeypatch):
    vault = KnowledgeVault()
    monkeypatch.setattr(vault, "root", Path("/proc/nonexistent/cannot-write"))
    assert vault.write_lesson(_learning(), task=_task()) is None


def test_notes_for_task_matches_on_source_task():
    vault = KnowledgeVault()
    vault.write_lesson(_learning("task-a", "Lesson Alpha"), task=_task("task-a"))
    vault.write_lesson(_learning("task-b", "Lesson Beta"), task=_task("task-b"))
    assert vault.notes_for_task("task-a") == ["Lesson-Lesson-Alpha"]
    assert vault.notes_for_task("task-b") == ["Lesson-Lesson-Beta"]
    assert vault.notes_for_task("task-c") == []


def test_notes_for_task_ignores_unparseable_notes():
    vault = KnowledgeVault()
    vault.root.mkdir(parents=True, exist_ok=True)
    (vault.root / "Lesson-Broken.md").write_text("no frontmatter here")
    (vault.root / "Lesson-BadYaml.md").write_text("---\n: : :\n---\nbody")
    assert vault.notes_for_task("anything") == []


async def test_backfill_writes_notes_for_existing_learnings():
    from issueforge.core.database import save_learning

    task = _task("task-backfill")
    await save_task(task)
    await save_learning(_learning("task-backfill", "Backfilled Lesson"))
    assert await backfill_notes() >= 1
    assert KnowledgeVault().notes_for_task("task-backfill") == ["Lesson-Backfilled-Lesson"]


async def test_dossier_links_lessons_into_the_graph():
    from issueforge.core.database import save_learning

    task = _task("task-linked")
    await save_task(task)
    await save_learning(_learning("task-linked", "Graph Linked Lesson"))
    await backfill_notes()

    await TaskDossierManager.sync_dossier_async(task)
    readme = TaskDossierManager.get_task_readme(task.id)
    assert "[[Lesson-Graph-Linked-Lesson]]" in readme
    assert "Lesson-Graph-Linked-Lesson" in await get_task_graph_edges(task.id)


async def test_recording_a_learning_writes_a_note():
    """The note is a projection of the existing extraction path, not a second one."""
    from types import SimpleNamespace

    from issueforge.agents.orchestrator import SupervisoryOrchestrator
    from issueforge.core.models import TestResult
    from issueforge.core.sandbox import NativeSandbox

    task = _task("task-record")
    await save_task(task)
    payload = (
        '{"topic": "Recorded Lesson", "summary": "s", '
        '"solution_pattern": "p", "tags": ["x"]}'
    )
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    orch = SupervisoryOrchestrator(sandbox=NativeSandbox(task.id, run_id="run-1"), task=task)
    # Learning extraction tries a local agy session first; report it unavailable so the
    # mocked API fallback is what answers (and no real binary is spawned).
    with patch("issueforge.agents.orchestrator.AgySessionRunner.run_prompt",
               new=AsyncMock(return_value=(False, ""))), \
         patch("issueforge.agents.orchestrator.call_llm_with_fallback", new=AsyncMock(return_value=resp)):
        await orch.record_task_learning(
            plan="p", diff_stat="1 file", test_result=TestResult(passed=True, test_runner="pytest")
        )

    assert len(await list_learnings(limit=10)) >= 1
    assert KnowledgeVault().notes_for_task(task.id) == ["Lesson-Recorded-Lesson"]


# --------------------------------------------------------------------- artifact caching

def _run(run_id: str, stage: str) -> PipelineRun:
    return PipelineRun(
        run_id=run_id, attempt_number=int(run_id.split("-")[1]),
        status=TaskStatus.FAILED, failure_stage=stage,
    )


def _write_plan(task_id: str, run_id: str, text: str) -> None:
    d = TaskDossierManager.get_task_dir(task_id) / "sandboxes" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "PLAN.md").write_text(text)


PLAN_TEXT = "# Plan\n\nThis plan is long enough to be considered real content by the cache.\n"


def test_plan_is_reused_after_a_late_failure():
    task = _task("cache-tester", runs=[_run("run-1", "TESTER")])
    _write_plan(task.id, "run-1", PLAN_TEXT)
    plan, source = ArtifactCacheManager.find_reusable_plan(task, "run-2")
    assert plan.startswith("# Plan")
    assert source == "run-1"


@pytest.mark.parametrize("stage", ["CODER", "CODING", "PLANNING"])
def test_plan_is_not_reused_when_planning_or_coding_failed(stage):
    """A coder that produced nothing may have had an unimplementable plan; re-plan instead."""
    task = _task(f"cache-{stage.lower()}", runs=[_run("run-1", stage)])
    _write_plan(task.id, "run-1", PLAN_TEXT)
    plan, source = ArtifactCacheManager.find_reusable_plan(task, "run-2")
    assert plan is None and source is None


def test_reusable_stage_set_excludes_coding():
    assert "TESTER" in REUSABLE_FAILURE_STAGES
    assert "CODER" not in REUSABLE_FAILURE_STAGES
    assert "CODING" not in REUSABLE_FAILURE_STAGES
    assert "PLANNING" not in REUSABLE_FAILURE_STAGES


def test_most_recent_reusable_run_wins():
    task = _task("cache-recent", runs=[_run("run-1", "TESTER"), _run("run-2", "TESTER")])
    _write_plan(task.id, "run-1", "# Older plan, long enough to count as real content.\n")
    _write_plan(task.id, "run-2", "# Newer plan, long enough to count as real content.\n")
    plan, source = ArtifactCacheManager.find_reusable_plan(task, "run-3")
    assert source == "run-2"
    assert "Newer" in plan


def test_no_previous_runs_means_no_cache():
    plan, source = ArtifactCacheManager.find_reusable_plan(_task("cache-fresh"), "run-1")
    assert plan is None and source is None


def test_trivially_short_plan_is_not_cached():
    task = _task("cache-short", runs=[_run("run-1", "TESTER")])
    _write_plan(task.id, "run-1", "tiny")
    plan, _source = ArtifactCacheManager.find_reusable_plan(task, "run-2")
    assert plan is None


def test_falls_back_to_the_plan_on_the_task_record():
    task = _task("cache-fallback", runs=[_run("run-1", "TESTER")], plan=PLAN_TEXT)
    plan, source = ArtifactCacheManager.find_reusable_plan(task, "run-2")
    assert plan == PLAN_TEXT.strip()
    assert source == "run-1"


def test_apply_plan_cache_writes_into_the_workspace(tmp_path):
    task = _task("cache-apply", runs=[_run("run-1", "TESTER")])
    _write_plan(task.id, "run-1", PLAN_TEXT)
    workspace = tmp_path / "sandboxes" / "run-2"
    applied = ArtifactCacheManager.apply_plan_cache(task, "run-2", workspace)
    assert applied is not None
    assert (workspace / "PLAN.md").read_text().startswith("# Plan")


def test_apply_plan_cache_is_a_noop_without_a_reusable_plan(tmp_path):
    task = _task("cache-none", runs=[_run("run-1", "CODER")])
    _write_plan(task.id, "run-1", PLAN_TEXT)
    workspace = tmp_path / "ws"
    assert ArtifactCacheManager.apply_plan_cache(task, "run-2", workspace) is None
    assert not (workspace / "PLAN.md").exists()


async def test_engine_skips_replanning_when_a_plan_is_cached():
    """The whole point of the cache: the Planner must not run again."""
    from issueforge.agents.engine import IssueforgeAgentEngine

    task = _task("cache-engine", runs=[_run("run-1", "TESTER")])
    task.target_branch_confirmed = True
    await save_task(task)
    _write_plan(task.id, "run-1", PLAN_TEXT)

    with patch("issueforge.agents.engine.GitRepoManager") as mock_git_cls:
        mock_git = mock_git_cls.return_value
        mock_git.clone_repository = AsyncMock(return_value=True)
        mock_git.create_working_branch = AsyncMock()
        mock_git.get_remote_branches = AsyncMock(return_value=["main"])
        mock_git.get_diff = AsyncMock(return_value=("", "", []))
        mock_git.get_changed_file_paths = AsyncMock(return_value=[])
        with patch("issueforge.agents.orchestrator.SupervisoryOrchestrator.triage_task",
                   new=AsyncMock(return_value=(False, None))):
            with patch("issueforge.agents.planner.PlannerAgent.execute", new_callable=AsyncMock) as mock_plan:
                with patch("issueforge.agents.coder.CoderAgent.execute", new=AsyncMock(return_value=[])):
                    res = await IssueforgeAgentEngine().execute_task_pipeline(task.id)

    mock_plan.assert_not_called()
    assert res.plan.startswith("# Plan")
