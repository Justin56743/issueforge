import ast
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

import networkx as nx

logger = logging.getLogger("issueforge.graph.ast")

# Directories that are never part of the project's own dependency graph.
EXCLUDED_DIR_NAMES = {
    ".git", ".issueforge", ".venv", "venv", "env", ".env", "__pycache__",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "site-packages", "dist", "build", ".tox", ".eggs",
}

MAX_FILES = 2000


class CodebaseASTGraph:
    """A directed import graph of a workspace, built with Python's stdlib `ast`.

    Edges point from importer to imported (`a.py -> b.py` means "a imports b"), so the
    reverse graph answers "who breaks if b changes". Only intra-project imports become
    edges — third-party modules resolve to no file and are skipped, which keeps the graph
    about code the agent can actually break.
    """

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)
        self.graph = nx.DiGraph()

    # ------------------------------------------------------------------ building

    def _is_excluded(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self.workspace_root).parts
        except ValueError:
            return True
        return any(p in EXCLUDED_DIR_NAMES or p.startswith(".") for p in parts[:-1])

    def iter_source_files(self) -> List[Path]:
        files: List[Path] = []
        for py_file in sorted(self.workspace_root.rglob("*.py")):
            if self._is_excluded(py_file):
                continue
            files.append(py_file)
            if len(files) >= MAX_FILES:
                logger.warning("AST graph truncated at %d files in %s", MAX_FILES, self.workspace_root)
                break
        return files

    def build_graph(self) -> nx.DiGraph:
        """Parse every project source file and map its import relationships."""
        self.graph.clear()
        if not self.workspace_root.exists():
            return self.graph

        source_files = self.iter_source_files()
        for py_file in source_files:
            rel_path = py_file.relative_to(self.workspace_root).as_posix()
            self.graph.add_node(rel_path, type="source_file")

        for py_file in source_files:
            rel_path = py_file.relative_to(self.workspace_root).as_posix()
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=rel_path)
            except (SyntaxError, UnicodeDecodeError, OSError) as exc:
                # A file the Coder just broke is exactly when this runs; skip, don't fail.
                logger.debug("Skipping unparseable file %s: %s", rel_path, exc)
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self._add_import_edge(rel_path, alias.name)
                elif isinstance(node, ast.ImportFrom):
                    base = self._resolve_relative(rel_path, node)
                    # `from pkg import mod` names a submodule, not a symbol, so try
                    # pkg.mod first — otherwise the edge lands on pkg/__init__.py and
                    # the real dependency on pkg/mod.py is lost.
                    matched_submodule = False
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        candidate = f"{base}.{alias.name}" if base else alias.name
                        if self._module_to_path(candidate, exact=True):
                            self._add_import_edge(rel_path, candidate)
                            matched_submodule = True
                    if not matched_submodule:
                        self._add_import_edge(rel_path, base)

        logger.info(
            "Built AST dependency graph for %s: %d nodes, %d edges",
            self.workspace_root, self.graph.number_of_nodes(), self.graph.number_of_edges(),
        )
        return self.graph

    def _resolve_relative(self, source_rel: str, node: ast.ImportFrom) -> Optional[str]:
        """Turn `from . import x` / `from ..pkg import y` into an absolute module path."""
        if not node.level:
            return node.module

        # level=1 is the containing package, level=2 its parent, and so on.
        parts = Path(source_rel).parent.parts
        climb = node.level - 1
        base = parts[: len(parts) - climb] if climb else parts
        if climb and len(parts) - climb < 0:
            return node.module
        prefix = ".".join(base)
        if node.module:
            return f"{prefix}.{node.module}" if prefix else node.module
        return prefix or None

    def _add_import_edge(self, source_file: str, module_name: Optional[str]) -> None:
        target = self._module_to_path(module_name)
        if target and target != source_file:
            self.graph.add_edge(source_file, target, relation="imports")

    def _module_to_path(self, module_name: Optional[str], exact: bool = False) -> Optional[str]:
        """Resolve a dotted module name to a workspace-relative file, if it is ours.

        Unless `exact`, tries progressively shorter prefixes so
        `from issueforge.core.models import Task` still resolves to `issueforge/core/models.py`
        even though the trailing name is a symbol rather than a module.
        """
        if not module_name:
            return None
        parts = module_name.split(".")
        while parts:
            stem = "/".join(parts)
            for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                if (self.workspace_root / candidate).is_file():
                    return candidate
            if exact:
                return None
            parts.pop()
        return None

    # ------------------------------------------------------------------ queries

    def dependents_of(self, rel_path: str, max_depth: int = 2) -> Set[str]:
        """Files that transitively import `rel_path`, up to `max_depth` hops."""
        if rel_path not in self.graph:
            return set()
        reverse = self.graph.reverse(copy=False)
        found = set(nx.bfs_tree(reverse, source=rel_path, depth_limit=max_depth).nodes())
        found.discard(rel_path)
        return found

    def to_dict(self) -> Dict[str, object]:
        """Serializable form, for persisting under the vault's codebase_graph/."""
        return {
            "workspace_root": str(self.workspace_root),
            "nodes": sorted(self.graph.nodes()),
            "edges": sorted([src, dst] for src, dst in self.graph.edges()),
            "node_count": self.graph.number_of_nodes(),
            "edge_count": self.graph.number_of_edges(),
        }
