import pytest
from issueforge.config import settings
from issueforge.core.sandbox import NativeSandbox
from issueforge.git.repo_manager import GitRepoManager


def test_auth_url_injection():
    sandbox = NativeSandbox("test-git-001")
    
    # Without tokens
    settings.github_token = None
    settings.gitlab_token = None
    mgr = GitRepoManager(sandbox, "https://github.com/org/repo.git")
    assert mgr._get_authenticated_url() == "https://github.com/org/repo.git"

    # With GitHub token
    settings.github_token = "ghp_secret_token_123"
    mgr = GitRepoManager(sandbox, "https://github.com/org/repo.git")
    assert "x-access-token:ghp_secret_token_123@github.com" in mgr._get_authenticated_url()

    # With GitLab token
    settings.gitlab_token = "glpat_secret_token_456"
    mgr_gl = GitRepoManager(sandbox, "https://gitlab.com/org/repo.git")
    assert "oauth2:glpat_secret_token_456@gitlab.com" in mgr_gl._get_authenticated_url()

    # Cleanup settings
    settings.github_token = None
    settings.gitlab_token = None


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
