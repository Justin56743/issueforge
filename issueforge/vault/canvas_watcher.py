import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from issueforge.config import settings
from issueforge.vault.canvas_builder import (
    CANVAS_FILENAME,
    CanvasDAGBuilder,
    CanvasStage,
    clean_directive_text,
    is_directive_text,
)

logger = logging.getLogger("issueforge.vault.watcher")


async def apply_canvas_directive(
    task_id: str, directive_text: str, run_id: Optional[str] = None
) -> bool:
    """Queue an operator directive dropped on the canvas onto the task.

    NOTE: this is deliberately a *queue*, not a live injection. `AgySessionRunner` starts
    `agy` with the whole prompt already materialised in a single `--print=` argv element,
    over plain pipes with no stdin and no PTY, so nothing can reach a running agent. The
    directive therefore lands in `task.custom_instructions` — the same channel operator
    answers already use (`TaskQueue.submit_question_answer`) — and takes effect at the next
    planner/coder/tester invocation.
    """
    from issueforge.core.database import get_task, save_task
    from issueforge.core.events import event_bus
    from issueforge.core.models import AgentRole, EventType, utc_now

    directive_text = (directive_text or "").strip()
    if not directive_text:
        return False

    task = await get_task(task_id)
    if not task:
        return False

    stamp = utc_now().strftime("%Y-%m-%d %H:%M:%SZ")
    task.custom_instructions = (
        f"{task.custom_instructions or ''}\n\n"
        f"[Operator Canvas Directive @ {stamp}]: {directive_text}"
    ).strip()
    await save_task(task)

    await event_bus.emit_log(
        task_id=task_id,
        run_id=run_id or task.active_run_id,
        role=AgentRole.ORCHESTRATOR.value,
        event_type=EventType.STEP,
        message=(
            f"🧑‍💻 Canvas directive queued (applies at the next agent turn): "
            f"{directive_text[:160]}"
        ),
        data={"kind": "canvas_directive", "directive": directive_text},
    )
    return True


class CanvasSteeringWatcher:
    """Watches a task's `task_dag.canvas` for operator directive cards.

    The detection logic lives in the pure `extract_new_directives` and the awaitable
    `poll_once`; `run_watch_loop` is a thin adapter over `watchfiles.awatch`. That split
    keeps the behaviour unit-testable without depending on inotify timing.
    """

    def __init__(
        self,
        canvas_path: Path,
        task_id: str,
        on_directive: Callable[[str], Awaitable[None]],
        debounce_seconds: float = 0.25,
    ):
        self.canvas_path = Path(canvas_path)
        self.task_id = task_id
        self.on_directive = on_directive
        self.debounce_seconds = debounce_seconds
        self.seen_nodes: Set[str] = set()
        self._snapshot_seen_nodes()

    def _snapshot_seen_nodes(self) -> None:
        """Treat everything already on the canvas as known, so startup fires nothing."""
        payload = self._read()
        if payload:
            self.seen_nodes = {
                n.get("id") for n in payload.get("nodes", []) if n.get("id")
            }

    def _read(self) -> Optional[dict]:
        if not self.canvas_path.exists():
            return None
        try:
            return json.loads(self.canvas_path.read_text(encoding="utf-8"))
        except Exception as exc:
            # Expected transiently if a writer is mid-save; the next event re-reads.
            logger.debug("Canvas %s unreadable: %s", self.canvas_path, exc)
            return None

    def extract_new_directives(self, payload: dict) -> List[Tuple[str, str]]:
        """Pure: return (node_id, clean_text) for unseen directive nodes, marking them seen."""
        found: List[Tuple[str, str]] = []
        for node in (payload or {}).get("nodes", []):
            node_id = node.get("id")
            if not node_id or node_id in self.seen_nodes:
                continue
            self.seen_nodes.add(node_id)
            text = node.get("text", "")
            if is_directive_text(text):
                cleaned = clean_directive_text(text)
                if cleaned:
                    found.append((node_id, cleaned))
        return found

    async def poll_once(self) -> List[str]:
        """Read the canvas, dispatch any new directives, and acknowledge them. Never raises."""
        dispatched: List[str] = []
        payload = self._read()
        if payload is None:
            return dispatched

        for node_id, directive in self.extract_new_directives(payload):
            logger.info("Canvas directive for %s: %s", self.task_id, directive)
            try:
                await self.on_directive(directive)
                dispatched.append(directive)
                builder = CanvasDAGBuilder(self.canvas_path, self.task_id)
                builder.mark_directive_applied(
                    node_id, "Queued by Issueforge — applies at the next agent turn."
                )
                # mark_directive_applied rewrites the node in place, so its id stays seen.
            except Exception as exc:
                logger.warning("Failed to apply canvas directive for %s: %s", self.task_id, exc)
        return dispatched

    async def run_watch_loop(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """Watch the dossier directory for canvas edits.

        Watches the *directory*, non-recursively, rather than the file:
        `awatch()` on a not-yet-existing path raises FileNotFoundError immediately, and
        editors (Obsidian included) save by replace, which can drop a file-level watch.
        Non-recursive matters too — the dossier contains `sandboxes/run-N/`, a full git
        clone the agents churn constantly, and watching it recursively would wake this loop
        on every file they touch.
        """
        from watchfiles import awatch

        watch_dir = self.canvas_path.parent
        watch_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Canvas steering watcher started on %s", self.canvas_path)
        try:
            async for _ in awatch(
                watch_dir,
                recursive=False,
                stop_event=stop_event,
                watch_filter=lambda _change, path: Path(path).name == CANVAS_FILENAME,
            ):
                try:
                    if self.debounce_seconds:
                        await asyncio.sleep(self.debounce_seconds)
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Canvas watch iteration failed for %s: %s", self.task_id, exc)
        except asyncio.CancelledError:
            logger.debug("Canvas steering watcher cancelled for %s", self.task_id)
            raise
        except Exception as exc:
            logger.warning("Canvas steering watcher stopped for %s: %s", self.task_id, exc)


class CanvasWatcherRegistry:
    """Owns one steering watcher task per active task id."""

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}

    async def start(self, task_id: str, run_id: Optional[str] = None) -> Optional[asyncio.Task]:
        if not settings.forge_canvas_watch_enabled:
            return None
        await self.stop(task_id)

        from issueforge.core.task_dossier import TaskDossierManager

        canvas_path = TaskDossierManager.get_task_dir(task_id) / CANVAS_FILENAME

        async def _on_directive(text: str) -> None:
            await apply_canvas_directive(task_id, text, run_id=run_id)

        try:
            watcher = CanvasSteeringWatcher(canvas_path, task_id, _on_directive)
            handle = asyncio.create_task(watcher.run_watch_loop())
            self._tasks[task_id] = handle
            return handle
        except Exception as exc:
            logger.warning("Could not start canvas watcher for %s: %s", task_id, exc)
            return None

    async def stop(self, task_id: str) -> None:
        handle = self._tasks.pop(task_id, None)
        if handle is None:
            return
        handle.cancel()
        # Awaiting the cancellation is what avoids "Task was destroyed but it is pending".
        await asyncio.gather(handle, return_exceptions=True)

    async def stop_all(self) -> None:
        for task_id in list(self._tasks):
            await self.stop(task_id)


canvas_watchers = CanvasWatcherRegistry()
