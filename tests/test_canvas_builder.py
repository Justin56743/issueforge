import asyncio
import json

import pytest

from issueforge.core.database import get_task, get_task_events, save_task
from issueforge.core.models import PlatformType, Task, TaskStatus, TaskType
from issueforge.core.task_dossier import TaskDossierManager
from issueforge.vault.canvas_builder import (
    CANVAS_FILENAME,
    STAGE_ORDER,
    CanvasColor,
    CanvasDAGBuilder,
    CanvasStage,
    clean_directive_text,
    is_directive_text,
)
from issueforge.vault.canvas_telemetry import mark, render, stage_for_status
from issueforge.vault.canvas_watcher import CanvasSteeringWatcher, apply_canvas_directive


@pytest.fixture
async def async_client_for_task():
    """A task with a synced dossier (and therefore a canvas), plus an ASGI client."""
    from httpx import ASGITransport, AsyncClient

    from issueforge.main import app

    task = _make_task("test-canvas-api")
    await save_task(task)
    TaskDossierManager.sync_dossier(task)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, task


@pytest.fixture
def builder(tmp_path):
    b = CanvasDAGBuilder(tmp_path / CANVAS_FILENAME, "issue-42")
    b.initialize_task_dag("Add dark mode")
    return b


def _make_task(task_id: str = "test-canvas-001") -> Task:
    return Task(
        id=task_id,
        title="Add dark mode",
        description="Toggle a theme.",
        repo_url="https://gitlab.com/acme/ui",
        repo_name="acme/ui",
        working_branch=f"forge/{task_id}",
        platform=PlatformType.GITLAB,
        task_type=TaskType.ISSUE,
    )


# --------------------------------------------------------------------- structure

def test_dag_initialization(builder):
    assert len(builder.data["nodes"]) == 7
    assert len(builder.data["edges"]) == 6
    assert all(n["type"] == "text" for n in builder.data["nodes"])
    ids = [n["id"] for n in builder.data["nodes"]]
    assert len(set(ids)) == 7
    assert all(len(i) == 16 for i in ids)
    assert [n["x"] for n in builder.data["nodes"]] == [-450, -100, 250, 600, 950, 1300, 1650]
    assert all(n["width"] == 280 and n["height"] == 160 for n in builder.data["nodes"])


def test_initial_stage_colors(builder):
    assert builder.get_stage_node(CanvasStage.ISSUE)["color"] == CanvasColor.PURPLE.value
    assert builder.get_stage_node(CanvasStage.GATE)["color"] == CanvasColor.CYAN.value
    assert "color" not in builder.get_stage_node(CanvasStage.CODER)


def test_edges_wire_left_to_right(builder):
    for edge in builder.data["edges"]:
        assert edge["fromSide"] == "right"
        assert edge["toSide"] == "left"
    froms = {e["fromNode"] for e in builder.data["edges"]}
    assert builder.stage_node_id(CanvasStage.REVIEWER) not in froms


def test_stage_node_ids_are_deterministic(tmp_path, builder):
    other = CanvasDAGBuilder(tmp_path / "other.canvas", "issue-42")
    assert other.stage_node_id(CanvasStage.TESTER) == builder.stage_node_id(CanvasStage.TESTER)
    different = CanvasDAGBuilder(tmp_path / "third.canvas", "issue-43")
    assert different.stage_node_id(CanvasStage.TESTER) != builder.stage_node_id(CanvasStage.TESTER)


def test_update_stage_node_accepts_int_index(builder):
    assert builder.stage_node_id(3) == builder.stage_node_id(CanvasStage.CODER)
    assert builder.update_stage_node(3, "### Coder\n\n*RUNNING*", CanvasColor.YELLOW.value)
    assert builder.get_stage_node(CanvasStage.CODER)["color"] == CanvasColor.YELLOW.value


# --------------------------------------------------------------------- mutation

def test_stage_color_mutation_sequence(tmp_path, builder):
    path = builder.canvas_path
    for color in (CanvasColor.CYAN, CanvasColor.YELLOW, CanvasColor.GREEN, CanvasColor.RED):
        builder.update_stage_node(CanvasStage.TESTER, f"### Tester {color.name}", color.value)
        # Re-read through a fresh builder to prove it persisted.
        fresh = CanvasDAGBuilder(path, "issue-42")
        assert fresh.get_stage_node(CanvasStage.TESTER)["color"] == color.value


def test_update_stage_node_after_directive_injection(builder):
    """Regression test for the spec's positional indexing.

    new-context.md addresses nodes by list position, so the first appended directive card
    makes every later stage update write to the wrong node.
    """
    directive_id = builder.inject_steering_directive("only touch auth endpoints")
    assert len(builder.data["nodes"]) == 8

    builder.update_stage_node(CanvasStage.CODER, "### Coder\n\n*RUNNING*", CanvasColor.YELLOW.value)

    fresh = CanvasDAGBuilder(builder.canvas_path, "issue-42")
    assert "*RUNNING*" in fresh.get_stage_node(CanvasStage.CODER)["text"]
    directive = next(n for n in fresh.data["nodes"] if n["id"] == directive_id)
    assert "only touch auth endpoints" in directive["text"]
    assert directive["color"] == CanvasColor.ORANGE.value


def test_update_missing_stage_returns_false(tmp_path):
    empty = CanvasDAGBuilder(tmp_path / "empty.canvas", "issue-99")
    assert empty.update_stage_node(CanvasStage.CODER, "text", "3") is False


def test_operator_added_nodes_are_preserved(builder):
    builder.data["nodes"].append({"id": "operator1", "type": "text", "text": "my note"})
    builder.save()
    builder.update_stage_node(CanvasStage.PLANNER, "### Planner\n\n*DONE*", CanvasColor.GREEN.value)
    fresh = CanvasDAGBuilder(builder.canvas_path, "issue-42")
    assert any(n["id"] == "operator1" for n in fresh.data["nodes"])


def test_steering_directive_injection_geometry(builder):
    target = dict(builder.get_stage_node(CanvasStage.CODER))
    directive_id = builder.inject_steering_directive("focus on tests")
    assert len(builder.data["edges"]) == 7
    node = next(n for n in builder.data["nodes"] if n["id"] == directive_id)
    assert node["x"] == target["x"]
    assert node["y"] == target["y"] - 220
    edge = next(e for e in builder.data["edges"] if e["fromNode"] == directive_id)
    assert edge["fromSide"] == "bottom" and edge["toSide"] == "top"
    assert edge["toNode"] == target["id"]


def test_ensure_initialized_is_idempotent(builder):
    builder.ensure_initialized("Add dark mode")
    builder.ensure_initialized("Add dark mode")
    assert len(builder.data["nodes"]) == 7
    assert len(builder.data["edges"]) == 6


def test_ensure_initialized_preserves_directives(builder):
    directive_id = builder.inject_steering_directive("keep me")
    # Simulate a partially-destroyed canvas: drop a stage node.
    builder.data["nodes"] = [
        n for n in builder.data["nodes"] if n["id"] != builder.stage_node_id(CanvasStage.TESTER)
    ]
    builder.save()
    fresh = CanvasDAGBuilder(builder.canvas_path, "issue-42")
    fresh.ensure_initialized("Add dark mode")
    assert fresh.is_initialized()
    assert any(n["id"] == directive_id for n in fresh.data["nodes"])


def test_save_is_atomic_and_leaves_no_temp_file(builder):
    builder.update_stage_node(CanvasStage.CODER, "### Coder", "3")
    residue = [p.name for p in builder.canvas_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert residue == []
    json.loads(builder.canvas_path.read_text())


def test_corrupt_canvas_self_heals(tmp_path):
    path = tmp_path / CANVAS_FILENAME
    path.write_text("this is not json")
    healed = CanvasDAGBuilder(path, "issue-42")
    healed.ensure_initialized("Add dark mode")
    assert len(healed.data["nodes"]) == 7


def test_mark_directive_applied_relabels_and_greens(builder):
    directive_id = builder.inject_steering_directive("only auth")
    assert builder.mark_directive_applied(directive_id, "queued")
    node = next(n for n in builder.data["nodes"] if n["id"] == directive_id)
    assert node["color"] == CanvasColor.GREEN.value
    # Relabelled text must no longer look like a directive, or the watcher would re-fire.
    assert not is_directive_text(node["text"])
    assert "only auth" in node["text"]


# --------------------------------------------------------------------- directive parsing

def test_is_directive_text_forms():
    assert is_directive_text("### 🧑‍💻 Operator Steering Directive\n\ndo x")
    assert is_directive_text("STEER: do x")
    assert not is_directive_text("### 🧪 Tester Agent\n\n*Status: RUNNING*")
    assert not is_directive_text("")


def test_clean_directive_text_strips_markers():
    assert clean_directive_text("STEER:  focus on auth ") == "focus on auth"
    assert clean_directive_text("### 🧑‍💻 Operator Steering Directive\n\nfocus") == "focus"


def test_extract_new_directives_is_pure_and_dedupes(tmp_path):
    path = tmp_path / CANVAS_FILENAME
    path.write_text(json.dumps({"nodes": [], "edges": []}))
    watcher = CanvasSteeringWatcher(path, "issue-42", lambda _t: asyncio.sleep(0), debounce_seconds=0)
    payload = {
        "nodes": [
            {"id": "a", "text": "### 🧑‍💻 Operator Steering Directive\n\ndo A"},
            {"id": "b", "text": "STEER: do B"},
            {"id": "c", "text": "### 🧪 Tester Agent\n\n*Status: RUNNING*"},
        ]
    }
    found = watcher.extract_new_directives(payload)
    assert [text for _id, text in found] == ["do A", "do B"]
    assert watcher.extract_new_directives(payload) == []


def test_watcher_ignores_preexisting_nodes(tmp_path):
    path = tmp_path / CANVAS_FILENAME
    path.write_text(json.dumps({"nodes": [{"id": "old", "text": "STEER: already there"}], "edges": []}))
    watcher = CanvasSteeringWatcher(path, "issue-42", lambda _t: asyncio.sleep(0), debounce_seconds=0)
    assert watcher.extract_new_directives(json.loads(path.read_text())) == []


async def test_poll_once_dispatches_and_acknowledges(tmp_path):
    path = tmp_path / CANVAS_FILENAME
    builder = CanvasDAGBuilder(path, "issue-42")
    builder.initialize_task_dag("Add dark mode")

    seen = []

    async def on_directive(text):
        seen.append(text)

    watcher = CanvasSteeringWatcher(path, "issue-42", on_directive, debounce_seconds=0)
    directive_id = CanvasDAGBuilder(path, "issue-42").inject_steering_directive("only auth")

    assert await watcher.poll_once() == ["only auth"]
    assert seen == ["only auth"]
    node = next(n for n in CanvasDAGBuilder(path, "issue-42").data["nodes"] if n["id"] == directive_id)
    assert node["color"] == CanvasColor.GREEN.value
    # Second poll must not re-dispatch.
    assert await watcher.poll_once() == []
    assert seen == ["only auth"]


async def test_poll_once_ignores_corrupt_canvas(tmp_path):
    path = tmp_path / CANVAS_FILENAME
    path.write_text("not json")
    watcher = CanvasSteeringWatcher(path, "issue-42", lambda _t: asyncio.sleep(0), debounce_seconds=0)
    assert await watcher.poll_once() == []


async def test_watch_loop_cancels_cleanly(tmp_path):
    path = tmp_path / CANVAS_FILENAME
    CanvasDAGBuilder(path, "issue-42").initialize_task_dag("Add dark mode")
    watcher = CanvasSteeringWatcher(path, "issue-42", lambda _t: asyncio.sleep(0), debounce_seconds=0)
    handle = asyncio.create_task(watcher.run_watch_loop())
    await asyncio.sleep(0.05)
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle


# --------------------------------------------------------------------- steering semantics

async def test_canvas_directive_queues_onto_custom_instructions():
    task = _make_task("test-directive-001")
    await save_task(task)
    assert await apply_canvas_directive(task.id, "only touch the auth endpoints")

    reloaded = await get_task(task.id)
    assert "[Operator Canvas Directive" in reloaded.custom_instructions
    assert "only touch the auth endpoints" in reloaded.custom_instructions

    events = await get_task_events(task.id)
    assert any((e.data or {}).get("kind") == "canvas_directive" for e in events)


async def test_canvas_directive_rejects_empty_and_unknown_task():
    task = _make_task("test-directive-002")
    await save_task(task)
    assert await apply_canvas_directive(task.id, "   ") is False
    assert await apply_canvas_directive("no-such-task", "do a thing") is False


# --------------------------------------------------------------------- telemetry

def test_render_is_pure_and_maps_colors():
    text, color = render(CanvasStage.TESTER, "REPAIR", "attempt 2/3")
    assert "Tester" in text and "REPAIR" in text and "attempt 2/3" in text
    assert color == CanvasColor.ORANGE.value
    assert render(CanvasStage.MERGE, "IDLE")[1] is None
    assert render(CanvasStage.CODER, "FAILED")[1] == CanvasColor.RED.value


def test_render_truncates_long_detail():
    _text, _color = render(CanvasStage.CODER, "DONE", "x" * 500)
    assert len(_text) < 300


def test_stage_for_status_covers_pipeline():
    assert stage_for_status(TaskStatus.PLANNING) is CanvasStage.PLANNER
    assert stage_for_status(TaskStatus.CODING) is CanvasStage.CODER
    assert stage_for_status(TaskStatus.TESTING) is CanvasStage.TESTER
    assert stage_for_status(TaskStatus.PUSHING) is CanvasStage.REVIEWER
    assert stage_for_status(TaskStatus.FAILED) is CanvasStage.ISSUE


async def test_mark_updates_canvas_via_dossier():
    task = _make_task("test-mark-001")
    TaskDossierManager.sync_dossier(task)
    await mark(task, CanvasStage.CODER, "RUNNING", "writing files")
    builder = CanvasDAGBuilder.for_task(task.id)
    node = builder.get_stage_node(CanvasStage.CODER)
    assert "RUNNING" in node["text"]
    assert node["color"] == CanvasColor.YELLOW.value


async def test_mark_never_raises(monkeypatch):
    """Canvas telemetry is cosmetic and must never be able to fail a pipeline run."""
    task = _make_task("test-mark-002")

    def boom(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr(CanvasDAGBuilder, "ensure_initialized", boom)
    await mark(task, CanvasStage.CODER, "RUNNING")


async def test_mark_is_noop_when_canvas_disabled(monkeypatch):
    from issueforge.config import settings as live_settings

    task = _make_task("test-mark-003")
    TaskDossierManager.sync_dossier(task)
    monkeypatch.setattr(live_settings, "forge_canvas_enabled", False)
    await mark(task, CanvasStage.CODER, "RUNNING", "should not appear")
    node = CanvasDAGBuilder.for_task(task.id).get_stage_node(CanvasStage.CODER)
    assert "should not appear" not in node["text"]


# --------------------------------------------------------------------- dashboard rendering

def test_inject_acknowledged_directive_is_not_re_detected(builder):
    """A directive queued from the dashboard must not be re-queued by the file watcher."""
    node_id = builder.inject_steering_directive("only auth", acknowledged=True)
    node = next(n for n in builder.data["nodes"] if n["id"] == node_id)
    assert node["color"] == CanvasColor.GREEN.value
    assert not is_directive_text(node["text"])
    assert "only auth" in node["text"]

    watcher = CanvasSteeringWatcher(builder.canvas_path, "issue-42", lambda _t: asyncio.sleep(0))
    watcher.seen_nodes = set()  # simulate a watcher that has never seen this file
    assert watcher.extract_new_directives(builder.data) == []


async def test_canvas_api_returns_the_dag(async_client_for_task):
    client, task = async_client_for_task
    res = await client.get(f"/api/tasks/{task.id}/canvas")
    assert res.status_code == 200
    payload = res.json()
    assert len(payload["nodes"]) == 7
    assert len(payload["edges"]) == 6


async def test_canvas_api_404s_for_unknown_task(async_client_for_task):
    client, _task = async_client_for_task
    assert (await client.get("/api/tasks/no-such-task/canvas")).status_code == 404


async def test_dashboard_directive_queues_and_acknowledges(async_client_for_task):
    client, task = async_client_for_task
    res = await client.post(
        f"/api/tasks/{task.id}/canvas/directive",
        json={"text": "only touch the auth endpoints"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "queued"

    reloaded = await get_task(task.id)
    assert "only touch the auth endpoints" in reloaded.custom_instructions

    # The card is written pre-acknowledged so the watcher cannot double-queue it.
    builder = CanvasDAGBuilder.for_task(task.id)
    directive_nodes = [n for n in builder.data["nodes"] if "only touch the auth" in n.get("text", "")]
    assert len(directive_nodes) == 1
    assert not is_directive_text(directive_nodes[0]["text"])


async def test_dashboard_directive_rejects_empty(async_client_for_task):
    client, task = async_client_for_task
    res = await client.post(f"/api/tasks/{task.id}/canvas/directive", json={"text": "   "})
    assert res.status_code == 400


async def test_dashboard_directive_404s_for_unknown_task(async_client_for_task):
    client, _task = async_client_for_task
    res = await client.post("/api/tasks/nope/canvas/directive", json={"text": "do a thing"})
    assert res.status_code == 404


async def test_task_detail_page_embeds_the_canvas_renderer(async_client_for_task):
    client, task = async_client_for_task
    html = (await client.get(f"/tasks/{task.id}")).text
    assert "taskCanvasViewport" in html
    assert "renderTaskCanvas" in html
    assert "canvasDirectiveInput" in html
