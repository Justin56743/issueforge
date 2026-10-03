import pytest
from issueforge.config import settings
from issueforge.core.sandbox import NativeSandbox
from issueforge.git.repo_manager import GitRepoManager


def _credential_fill(env: dict, host: str, tmp_path) -> str:
    """Ask git, with only `env` and an empty HOME, what it would send to `host`."""
    import subprocess
    run_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **env}
    res = subprocess.run(
        ["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n",
        capture_output=True, text=True, env=run_env,
    )
    return res.stdout


def test_token_is_scoped_to_the_exact_host_and_never_in_the_url(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "github_token", "ghp_secret_token_123")
    monkeypatch.setattr(settings, "gitlab_token", "glpat_secret_token_456")
    sandbox = NativeSandbox("test-git-001")

    gh = GitRepoManager(sandbox, "https://github.com/org/repo.git").credential_env()
    assert "password=ghp_secret_token_123" in _credential_fill(gh, "github.com", tmp_path)
    # A look-alike host gets nothing, even with the GitHub credentials in the env.
    assert "ghp_secret" not in _credential_fill(gh, "github.com.evil.io", tmp_path)

    gl = GitRepoManager(sandbox, "https://gitlab.com/org/repo.git").credential_env()
    assert "password=glpat_secret_token_456" in _credential_fill(gl, "gitlab.com", tmp_path)

    # Substring matches used to inject the PATs into these URLs.
    assert GitRepoManager(sandbox, "https://github.com.evil.io/a/b.git").credential_env() == {}
    assert GitRepoManager(sandbox, "https://gitlab.attacker.net/a/b.git").credential_env() == {}
    assert not hasattr(GitRepoManager, "_get_authenticated_url")


@pytest.mark.asyncio
async def test_git_local_repo_operations():
    task_id = "test-git-ops"
    sandbox = NativeSandbox(task_id)
    sandbox.setup()

    # Initialize a local mock git repo
    await sandbox.run_command("git init", emit_events=False)
    await sandbox.run_command('git config user.name "Test"', emit_events=False)
    await sandbox.run_command('git config user.email "test@test.com"', emit_events=False)

    sandbox.write_file("main.py", "print('initial')\n")
    await sandbox.run_command("git add main.py && git commit -m 'initial commit'", emit_events=False)

    git_mgr = GitRepoManager(sandbox, "https://github.com/test/repo.git", base_branch="main", working_branch="forge/test-branch")
    await git_mgr.create_working_branch()

    # Modify file
    sandbox.write_file("main.py", "print('modified')\n")
    sandbox.write_file("new_file.py", "print('new')\n")

    stat, diff_text, file_diffs = await git_mgr.get_diff()
    assert "main.py" in stat or "new_file.py" in stat
    assert len(file_diffs) >= 1

    # Commit changes
    committed = await git_mgr.commit_changes("feat: update main")
    assert committed

    sandbox.cleanup()


async def _make_origin_repo(path, extra_files=None):
    """Create a local git repository usable as a clone source."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    run = lambda cmd: subprocess.run(cmd, shell=True, cwd=path, capture_output=True, text=True, check=True)
    run("git init -b main")
    run('git config user.name "Origin"')
    run('git config user.email "origin@test.dev"')
    (path / "main.py").write_text("print('origin')\n", encoding="utf-8")
    for name, content in (extra_files or {}).items():
        (path / name).write_text(content, encoding="utf-8")
    run("git add -A")
    run('git commit -m "initial commit"')


async def test_agent_artifacts_never_reach_the_commit(tmp_path):
    """PLAN.md/TEST_RESULTS.md/REVIEW.md are agent handoff notes, not the operator's change."""
    origin = tmp_path / "origin"
    await _make_origin_repo(origin)

    sandbox = NativeSandbox("test-git-artifact-exclude")
    git_mgr = GitRepoManager(sandbox, str(origin), base_branch="main", working_branch="forge/artifacts")
    assert await git_mgr.clone_repository()
    await git_mgr.create_working_branch()

    sandbox.write_file("PLAN.md", "# Plan\n")
    sandbox.write_file("TEST_RESULTS.md", "# Results\n")
    sandbox.write_file("REVIEW.md", "# Review\n")
    sandbox.write_file("feature.py", "print('real work')\n")

    # The artifacts must not even register as workspace changes.
    changed = await git_mgr.get_changed_file_paths()
    assert changed == ["feature.py"]

    assert await git_mgr.commit_changes("feat: real work")

    tracked = (await sandbox.run_command("git ls-files", emit_events=False)).stdout.split()
    assert "feature.py" in tracked
    for artifact in ("PLAN.md", "TEST_RESULTS.md", "REVIEW.md"):
        assert artifact not in tracked
        # They stay on disk — the downstream agents still read them.
        assert (sandbox.workspace_path / artifact).exists()

    sandbox.cleanup()


async def test_repo_that_tracks_its_own_plan_still_commits_plan_changes(tmp_path):
    """The exclusion is untracked-only: a repository versioning PLAN.md keeps working."""
    origin = tmp_path / "origin-with-plan"
    await _make_origin_repo(origin, extra_files={"PLAN.md": "# Upstream plan\n"})

    sandbox = NativeSandbox("test-git-tracked-plan")
    git_mgr = GitRepoManager(sandbox, str(origin), base_branch="main", working_branch="forge/tracked-plan")
    assert await git_mgr.clone_repository()
    await git_mgr.create_working_branch()

    sandbox.write_file("PLAN.md", "# Upstream plan\n\n## New section\n")
    assert "PLAN.md" in await git_mgr.get_changed_file_paths()

    assert await git_mgr.commit_changes("docs: extend the plan")
    committed = (await sandbox.run_command("git show --name-only --format= HEAD", emit_events=False)).stdout
    assert "PLAN.md" in committed

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_diff_includes_new_files_and_commit_skips_agent_skills():
    """A Coder that only creates files must still produce a diff, and the skill files
    copied into the sandbox for agy must never reach the commit."""
    sandbox = NativeSandbox("test-git-new-files")
    sandbox.setup()
    # Identity before the first commit: CI runners have no global git identity.
    await sandbox.run_command(
        'git init -q && git config user.name "Test" && git config user.email "t@t.com" '
        "&& git commit -q --allow-empty -m init",
        emit_events=False,
    )
    git_mgr = GitRepoManager(sandbox, "https://github.com/test/repo.git", working_branch="forge/t")
    git_mgr._exclude_agent_artifacts()

    sandbox.write_file(".agents/skills/ponytail/SKILL.md", "skill\n")
    sandbox.write_file("brand_new.py", "print('new')\n")

    stat, diff_text, _ = await git_mgr.get_diff()
    assert "brand_new.py" in stat
    assert "+print('new')" in diff_text
    assert ".agents" not in diff_text

    assert await git_mgr.commit_changes("feat: add module")
    committed = await sandbox.run_command("git show --name-only --format= HEAD", emit_events=False)
    assert committed.stdout.split() == ["brand_new.py"]

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_git_commands_never_carry_the_token(monkeypatch):
    """Commands are logged to SSE, the events table and sandbox.log verbatim."""
    from unittest.mock import AsyncMock
    from issueforge.core.sandbox import CommandResult

    monkeypatch.setattr(settings, "github_token", "ghp_secret_token_123")
    sandbox = NativeSandbox("test-git-token-log")
    calls = []

    async def record(command, **kwargs):
        calls.append((command, kwargs.get("env_vars") or {}))
        return CommandResult(0, "", "")

    monkeypatch.setattr(sandbox, "run_command", AsyncMock(side_effect=record))
    mgr = GitRepoManager(sandbox, "https://github.com/org/repo.git", working_branch="forge/t")
    await mgr.clone_repository()
    await mgr.get_remote_branches()
    await mgr.push_working_branch()

    assert calls
    assert all("ghp_secret" not in command for command, _ in calls)
    network = [env for command, env in calls if command.startswith(("git clone", "git push")) or "git fetch" in command]
    assert network and all(env.get("ISSUEFORGE_GIT_TOKEN") == "ghp_secret_token_123" for env in network)


@pytest.mark.asyncio
async def test_sandbox_commands_do_not_inherit_server_secrets(monkeypatch):
    monkeypatch.setenv("FORGE_AUTH_TOKEN", "dashboard-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "llm-secret")
    sandbox = NativeSandbox("test-env-scrub")
    sandbox.setup()
    res = await sandbox.run_command("env", emit_events=False, env_vars={"EXTRA": "kept"})
    assert "dashboard-secret" not in res.stdout
    assert "llm-secret" not in res.stdout
    assert "EXTRA=kept" in res.stdout
    assert "PATH=" in res.stdout
    sandbox.cleanup()
