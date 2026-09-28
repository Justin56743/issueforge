"""Obsidian vault subsystem.

Issueforge stores its state as an Obsidian-compliant vault rather than an opaque cache, so the
operator can inspect and steer the system from Obsidian itself:

- `manager`        — vault bootstrap and `.obsidian/` configuration
- `linker`         — `[[wikilink]]` extraction and SQLite edge indexing
- `canvas_builder` — live `task_dag.canvas` execution DAG
- `canvas_watcher` — reverse steering from operator notes dropped on the canvas
"""

from issueforge.vault.canvas_builder import CanvasColor, CanvasDAGBuilder, CanvasStage, canvas_for
from issueforge.vault.canvas_watcher import CanvasSteeringWatcher, canvas_watchers
from issueforge.vault.linker import VaultLinkIndexer, sync_task_graph, wikify
from issueforge.vault.manager import VaultManager

__all__ = [
    "CanvasColor",
    "CanvasDAGBuilder",
    "CanvasSteeringWatcher",
    "CanvasStage",
    "VaultLinkIndexer",
    "VaultManager",
    "canvas_for",
    "canvas_watchers",
    "sync_task_graph",
    "wikify",
]
