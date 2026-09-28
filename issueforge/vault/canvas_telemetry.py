import logging
from typing import Optional, Tuple

from issueforge.config import settings
from issueforge.core.models import TaskStatus
from issueforge.vault.canvas_builder import CanvasColor, CanvasDAGBuilder, CanvasStage

logger = logging.getLogger("issueforge.vault.canvas_telemetry")

# Stage titles as rendered on the canvas.
_STAGE_TITLES = {
    CanvasStage.ISSUE: "📥 Ingested Task",
    CanvasStage.PLANNER: "🧠 Planner Agent",
    CanvasStage.GATE: "🚦 Branch Gate",
    CanvasStage.CODER: "💻 Coder Agent (`agy`)",
    CanvasStage.TESTER: "🧪 Tester Agent",
    CanvasStage.MERGE: "🔀 AST Merge Guard",
    CanvasStage.REVIEWER: "🔍 Reviewer Agent",
}

# state -> colour. Anything unlisted renders uncoloured.
_STATE_COLORS = {
    "INGESTED": CanvasColor.PURPLE,
    "DISCOVERING": CanvasColor.CYAN,
    "AWAITING_INPUT": CanvasColor.CYAN,
    "AWAITING_CONFIRMATION": CanvasColor.GREEN,
    "RESOLVED": CanvasColor.GREEN,
    "RUNNING": CanvasColor.YELLOW,
    "PUSHING": CanvasColor.YELLOW,
    "REPAIR": CanvasColor.ORANGE,
    "CANCELLED": CanvasColor.ORANGE,
    "DONE": CanvasColor.GREEN,
    "PASSED": CanvasColor.GREEN,
    "MERGED": CanvasColor.GREEN,
    "FAILED": CanvasColor.RED,
    "ERROR": CanvasColor.RED,
}

# Which canvas node represents a given task status, for the generic failure handlers.
_STATUS_STAGES = {
    TaskStatus.PENDING_APPROVAL: CanvasStage.ISSUE,
    TaskStatus.APPROVED: CanvasStage.ISSUE,
    TaskStatus.AWAITING_BRANCH_SELECTION: CanvasStage.GATE,
    TaskStatus.AWAITING_INPUT: CanvasStage.GATE,
    TaskStatus.PLANNING: CanvasStage.PLANNER,
    TaskStatus.CODING: CanvasStage.CODER,
    TaskStatus.TESTING: CanvasStage.TESTER,
    TaskStatus.REVISION_REQUESTED: CanvasStage.CODER,
    TaskStatus.AWAITING_CONFIRMATION: CanvasStage.REVIEWER,
    TaskStatus.CONFIRMED: CanvasStage.REVIEWER,
    TaskStatus.PUSHING: CanvasStage.REVIEWER,
    TaskStatus.COMPLETED: CanvasStage.REVIEWER,
}


def stage_for_status(status: TaskStatus) -> CanvasStage:
    return _STATUS_STAGES.get(status, CanvasStage.ISSUE)


def render(stage: CanvasStage, state: str, detail: str = "") -> Tuple[str, Optional[str]]:
    """Pure: map a stage/state/detail into canvas node markdown and a colour code."""
    title = _STAGE_TITLES.get(stage, stage.value)
    text = f"### {title}\n\n*Status: {state}*"
    if detail:
        clean = " ".join(str(detail).split())
        if len(clean) > 180:
            clean = clean[:177] + "..."
        text += f"\n\n{clean}"
    color = _STATE_COLORS.get(state)
    return text, (color.value if color else None)


async def mark(task, stage: CanvasStage, state: str, detail: str = "") -> None:
    """Update one canvas stage node for a task.

    Swallows every error by design: canvas telemetry is cosmetic and must never be able to
    fail a pipeline run.
    """
    if not settings.forge_canvas_enabled:
        return
    try:
        from issueforge.core.task_dossier import TaskDossierManager
        from issueforge.vault.canvas_builder import CANVAS_FILENAME

        builder = CanvasDAGBuilder(
            TaskDossierManager.get_task_dir(task.id) / CANVAS_FILENAME, task.id
        )
        builder.ensure_initialized(task.title)
        text, color = render(stage, state, detail)
        builder.update_stage_node(stage, text, color)
    except Exception as exc:
        logger.debug("Canvas telemetry skipped for %s/%s: %s", getattr(task, "id", "?"), stage, exc)
