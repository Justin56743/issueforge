import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from issueforge.config import settings

logger = logging.getLogger("issueforge.vault.manager")

# Color-coded graph topology groups. Values are Obsidian's packed 24-bit RGB integers.
_COLOR_GROUPS = [
    {"query": "tag:#issueforge/task", "color": {"a": 1, "rgb": 4317676}},        # Emerald: tasks
    {"query": "tag:#issueforge/knowledge", "color": {"a": 1, "rgb": 16744448}},  # Orange: knowledge
    {"query": "tag:#issueforge/ast", "color": {"a": 1, "rgb": 2201331}},         # Cyan: code AST
    {"query": "tag:#priority/critical", "color": {"a": 1, "rgb": 16711680}},  # Red: critical
]

_APP_CONFIG: Dict[str, Any] = {
    "legacyEditor": False,
    "livePreview": True,
    "showLineNumber": True,
    "useTab": False,
    "tabSize": 4,
    "autoConvertHtml": True,
    "attachmentFolderPath": "attachments",
}

_GRAPH_CONFIG: Dict[str, Any] = {
    "collapse-filter": False,
    "search": "",
    "showTags": True,
    "showAttachments": False,
    # Unresolved links are meaningful here: Issue-* and Branch-* nodes have no backing file
    # but are what visually connects tasks to each other.
    "hideUnresolved": False,
    "showOrphans": True,
    "colorGroups": _COLOR_GROUPS,
    # Forces tuned for the high node density of an edge device's task history.
    "forces": {
        "textOpacityThreshold": -3,
        "nodeStrength": -230,
        "linkStrength": 15,
        "linkDistance": 30,
        "centerStrength": 0.5,
    },
}


class VaultManager:
    """Manages the physical Obsidian vault structure on the native filesystem.

    Ensures the vault root behaves as a fully-featured Obsidian workspace with custom
    graph groupings. Existing config files are never overwritten, so operator tweaks and
    Obsidian's own rewrites survive a restart.
    """

    def __init__(self, vault_root: Optional[Path] = None):
        settings.ensure_directories()
        self.vault_root = Path(vault_root).expanduser().resolve() if vault_root else settings.forge_vault_root
        self.obsidian_dir = self.vault_root / ".obsidian"
        self.tasks_dir = self.vault_root / "tasks"
        self.knowledge_dir = self.vault_root / "knowledge_vault"
        self.graph_dir = self.vault_root / "codebase_graph"

    def initialize_vault(self) -> None:
        """Create the mandatory vault directories and seed the Obsidian configuration."""
        for directory in (self.obsidian_dir, self.tasks_dir, self.knowledge_dir, self.graph_dir):
            directory.mkdir(parents=True, exist_ok=True)
            logger.debug("Verified vault directory: %s", directory)

        self._write_if_absent(self.obsidian_dir / "app.json", _APP_CONFIG)
        self._write_if_absent(self.obsidian_dir / "graph.json", _GRAPH_CONFIG)

        logger.info("Obsidian vault initialized at %s", self.vault_root)

    @staticmethod
    def _write_if_absent(path: Path, payload: Dict[str, Any]) -> bool:
        """Write JSON config only when missing. Returns True if a file was created."""
        if path.exists():
            return False
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Wrote vault config: %s", path)
        return True
