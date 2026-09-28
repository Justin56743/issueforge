import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from issueforge.agents.merge_resolver import MergeConflictResolver
from issueforge.config import settings
from issueforge.core.models import PlatformType, Task, TaskType
from issueforge.core.sandbox import NativeSandbox


@pytest.fixture(autouse=True)
def no_real_agy():
    """The resolver tries a local `agy` session before the API.

    Tests must never spawn the real 212MB binary — it would hang the suite on its
    per-file timeout. Reporting "unavailable" exercises the API fallback path, which is
    what the individual tests mock.
    """
    with patch(
        "issueforge.agents.merge_resolver.AgySessionRunner.run_prompt",
        new=AsyncMock(return_value=(False, "")),
    ):
        yield


def _sh(command: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)


def _llm(content: str):
    """A fake litellm response shaped like resp.choices[0].message.content."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _make_task(task_id: str) -> Task:
    return Task(
        id=task_id,
        title="Merge guard fixture",
        description="d",
        repo_url="https://gitlab.com/acme/x",
        repo_name="acme/x",
        working_branch="forge/f",
        platform=PlatformType.GITLAB,
        task_type=TaskType.ISSUE,
        active_run_id="run-1",
    )


@pytest.fixture
def conflicted_repo(tmp_path, monkeypatch):
    """A sandbox whose branch conflicts with origin/main on the same line."""
    monkeypatch.setattr(settings, "forge_tasks_root", tmp_path / "tasks")
    sandbox = NativeSandbox("test-merge", run_id="run-1")
    wp = sandbox.setup()

    origin = tmp_path / "origin.git"
    _sh(f"git init --bare -b main '{origin}'", tmp_path)
    _sh("git init -b main .", wp)
    _sh("git config user.email t@t.t && git config user.name t", wp)

    (wp / "mod.py").write_text("def greet():\n    return 'base'\n")
    (wp / "untouched.py").write_text("SAFE = 1\n")
    _sh("git add -A && git commit -m base", wp)
    _sh(f"git remote add origin '{origin}' && git push -u origin main", wp)

    # origin/main advances
    _sh("git checkout -b upstream", wp)
    (wp / "mod.py").write_text("def greet():\n    return 'from main'\n\ndef extra_main():\n    return 2\n")
    _sh("git add -A && git commit -m upstream && git push origin upstream:main", wp)

    # our branch diverges on the same line
    _sh("git checkout main && git reset --hard HEAD~1 && git checkout -b forge/f", wp)
    (wp / "mod.py").write_text("def greet():\n    return 'from feature'\n\ndef extra_feature():\n    return 3\n")
    _sh("git add -A && git commit -m feature", wp)
    return sandbox, wp


@pytest.fixture
def clean_repo(tmp_path, monkeypatch):
    """A sandbox whose branch merges cleanly into origin/main."""
    monkeypatch.setattr(settings, "forge_tasks_root", tmp_path / "tasks")
    sandbox = NativeSandbox("test-merge-clean", run_id="run-1")
    wp = sandbox.setup()
    origin = tmp_path / "origin.git"
    _sh(f"git init --bare -b main '{origin}'", tmp_path)
    _sh("git init -b main .", wp)
    _sh("git config user.email t@t.t && git config user.name t", wp)
    (wp / "mod.py").write_text("V = 1\n")
    _sh("git add -A && git commit -m base", wp)
    _sh(f"git remote add origin '{origin}' && git push -u origin main", wp)
    _sh("git checkout -b forge/f", wp)
    (wp / "other.py").write_text("W = 2\n")
    _sh("git add -A && git commit -m feature", wp)
    return sandbox, wp


def _is_clean(wp: Path) -> bool:
    return _sh("git status --porcelain", wp).stdout.strip() == ""


def _head(wp: Path) -> str:
    return _sh("git rev-parse HEAD", wp).stdout.strip()


# --------------------------------------------------------------------- pure helpers

def test_strip_code_fences():
    assert MergeConflictResolver.strip_code_fences("```python\nx = 1\n```") == "x = 1"
    assert MergeConflictResolver.strip_code_fences("```\nx = 1\n```") == "x = 1"
    assert MergeConflictResolver.strip_code_fences("x = 1") == "x = 1"


def test_has_conflict_markers():
    assert MergeConflictResolver.has_conflict_markers("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> main\n")
    assert not MergeConflictResolver.has_conflict_markers("x = 1\n")
    # A marker-looking string mid-line is not a conflict marker.
    assert not MergeConflictResolver.has_conflict_markers('s = "a ======= b"\n')


# --------------------------------------------------------------------- probing

async def test_dry_run_conflict_probe_detects_conflict(conflicted_repo):
    sandbox, wp = conflicted_repo
    resolver = MergeConflictResolver(sandbox)
    assert await resolver.probe_for_conflicts("main") is True
    conflicted = await resolver.list_conflicted_files()
    assert [p.name for p in conflicted] == ["mod.py"]
    assert MergeConflictResolver.has_conflict_markers((wp / "mod.py").read_text())
    await resolver.abort_merge()
    assert _is_clean(wp)


async def test_probe_on_clean_branch_aborts_and_reports_no_conflict(clean_repo):
    """A clean probe must not silently merge the target branch into our work."""
    sandbox, wp = clean_repo
    head_before = _head(wp)
    resolver = MergeConflictResolver(sandbox)
    assert await resolver.probe_for_conflicts("main") is False
    assert _is_clean(wp)
    assert _head(wp) == head_before


async def test_pipeline_on_clean_branch_is_a_noop(clean_repo):
    sandbox, wp = clean_repo
    head_before = _head(wp)
    ok, message = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")
    assert ok is True
    assert "clean" in message.lower()
    assert _head(wp) == head_before


# --------------------------------------------------------------------- resolution

async def test_conflict_markers_are_resolved_and_committed(conflicted_repo):
    sandbox, wp = conflicted_repo
    merged = (
        "def greet():\n    return 'from feature'\n\n"
        "def extra_main():\n    return 2\n\n"
        "def extra_feature():\n    return 3\n"
    )
    resolver = MergeConflictResolver(sandbox, task=_make_task("test-merge"))
    with patch("issueforge.agents.merge_resolver.call_llm_with_fallback", new=AsyncMock(return_value=_llm(merged))):
        ok, message = await resolver.execute_resolution_pipeline("main")

    assert ok is True, message
    final = (wp / "mod.py").read_text()
    assert not MergeConflictResolver.has_conflict_markers(final)
    # Both sides' additions survived the reconciliation.
    assert "extra_main" in final and "extra_feature" in final
    assert resolver.resolved_files == ["mod.py"]
    assert "chore(merge)" in _sh("git log -1 --pretty=%s", wp).stdout
    assert _is_clean(wp)


async def test_markdown_fenced_response_is_unwrapped(conflicted_repo):
    sandbox, wp = conflicted_repo
    merged = "def greet():\n    return 'ok'\n"
    with patch("issueforge.agents.merge_resolver.call_llm_with_fallback",
               new=AsyncMock(return_value=_llm(f"```python\n{merged}```"))):
        ok, _ = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")
    assert ok is True
    assert (wp / "mod.py").read_text().strip() == merged.strip()


async def test_syntax_check_leaves_no_bytecode_in_the_workspace(conflicted_repo):
    """compileall must not litter __pycache__ — git add -A would sweep it into the commit."""
    sandbox, wp = conflicted_repo
    with patch("issueforge.agents.merge_resolver.call_llm_with_fallback",
               new=AsyncMock(return_value=_llm("def greet():\n    return 'ok'\n"))):
        ok, _ = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")
    assert ok is True
    assert list(wp.rglob("__pycache__")) == []
    assert _is_clean(wp)


# --------------------------------------------------------------------- failure paths

@pytest.mark.parametrize(
    "bad_response,label",
    [
        ("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> main\n", "markers left in place"),
        ("   ", "empty response"),
        ("def greet(:\n  not python\n", "syntactically broken"),
    ],
)
async def test_failed_resolution_aborts_the_merge(conflicted_repo, bad_response, label):
    """Every failure path must leave the branch exactly as it was."""
    sandbox, wp = conflicted_repo
    head_before = _head(wp)
    with patch("issueforge.agents.merge_resolver.call_llm_with_fallback",
               new=AsyncMock(return_value=_llm(bad_response))):
        ok, message = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")

    assert ok is False, label
    assert message
    assert _is_clean(wp), label
    assert _head(wp) == head_before, label
    assert not (wp / ".git" / "MERGE_HEAD").exists(), label


async def test_llm_exception_aborts_cleanly(conflicted_repo):
    sandbox, wp = conflicted_repo
    head_before = _head(wp)
    with patch("issueforge.agents.merge_resolver.call_llm_with_fallback",
               new=AsyncMock(side_effect=RuntimeError("all models failed"))):
        ok, message = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")
    assert ok is False
    assert "resolve by hand" in message
    assert _is_clean(wp)
    assert _head(wp) == head_before


async def test_unknown_target_branch_does_not_hang_or_corrupt(clean_repo):
    sandbox, wp = clean_repo
    head_before = _head(wp)
    ok, _ = await MergeConflictResolver(sandbox).execute_resolution_pipeline("no-such-branch")
    # No conflict can be detected against a branch that does not exist; must stay clean.
    assert ok is True
    assert _is_clean(wp)
    assert _head(wp) == head_before


# --------------------------------------------------------------------- verification gate

async def test_verification_reports_blast_radius(conflicted_repo):
    sandbox, _wp = conflicted_repo
    resolver = MergeConflictResolver(sandbox)
    resolver.resolved_files = ["mod.py"]
    ok, detail, radius = await resolver.verify_resolution_integrity()
    assert ok is True
    assert "passed" in detail.lower()
    assert radius is not None


async def test_verification_skips_syntax_check_for_non_python(conflicted_repo):
    sandbox, _wp = conflicted_repo
    resolver = MergeConflictResolver(sandbox)
    resolver.resolved_files = ["README.md"]
    ok, detail, _radius = await resolver.verify_resolution_integrity()
    assert ok is True
    assert "skipped" in detail.lower()


# --------------------------------------------------------------------- agy-first routing

async def test_resolution_prefers_a_local_agy_session(conflicted_repo):
    """Project policy is agy first, hosted API only as fallback."""
    sandbox, wp = conflicted_repo
    merged = "def greet():\n    return 'via agy'\n"

    agy = AsyncMock(return_value=(True, merged))
    api = AsyncMock()
    with patch("issueforge.agents.merge_resolver.AgySessionRunner.run_prompt", new=agy), \
         patch("issueforge.agents.merge_resolver.call_llm_with_fallback", new=api):
        ok, _msg = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")

    assert ok is True
    agy.assert_awaited()
    api.assert_not_awaited()          # the API must not be touched when agy answers
    assert "via agy" in (wp / "mod.py").read_text()


async def test_resolution_falls_back_to_the_api_when_agy_fails(conflicted_repo):
    sandbox, wp = conflicted_repo
    merged = "def greet():\n    return 'via api'\n"

    agy = AsyncMock(side_effect=RuntimeError("agy binary missing"))
    with patch("issueforge.agents.merge_resolver.AgySessionRunner.run_prompt", new=agy), \
         patch("issueforge.agents.merge_resolver.call_llm_with_fallback",
               new=AsyncMock(return_value=_llm(merged))):
        ok, _msg = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")

    assert ok is True
    assert "via api" in (wp / "mod.py").read_text()


async def test_resolution_fails_cleanly_when_both_routes_fail(conflicted_repo):
    sandbox, wp = conflicted_repo
    head_before = _head(wp)
    with patch("issueforge.agents.merge_resolver.AgySessionRunner.run_prompt",
               new=AsyncMock(return_value=(False, ""))), \
         patch("issueforge.agents.merge_resolver.call_llm_with_fallback",
               new=AsyncMock(side_effect=RuntimeError("no models"))):
        ok, message = await MergeConflictResolver(sandbox).execute_resolution_pipeline("main")

    assert ok is False
    assert "resolve by hand" in message
    assert _is_clean(wp)
    assert _head(wp) == head_before
