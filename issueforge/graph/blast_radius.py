import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from issueforge.graph.ast_parser import CodebaseASTGraph

logger = logging.getLogger("issueforge.graph.blast_radius")

# pytest's own discovery rules, so selected tests are ones the runner would actually collect.
_TEST_FILE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py)$")
_TEST_DIR = re.compile(r"(^|/)tests?(/|$)")

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"


def is_test_path(rel_path: str) -> bool:
    """True for files pytest would collect as tests."""
    path = str(rel_path).replace("\\", "/")
    return bool(_TEST_FILE.search(path)) or (
        bool(_TEST_DIR.search(path)) and path.endswith(".py")
    )


@dataclass
class BlastRadius:
    """Downstream impact of a set of modified files."""

    modified_files: List[str] = field(default_factory=list)
    # Modified files that are actually in the graph (i.e. project Python sources).
    tracked_roots: List[str] = field(default_factory=list)
    impacted_files: List[str] = field(default_factory=list)
    suggested_tests: List[str] = field(default_factory=list)
    risk_level: str = RISK_LOW
    max_depth: int = 2
    graph_nodes: int = 0
    graph_edges: int = 0
    # True when nothing could be traced: no graph, or no modified file appears in it.
    # Callers must not read "no impact" as "safe" in that case.
    indeterminate: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "modified_files": self.modified_files,
            "tracked_roots": self.tracked_roots,
            "impacted_files": self.impacted_files,
            "suggested_tests": self.suggested_tests,
            "risk_level": self.risk_level,
            "max_depth": self.max_depth,
            "graph_nodes": self.graph_nodes,
            "graph_edges": self.graph_edges,
            "indeterminate": self.indeterminate,
        }

    def summary(self) -> str:
        if self.indeterminate:
            return "Blast radius: not determinable (no Python import graph for these changes)"
        return (
            f"Blast radius: {self.risk_level} risk — "
            f"{len(self.impacted_files)} impacted file(s), "
            f"{len(self.suggested_tests)} related test file(s)"
        )


def _assess_risk(impacted: Sequence[str], tests: Sequence[str]) -> str:
    if len(impacted) > 8 or len(tests) > 4:
        return RISK_HIGH
    if len(impacted) > 3:
        return RISK_MEDIUM
    return RISK_LOW


def calculate_blast_radius(
    graph: CodebaseASTGraph,
    modified_files: Sequence[str],
    max_depth: int = 2,
) -> BlastRadius:
    """Trace downstream dependents of the modified files and score the risk.

    Advisory only: the result is surfaced to the operator and the Reviewer, and never
    used to narrow what the Tester actually runs. A missed import edge (dynamic imports,
    plugin registries, non-Python callers) would otherwise silently skip a real test.
    """
    modified = [str(f).replace("\\", "/").lstrip("./") for f in modified_files if f]
    result = BlastRadius(
        modified_files=sorted(set(modified)),
        max_depth=max_depth,
        graph_nodes=graph.graph.number_of_nodes(),
        graph_edges=graph.graph.number_of_edges(),
    )

    tracked = [f for f in result.modified_files if f in graph.graph]
    result.tracked_roots = tracked

    if not tracked:
        # Either the change is non-Python, or the graph could not be built.
        result.indeterminate = True
        result.impacted_files = result.modified_files
        result.suggested_tests = sorted(f for f in result.modified_files if is_test_path(f))
        return result

    impacted = set(tracked)
    for root in tracked:
        impacted |= graph.dependents_of(root, max_depth=max_depth)

    result.impacted_files = sorted(impacted)
    result.suggested_tests = sorted(f for f in impacted if is_test_path(f))
    result.risk_level = _assess_risk(result.impacted_files, result.suggested_tests)
    return result


def persist_graph(graph: CodebaseASTGraph, repo_name: str) -> Optional[Path]:
    """Write the import graph into the vault's codebase_graph/ for inspection.

    Best-effort: a graph artifact is never worth failing a pipeline over.
    """
    try:
        import json

        from issueforge.config import settings

        settings.ensure_directories()
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", repo_name or "workspace").strip("-") or "workspace"
        out_dir = settings.graph_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{slug}-ast.json"
        out_path.write_text(json.dumps(graph.to_dict(), indent=2), encoding="utf-8")
        return out_path
    except Exception as exc:
        logger.debug("Could not persist AST graph for %s: %s", repo_name, exc)
        return None


def analyze_workspace(
    workspace_root: Path,
    modified_files: Sequence[str],
    max_depth: int = 2,
    repo_name: Optional[str] = None,
) -> BlastRadius:
    """Build the import graph for a workspace and compute the blast radius in one step."""
    try:
        graph = CodebaseASTGraph(Path(workspace_root))
        graph.build_graph()
        if repo_name:
            persist_graph(graph, repo_name)
        return calculate_blast_radius(graph, modified_files, max_depth=max_depth)
    except Exception as exc:
        logger.warning("Blast radius analysis failed for %s: %s", workspace_root, exc)
        radius = BlastRadius(
            modified_files=sorted({str(f) for f in modified_files if f}),
            max_depth=max_depth,
            indeterminate=True,
        )
        radius.impacted_files = radius.modified_files
        return radius
