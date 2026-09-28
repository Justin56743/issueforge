from pathlib import Path

import pytest

from issueforge.graph.ast_parser import CodebaseASTGraph
from issueforge.graph.blast_radius import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    analyze_workspace,
    calculate_blast_radius,
    is_test_path,
)


@pytest.fixture
def workspace(tmp_path) -> Path:
    """A small multi-module project exercising every import form."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text("VALUE = 1\n")
    (tmp_path / "pkg" / "service.py").write_text("from pkg.core import VALUE\n")
    (tmp_path / "pkg" / "api.py").write_text("from pkg.service import VALUE\nimport json\n")
    (tmp_path / "pkg" / "relative.py").write_text("from . import core\n")
    (tmp_path / "pkg" / "submodule_import.py").write_text("from pkg import core\n")
    (tmp_path / "pkg" / "standalone.py").write_text("import os\n")
    (tmp_path / "tests" / "test_core.py").write_text("from pkg.core import VALUE\n")
    (tmp_path / "tests" / "test_api.py").write_text("from pkg.api import VALUE\n")
    return tmp_path


# --------------------------------------------------------------------- graph construction

def test_ast_graph_construction(workspace):
    graph = CodebaseASTGraph(workspace)
    graph.build_graph()
    assert "pkg/core.py" in graph.graph
    assert graph.graph.has_edge("pkg/service.py", "pkg/core.py")
    assert graph.graph.has_edge("pkg/api.py", "pkg/service.py")
    assert graph.graph.has_edge("tests/test_core.py", "pkg/core.py")


def test_third_party_imports_are_not_nodes(workspace):
    graph = CodebaseASTGraph(workspace)
    graph.build_graph()
    # `import json` / `import os` resolve to no project file and must not pollute the graph.
    assert not any("json" in n or n == "os.py" for n in graph.graph.nodes())
    assert graph.graph.out_degree("pkg/standalone.py") == 0


def test_relative_and_submodule_imports_resolve_to_the_module(workspace):
    """`from . import core` and `from pkg import core` must hit pkg/core.py, not __init__."""
    graph = CodebaseASTGraph(workspace)
    graph.build_graph()
    assert graph.graph.has_edge("pkg/relative.py", "pkg/core.py")
    assert graph.graph.has_edge("pkg/submodule_import.py", "pkg/core.py")


def test_excluded_directories_are_skipped(workspace):
    for junk in (".venv", "node_modules", "__pycache__"):
        (workspace / junk).mkdir()
        (workspace / junk / "noise.py").write_text("import os\n")
    graph = CodebaseASTGraph(workspace)
    graph.build_graph()
    assert not any("noise.py" in n for n in graph.graph.nodes())


def test_unparseable_file_does_not_break_the_graph(workspace):
    """The Coder may have just written a syntax error; the graph must still build."""
    (workspace / "pkg" / "broken.py").write_text("def oops(:\n")
    graph = CodebaseASTGraph(workspace)
    graph.build_graph()
    assert "pkg/broken.py" in graph.graph          # still a node
    assert graph.graph.has_edge("pkg/service.py", "pkg/core.py")  # others unaffected


def test_graph_serializes_for_the_vault(workspace):
    graph = CodebaseASTGraph(workspace)
    graph.build_graph()
    payload = graph.to_dict()
    assert payload["node_count"] == len(payload["nodes"])
    assert payload["edge_count"] == len(payload["edges"])
    assert all(isinstance(e, list) and len(e) == 2 for e in payload["edges"])


def test_empty_or_missing_workspace(tmp_path):
    assert CodebaseASTGraph(tmp_path / "nope").build_graph().number_of_nodes() == 0
    assert CodebaseASTGraph(tmp_path).build_graph().number_of_nodes() == 0


# --------------------------------------------------------------------- blast radius

def test_blast_radius_finds_downstream_dependents(workspace):
    radius = analyze_workspace(workspace, ["pkg/core.py"])
    assert not radius.indeterminate
    assert "pkg/service.py" in radius.impacted_files   # depth 1
    assert "pkg/api.py" in radius.impacted_files       # depth 2
    assert "pkg/core.py" in radius.impacted_files      # the root itself


def test_blast_radius_respects_max_depth(workspace):
    shallow = analyze_workspace(workspace, ["pkg/core.py"], max_depth=1)
    assert "pkg/service.py" in shallow.impacted_files
    assert "pkg/api.py" not in shallow.impacted_files
    assert "pkg/api.py" in analyze_workspace(workspace, ["pkg/core.py"], max_depth=2).impacted_files


def test_selective_test_discovery(workspace):
    radius = analyze_workspace(workspace, ["pkg/core.py"])
    assert "tests/test_core.py" in radius.suggested_tests
    # test_api.py imports api -> service -> core, three hops from core.
    assert "tests/test_api.py" not in radius.suggested_tests
    assert "tests/test_api.py" in analyze_workspace(workspace, ["pkg/core.py"], max_depth=3).suggested_tests


def test_leaf_change_is_low_risk(workspace):
    radius = analyze_workspace(workspace, ["pkg/standalone.py"])
    assert radius.risk_level == RISK_LOW
    assert radius.impacted_files == ["pkg/standalone.py"]


def test_risk_levels_scale_with_impact(tmp_path):
    (tmp_path / "hub.py").write_text("V = 1\n")
    graph_files = []
    for i in range(12):
        name = f"leaf{i}.py"
        (tmp_path / name).write_text("from hub import V\n")
        graph_files.append(name)
    graph = CodebaseASTGraph(tmp_path)
    graph.build_graph()
    assert calculate_blast_radius(graph, ["hub.py"]).risk_level == RISK_HIGH
    assert calculate_blast_radius(graph, ["leaf0.py"]).risk_level == RISK_LOW


def test_medium_risk_band(tmp_path):
    (tmp_path / "hub.py").write_text("V = 1\n")
    for i in range(4):
        (tmp_path / f"leaf{i}.py").write_text("from hub import V\n")
    graph = CodebaseASTGraph(tmp_path)
    graph.build_graph()
    # 1 root + 4 dependents = 5 impacted, 0 tests -> MEDIUM
    assert calculate_blast_radius(graph, ["hub.py"]).risk_level == RISK_MEDIUM


def test_non_python_change_is_flagged_indeterminate(workspace):
    """A README or YAML change has no import graph; that must not read as 'no impact'."""
    radius = analyze_workspace(workspace, ["README.md"])
    assert radius.indeterminate is True
    assert "not determinable" in radius.summary()


def test_untracked_new_file_is_indeterminate(workspace):
    radius = analyze_workspace(workspace, ["pkg/brand_new.py"])
    assert radius.indeterminate is True


def test_analyze_workspace_never_raises(tmp_path):
    radius = analyze_workspace(tmp_path / "does-not-exist", ["a.py"])
    assert radius.indeterminate is True
    assert radius.modified_files == ["a.py"]


def test_blast_radius_is_serializable(workspace):
    payload = analyze_workspace(workspace, ["pkg/core.py"]).to_dict()
    for key in ("impacted_files", "suggested_tests", "risk_level", "indeterminate"):
        assert key in payload


# --------------------------------------------------------------------- test detection

def test_is_test_path_matches_pytest_discovery():
    assert is_test_path("tests/test_core.py")
    assert is_test_path("test_core.py")
    assert is_test_path("pkg/core_test.py")
    assert is_test_path("tests/helpers.py")      # inside a tests/ dir
    assert not is_test_path("pkg/core.py")
    assert not is_test_path("pkg/latest.py")     # 'test' as a substring, not a test
    assert not is_test_path("tests/fixture.json")
