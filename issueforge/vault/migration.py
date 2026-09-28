import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

from issueforge.config import settings

logger = logging.getLogger("issueforge.vault.migration")

MIGRATED_SUFFIX = ".migrated"
Action = Literal["move", "skip-collision", "skip-orphan", "already-migrated"]


def legacy_tasks_root() -> Path:
    """Where dossiers landed before the vault root existed: CWD-relative `.issueforge/tasks`."""
    return (Path.cwd() / ".issueforge" / "tasks").resolve()


@dataclass
class DossierPlan:
    src: Path
    dst: Path
    action: Action
    reason: str
    size_bytes: int = 0
    in_db: bool = False


@dataclass
class MigrationReport:
    planned: List[DossierPlan] = field(default_factory=list)
    legacy_db: Optional[Path] = None
    legacy_db_bytes: int = 0
    applied: bool = False

    @property
    def movable(self) -> List[DossierPlan]:
        return [p for p in self.planned if p.action == "move"]


def _dir_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except Exception:
        return 0


def detect_legacy_dossiers() -> List[Path]:
    """Cheap, synchronous check for the startup warning.

    Ignores tombstones and `test-*` fixture leftovers: plan_migration classifies those as
    orphans and skips them, so warning about them would be permanent and unactionable.
    """
    source = legacy_tasks_root()
    if not source.exists() or source == settings.forge_tasks_root:
        return []
    return [
        d for d in source.iterdir()
        if d.is_dir()
        and not d.name.startswith((".", "test-", "test_"))
        and not d.name.endswith(MIGRATED_SUFFIX)
    ]


async def plan_migration(
    source: Optional[Path] = None,
    dest: Optional[Path] = None,
    include_orphans: bool = False,
) -> MigrationReport:
    """Classify every legacy dossier without touching the filesystem."""
    from issueforge.core.database import list_tasks

    source = (source or legacy_tasks_root()).resolve()
    dest = (dest or settings.forge_tasks_root).resolve()
    report = MigrationReport()

    legacy_db = source.parent / "issueforge.db"
    if legacy_db.exists() and legacy_db.resolve() != settings.forge_db_path.resolve():
        report.legacy_db = legacy_db
        report.legacy_db_bytes = legacy_db.stat().st_size

    if not source.exists() or source == dest:
        return report

    try:
        known_ids = {t.id for t in await list_tasks(limit=10000)}
    except Exception as exc:
        logger.warning("Could not read task ids from the database: %s", exc)
        known_ids = set()

    for src in sorted(p for p in source.iterdir() if p.is_dir()):
        if src.name.startswith(".") or src.name.endswith(MIGRATED_SUFFIX):
            continue
        dst = dest / src.name
        in_db = src.name in known_ids
        size = _dir_size(src)

        if dst.exists() and any(dst.iterdir()):
            action, reason = "skip-collision", "destination already exists and is not empty"
        elif not in_db and src.name.startswith(("test-", "test_")) and not include_orphans:
            action, reason = "skip-orphan", "test fixture leftover (not in database)"
        elif not in_db:
            action, reason = "move", "not in database — verify before deleting the source"
        else:
            action, reason = "move", "active task dossier"

        report.planned.append(
            DossierPlan(src=src, dst=dst, action=action, reason=reason, size_bytes=size, in_db=in_db)
        )
    return report


def apply_migration(report: MigrationReport) -> MigrationReport:
    """Copy each movable dossier, verify it, then tombstone the source.

    Copy-then-rename rather than `shutil.move` so a failure part-way across a filesystem
    boundary leaves the original intact. Nothing is ever deleted: sources become
    `<name>.migrated`, which also makes re-runs no-ops.
    """
    for plan in report.planned:
        if plan.action != "move":
            continue
        try:
            shutil.copytree(plan.src, plan.dst, symlinks=True, dirs_exist_ok=False)
            if _dir_size(plan.dst) != plan.size_bytes:
                plan.action = "skip-collision"
                plan.reason = "size mismatch after copy — source left untouched"
                continue
            plan.src.rename(plan.src.parent / f"{plan.src.name}{MIGRATED_SUFFIX}")
            logger.info("Migrated dossier %s -> %s", plan.src.name, plan.dst)
        except Exception as exc:
            plan.action = "skip-collision"
            plan.reason = f"copy failed: {exc}"
            logger.warning("Failed to migrate %s: %s", plan.src, exc)
    report.applied = True
    return report
