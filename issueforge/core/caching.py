import logging
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("issueforge.core.caching")

PLAN_FILENAME = "PLAN.md"

# Failure stages that imply the plan itself was sound — the run got past planning and
# broke later, so regenerating the plan would burn tokens to arrive at the same place.
#
# CODER/CODING are deliberately excluded. A coder that produced no changes may have been
# handed an unimplementable plan, and reusing it would loop on the same dead end. The
# previous failure context is injected into the prompt either way, so a fresh plan there
# is the safer default.
REUSABLE_FAILURE_STAGES = frozenset({
    "TESTER",
    "TESTING",
    "REVIEWER",
    "MERGE_CONFLICT",
    "GIT",
    "AWAITING_CONFIRMATION",
})


class ArtifactCacheManager:
    """Carries verified artifacts forward across retry attempts.

    Each retry clones a clean sandbox, which is what keeps a broken run from poisoning the
    next one. That also throws away work that was already correct — most expensively the
    plan. This restores just the plan, and only when the previous run proved it viable by
    failing after the planning stage.
    """

    @staticmethod
    def find_reusable_plan(task, current_run_id: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (plan_text, source_run_id) from the most recent run that got past planning.

        Reads the dossier-level copy the Planner writes, so it survives the sandbox wipe.
        """
        previous = [r for r in (task.runs or []) if r.run_id != current_run_id]
        for run in reversed(previous):
            stage = (run.failure_stage or "").upper()
            if stage not in REUSABLE_FAILURE_STAGES:
                continue
            plan = ArtifactCacheManager._read_plan(task, run.run_id)
            if plan:
                return plan, run.run_id
        return None, None

    @staticmethod
    def _read_plan(task, run_id: str) -> Optional[str]:
        from issueforge.core.task_dossier import TaskDossierManager

        task_dir = TaskDossierManager.get_task_dir(task.id)
        candidates: List[Path] = [
            task_dir / "sandboxes" / run_id / PLAN_FILENAME,
            task_dir / PLAN_FILENAME,
        ]
        for path in candidates:
            try:
                if path.is_file():
                    text = path.read_text(encoding="utf-8").strip()
                    if len(text) > 50:
                        return text
            except OSError:
                continue
        # Fall back to the plan persisted on the task record itself.
        plan = (getattr(task, "plan", None) or "").strip()
        return plan if len(plan) > 50 else None

    @staticmethod
    def apply_plan_cache(task, current_run_id: str, workspace_path: Path) -> Optional[str]:
        """Write a reusable plan into the fresh sandbox. Returns the plan text, or None.

        Must run *after* clone_repository: cloning wipes any leftover files in the
        workspace, so a plan written before it would be destroyed.
        """
        plan, source_run = ArtifactCacheManager.find_reusable_plan(task, current_run_id)
        if not plan:
            return None
        try:
            workspace_path.mkdir(parents=True, exist_ok=True)
            (workspace_path / PLAN_FILENAME).write_text(plan, encoding="utf-8")
            logger.info(
                "Reused plan from %s for %s in task %s", source_run, current_run_id, task.id
            )
            return plan
        except OSError as exc:
            logger.warning("Could not write cached plan for %s: %s", task.id, exc)
            return None

    @staticmethod
    def describe_source(task, current_run_id: str) -> Optional[str]:
        _plan, source_run = ArtifactCacheManager.find_reusable_plan(task, current_run_id)
        return source_run
