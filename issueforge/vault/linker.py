import hashlib
import logging
import re
from pathlib import Path
from typing import Dict, List, Set

logger = logging.getLogger("issueforge.vault.linker")

WIKILINK_PATTERN = re.compile(r"\[\[(.*?)\]\]")

# Characters that would change a wikilink's meaning or break the filename it resolves to.
# "/" is the important one: [[a/b]] is a vault *path* in Obsidian, not a node called "a/b".
_UNSAFE_NODE_CHARS = re.compile(r"[\\/|#^\[\]:]+")


def wikify(name: str) -> str:
    """Normalize an arbitrary string into a safe, stable Obsidian node name."""
    cleaned = _UNSAFE_NODE_CHARS.sub("-", str(name).strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip(" -._")


class VaultLinkIndexer:
    """Parses wikilinks out of Markdown dossiers.

    Kept pure and synchronous: the persistence half lives in `issueforge.core.database`, which
    lets the parsing be unit-tested without a database or an event loop.
    """

    @staticmethod
    def extract_links_from_text(text: str) -> Set[str]:
        links: Set[str] = set()
        for match in WIKILINK_PATTERN.findall(text or ""):
            # Handle aliases and block refs: [[Target|Display]] / [[Target#Heading]] -> Target
            clean = match.split("|")[0].split("#")[0].strip()
            if clean:
                links.add(clean)
        return links

    @staticmethod
    def qualify_local_links(links: Set[str], task_id: str, task_dir: Path) -> Set[str]:
        """Namespace links that resolve to a file inside the task's own folder.

        Obsidian resolves a bare [[PLAN]] or [[task_dag.canvas]] relative to the note's own
        folder, so every task's README legitimately links its own. Left unqualified they
        would collapse into one shared SQLite node and make every task look connected to
        every other. The README keeps the relative form; only the index is qualified.
        """
        qualified: Set[str] = set()
        for link in links:
            if (task_dir / link).exists() or (task_dir / f"{link}.md").exists():
                qualified.add(f"{task_id}/{link}")
            else:
                qualified.add(link)
        return qualified

    @classmethod
    def extract_links(cls, file_path: Path) -> Set[str]:
        path = Path(file_path)
        if not path.exists():
            return set()
        try:
            return cls.extract_links_from_text(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Could not read %s for wikilinks: %s", path, exc)
            return set()


# task_id -> fingerprint of the last indexed link set. The engine calls sync_dossier at
# roughly sixteen points per run; this collapses that to a DB write only when links change.
_LAST_FINGERPRINT: Dict[str, str] = {}


def _fingerprint(links: List[str]) -> str:
    return hashlib.sha1("\n".join(sorted(links)).encode("utf-8")).hexdigest()


def reset_fingerprint_cache() -> None:
    """Clear the debounce cache — used by tests and after a task is deleted."""
    _LAST_FINGERPRINT.clear()


async def sync_task_graph(task, force: bool = False) -> List[str]:
    """Re-index a task dossier's wikilinks into the SQLite graph edge table.

    Deliberately not wired into `TaskDossierManager.sync_dossier`, which the engine calls
    at roughly sixteen points per run; this is awaited only at meaningful state changes.
    Never raises — graph indexing is telemetry and must not fail a pipeline.
    """
    from issueforge.core.database import replace_task_graph_edges
    from issueforge.core.task_dossier import TaskDossierManager

    try:
        task_dir = TaskDossierManager.get_task_dir(task.id)
        readme = task_dir / "README.md"
        links = sorted(
            VaultLinkIndexer.qualify_local_links(
                VaultLinkIndexer.extract_links(readme), task.id, task_dir
            )
        )
        fingerprint = _fingerprint(links)
        if not force and _LAST_FINGERPRINT.get(task.id) == fingerprint:
            return links
        await replace_task_graph_edges(task.id, links)
        _LAST_FINGERPRINT[task.id] = fingerprint
        logger.debug("Synchronized %d graph edges for task %s", len(links), task.id)
        return links
    except Exception as exc:
        logger.warning("Graph edge sync failed for task %s: %s", getattr(task, "id", "?"), exc)
        return []
