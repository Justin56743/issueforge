import hashlib
import json
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("issueforge.vault.canvas")


class CanvasStage(str, Enum):
    """The seven lifecycle nodes rendered on a task's execution DAG."""

    ISSUE = "issue"
    PLANNER = "planner"
    GATE = "gate"
    CODER = "coder"
    TESTER = "tester"
    MERGE = "merge"
    REVIEWER = "reviewer"


STAGE_ORDER: Tuple[CanvasStage, ...] = (
    CanvasStage.ISSUE,
    CanvasStage.PLANNER,
    CanvasStage.GATE,
    CanvasStage.CODER,
    CanvasStage.TESTER,
    CanvasStage.MERGE,
    CanvasStage.REVIEWER,
)


class CanvasColor(str, Enum):
    """Obsidian's predefined canvas colour slots."""

    RED = "1"       # FAILED / syntax regression
    ORANGE = "2"    # WARNING / conflict / human directive
    YELLOW = "3"    # RUNNING / in-flight
    GREEN = "4"     # SUCCESS / verified
    CYAN = "5"      # HITL_PAUSED / awaiting operator
    PURPLE = "6"    # REVIEW / architectural ingestion


DIRECTIVE_HEADER = "### 🧑‍💻 Operator Steering Directive"
DIRECTIVE_PREFIX = "STEER:"
DIRECTIVE_APPLIED_HEADER = "### ✅ Directive Queued by Issueforge"

CANVAS_FILENAME = "task_dag.canvas"

_NODE_WIDTH = 280
_NODE_HEIGHT = 160
_NODE_X0 = -450
_NODE_XSTEP = 350

# Initial text and colour for each stage node.
_INITIAL_STAGES: Dict[CanvasStage, Tuple[str, Optional[str]]] = {
    CanvasStage.ISSUE: ("### 📥 Ingested Task", CanvasColor.PURPLE.value),
    CanvasStage.PLANNER: ("### 🧠 Planner Agent\n\n*Status: PENDING*", None),
    CanvasStage.GATE: ("### 🚦 Branch Gate\n\n*Awaiting Target Selection*", CanvasColor.CYAN.value),
    CanvasStage.CODER: ("### 💻 Coder Agent (`agy`)\n\n*Status: IDLE*", None),
    CanvasStage.TESTER: ("### 🧪 Tester Agent\n\n*Status: AWAITING CODE*", None),
    CanvasStage.MERGE: ("### 🔀 AST Merge Guard\n\n*Status: IDLE*", None),
    CanvasStage.REVIEWER: ("### 🔍 Reviewer Agent\n\n*Status: PENDING*", None),
}


def _coerce_stage(stage: Union[CanvasStage, int, str]) -> CanvasStage:
    """Accept a stage enum, its value, or the spec's positional index."""
    if isinstance(stage, CanvasStage):
        return stage
    if isinstance(stage, int):
        if 0 <= stage < len(STAGE_ORDER):
            return STAGE_ORDER[stage]
        raise ValueError(f"stage index out of range: {stage}")
    return CanvasStage(stage)


class CanvasDAGBuilder:
    """Constructs and mutates an Obsidian Canvas file as a live pipeline DAG.

    Node identity is a deterministic `sha1(task_id:stage)` rather than a list position. That
    matters: the spec's positional indexing silently writes to the wrong node as soon as an
    operator directive card is appended, and Obsidian is free to reorder nodes when the
    operator drags them. Deriving ids means nothing has to be persisted alongside the canvas
    and the mapping survives rewrites, reordering and restarts.
    """

    def __init__(self, canvas_path: Path, task_id: str):
        self.canvas_path = Path(canvas_path)
        self.task_id = task_id
        self.data: Dict[str, List[Any]] = {"nodes": [], "edges": []}
        self.reload()

    @classmethod
    def for_task(cls, task_id: str) -> "CanvasDAGBuilder":
        from issueforge.core.task_dossier import TaskDossierManager

        return cls(TaskDossierManager.get_task_dir(task_id) / CANVAS_FILENAME, task_id)

    # ------------------------------------------------------------------ identity

    def stage_node_id(self, stage: Union[CanvasStage, int, str]) -> str:
        key = f"{self.task_id}:{_coerce_stage(stage).value}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def _edge_id(self, src: CanvasStage, dst: CanvasStage) -> str:
        key = f"{self.task_id}:edge:{src.value}->{dst.value}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _random_id() -> str:
        return hashlib.sha1(os.urandom(16)).hexdigest()[:16]

    # ------------------------------------------------------------------ io

    def reload(self) -> None:
        if not self.canvas_path.exists():
            self.data = {"nodes": [], "edges": []}
            return
        try:
            loaded = json.loads(self.canvas_path.read_text(encoding="utf-8"))
            self.data = {
                "nodes": list(loaded.get("nodes", [])),
                "edges": list(loaded.get("edges", [])),
            }
        except Exception as exc:
            logger.error("Failed to parse canvas %s (%s). Treating as blank.", self.canvas_path, exc)
            self.data = {"nodes": [], "edges": []}

    def save(self) -> None:
        """Write atomically — a watcher reading a half-written file would see invalid JSON."""
        self.canvas_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.canvas_path.with_name(self.canvas_path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.canvas_path)

    # ------------------------------------------------------------------ queries

    def get_stage_node(self, stage: Union[CanvasStage, int, str]) -> Optional[Dict[str, Any]]:
        node_id = self.stage_node_id(stage)
        return next((n for n in self.data["nodes"] if n.get("id") == node_id), None)

    def is_initialized(self) -> bool:
        present = {n.get("id") for n in self.data["nodes"]}
        return all(self.stage_node_id(s) in present for s in STAGE_ORDER)

    # ------------------------------------------------------------------ mutation

    def initialize_task_dag(self, issue_title: str, force: bool = False) -> None:
        """Build the clean horizontal lifecycle DAG.

        Rebuilds from scratch, discarding operator-added cards — so callers that run often
        should use `ensure_initialized` instead.
        """
        if self.is_initialized() and not force:
            return

        nodes: List[Dict[str, Any]] = []
        for index, stage in enumerate(STAGE_ORDER):
            text, color = _INITIAL_STAGES[stage]
            if stage is CanvasStage.ISSUE:
                text = f"{text}\n**{self.task_id}**\n\n*{issue_title}*"
            node: Dict[str, Any] = {
                "id": self.stage_node_id(stage),
                "type": "text",
                "text": text,
                "x": _NODE_X0 + (_NODE_XSTEP * index),
                "y": 0,
                "width": _NODE_WIDTH,
                "height": _NODE_HEIGHT,
            }
            if color:
                node["color"] = color
            nodes.append(node)

        edges = [
            {
                "id": self._edge_id(STAGE_ORDER[i], STAGE_ORDER[i + 1]),
                "fromNode": self.stage_node_id(STAGE_ORDER[i]),
                "fromSide": "right",
                "toNode": self.stage_node_id(STAGE_ORDER[i + 1]),
                "toSide": "left",
            }
            for i in range(len(STAGE_ORDER) - 1)
        ]

        self.data = {"nodes": nodes, "edges": edges}
        self.save()

    def ensure_initialized(self, issue_title: str) -> None:
        """Create the DAG if absent or incomplete, preserving any operator-added nodes."""
        self.reload()
        if self.is_initialized():
            return
        preserved_nodes = [
            n for n in self.data["nodes"]
            if n.get("id") not in {self.stage_node_id(s) for s in STAGE_ORDER}
        ]
        preserved_edges = list(self.data["edges"])
        self.initialize_task_dag(issue_title, force=True)
        if preserved_nodes:
            stage_ids = {self.stage_node_id(s) for s in STAGE_ORDER}
            self.data["nodes"].extend(preserved_nodes)
            known = {n.get("id") for n in self.data["nodes"]}
            self.data["edges"].extend(
                e for e in preserved_edges
                if e.get("fromNode") in known and e.get("toNode") in known
                and not (e.get("fromNode") in stage_ids and e.get("toNode") in stage_ids)
            )
            self.save()

    def update_stage_node(
        self,
        stage: Union[CanvasStage, int, str],
        markdown_text: str,
        color_code: Optional[str] = None,
    ) -> bool:
        """Update one stage node's telemetry text and colour. Returns False if it is missing."""
        self.reload()
        node = self.get_stage_node(stage)
        if node is None:
            logger.debug("Canvas stage %s not present in %s", stage, self.canvas_path)
            return False

        # Skip pointless rewrites: they cost IO and wake the steering watcher.
        if node.get("text") == markdown_text and node.get("color") == color_code:
            return True

        node["text"] = markdown_text
        if color_code:
            node["color"] = color_code
        else:
            node.pop("color", None)
        self.save()
        return True

    def inject_steering_directive(
        self,
        directive_text: str,
        target_stage: Union[CanvasStage, int, str] = CanvasStage.CODER,
        acknowledged: bool = False,
    ) -> Optional[str]:
        """Attach an operator directive card above a stage node.

        `acknowledged=True` writes the card already marked as queued (green). Use it when
        the directive has been applied through another route — the dashboard — so the file
        watcher does not see a fresh directive node and queue the same instruction twice.
        """
        self.reload()
        target = self.get_stage_node(target_stage) or (
            self.data["nodes"][0] if self.data["nodes"] else None
        )
        if target is None:
            return None

        directive_id = self._random_id()
        if acknowledged:
            text = (
                f"{DIRECTIVE_APPLIED_HEADER}\n\n{directive_text}\n\n"
                f"*Queued from the dashboard — applies at the next agent turn.*"
            )
            color = CanvasColor.GREEN.value
        else:
            text = f"{DIRECTIVE_HEADER}\n\n{directive_text}"
            color = CanvasColor.ORANGE.value

        self.data["nodes"].append({
            "id": directive_id,
            "type": "text",
            "text": text,
            "x": target.get("x", 0),
            "y": target.get("y", 0) - 220,
            "width": _NODE_WIDTH,
            "height": _NODE_HEIGHT,
            "color": color,
        })
        self.data["edges"].append({
            "id": self._random_id(),
            "fromNode": directive_id,
            "fromSide": "bottom",
            "toNode": target["id"],
            "toSide": "top",
        })
        self.save()
        return directive_id

    def mark_directive_applied(self, node_id: str, note: str = "") -> bool:
        """Relabel a directive card green once queued.

        Doubles as the watcher's loop-breaker: the rewritten text is no longer a directive,
        so our own save cannot be re-detected as new operator input.
        """
        self.reload()
        node = next((n for n in self.data["nodes"] if n.get("id") == node_id), None)
        if node is None:
            return False
        body = clean_directive_text(node.get("text", ""))
        suffix = f"\n\n*{note}*" if note else ""
        node["text"] = f"{DIRECTIVE_APPLIED_HEADER}\n\n{body}{suffix}"
        node["color"] = CanvasColor.GREEN.value
        self.save()
        return True


def is_directive_text(text: str) -> bool:
    stripped = (text or "").strip()
    return DIRECTIVE_HEADER in stripped or stripped.startswith(DIRECTIVE_PREFIX)


def clean_directive_text(text: str) -> str:
    return (
        (text or "")
        .replace(DIRECTIVE_HEADER, "")
        .replace(DIRECTIVE_APPLIED_HEADER, "")
        .replace(DIRECTIVE_PREFIX, "", 1)
        .strip()
    )


def canvas_for(task_id: str) -> CanvasDAGBuilder:
    """Convenience accessor for a task's canvas at its dossier root."""
    return CanvasDAGBuilder.for_task(task_id)
