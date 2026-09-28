import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from issueforge.config import settings
from issueforge.vault.linker import wikify

logger = logging.getLogger("issueforge.vault.knowledge")

LESSON_PREFIX = "Lesson"


def lesson_node_name(topic: str) -> str:
    """Stable, collision-resistant Obsidian node name for a lesson."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(topic or "untitled")).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:70].strip("-") or "untitled"
    return f"{LESSON_PREFIX}-{slug}"


class KnowledgeVault:
    """Materializes harness learnings as Obsidian notes under `knowledge_vault/`.

    The SQLite `task_learnings` table stays the queryable index and the retrieval path —
    it already works and is what the Planner reads. This adds a human-readable, graph-
    visible projection of the same rows, so lessons show up in Obsidian linked to the
    repository and issue that produced them, and can be edited or annotated by hand.
    """

    def __init__(self, vault_root: Optional[Path] = None):
        settings.ensure_directories()
        self.root = Path(vault_root) if vault_root else settings.knowledge_dir

    def note_path(self, topic: str) -> Path:
        return self.root / f"{lesson_node_name(topic)}.md"

    def write_lesson(self, learning, task=None) -> Optional[Path]:
        """Write (or refresh) the note for one learning. Never raises."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            node = lesson_node_name(learning.topic)

            # Link to the same shared nodes the task dossier emits, so the lesson and the
            # tasks it came from become connected in the Obsidian graph.
            links: List[str] = []
            if task is not None:
                repo_node = f"Repo-{wikify(str(task.repo_name)).replace('/', '-')}"
                links.append(repo_node)
                if getattr(task, "issue_number", None):
                    platform = str(task.platform.value).lower()
                    repo_slug = wikify(str(task.repo_name)).replace("/", "-")
                    links.append(f"Issue-{platform}-{repo_slug}-{task.issue_number}")

            frontmatter: Dict[str, Any] = {
                "title": learning.topic,
                "type": "lesson",
                "repo": getattr(task, "repo_name", None),
                "tags": ["issueforge/knowledge"] + [f"topic/{wikify(t).lower()}" for t in (learning.tags or [])],
                "source_task": learning.task_id,
                "created_at": learning.created_at.isoformat() if learning.created_at else None,
            }
            header = yaml.safe_dump(
                frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
            )

            body = f"""---
{header}---

# {learning.topic}

## Summary
{_escape_wikilinks(learning.summary)}

## Solution Pattern
{_escape_wikilinks(learning.solution_pattern)}

## Provenance
"""
            if links:
                body += "".join(f"- [[{link}]]\n" for link in links)
            if learning.task_id:
                body += f"- Source task: `{learning.task_id}`\n"

            path = self.note_path(learning.topic)
            path.write_text(body, encoding="utf-8")
            logger.info("Wrote knowledge note %s", path.name)
            return path
        except Exception as exc:
            logger.warning("Could not write knowledge note for %r: %s", getattr(learning, "topic", "?"), exc)
            return None

    def notes_for_task(self, task_id: str) -> List[str]:
        """Node names of lessons produced by a task, read from note frontmatter.

        Synchronous on purpose: the dossier README writer is sync, and the note count is
        small enough that scanning beats threading a DB call through it.
        """
        found: List[str] = []
        if not self.root.exists():
            return found
        for note in sorted(self.root.glob(f"{LESSON_PREFIX}-*.md")):
            try:
                text = note.read_text(encoding="utf-8")
            except OSError:
                continue
            if not text.startswith("---"):
                continue
            end = text.find("\n---", 3)
            if end == -1:
                continue
            try:
                meta = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                continue
            if meta.get("source_task") == task_id:
                found.append(note.stem)
        return found


def _escape_wikilinks(text: Any) -> str:
    """LLM-authored lesson text must not inject graph nodes of its own."""
    return str(text or "").replace("[[", "[​[").replace("]]", "]​]")


def write_lesson_note(learning, task=None) -> Optional[Path]:
    return KnowledgeVault().write_lesson(learning, task=task)


async def backfill_notes() -> int:
    """Materialize notes for every learning already in the database."""
    from issueforge.core.database import get_task, list_learnings

    vault = KnowledgeVault()
    written = 0
    for learning in await list_learnings(limit=1000):
        task = await get_task(learning.task_id) if learning.task_id else None
        if vault.write_lesson(learning, task=task):
            written += 1
    return written
