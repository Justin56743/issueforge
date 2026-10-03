import logging
import re
import shlex
import shutil
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from issueforge.config import settings
from issueforge.core.models import FileDiff
from issueforge.core.sandbox import NativeSandbox

logger = logging.getLogger("issueforge.git.repo_manager")


# Pipeline artifacts the agents write into the workspace root (PLAN.md by the
# Planner, TEST_RESULTS.md by the Tester, REVIEW.md by the Reviewer). They are
# scratch handoff documents, not the change the operator asked for, so they must
# never reach `git add -A`. They go into `.git/info/exclude` rather than a filter
# at commit time: that path ignores them only while they are *untracked*, so a
# repository that genuinely versions its own PLAN.md still diffs and commits it.
# `.agents/` holds the skill files copied into every sandbox for the agy CLI.
AGENT_ARTIFACT_FILES = ("PLAN.md", "TEST_RESULTS.md", "REVIEW.md", ".agents/")
# Created anywhere in the tree when the Tester installs the project's dependencies.
DEPENDENCY_ARTIFACTS = ("node_modules/", "*.egg-info/")


class GitRepoManager:
    """Handles native Git operations inside the isolated sandbox."""

    def __init__(self, sandbox: NativeSandbox, repo_url: str, base_branch: str = "main", working_branch: str = "forge/task"):
        self.sandbox = sandbox
        self.raw_repo_url = repo_url
        self.base_branch = base_branch
        self.working_branch = working_branch

    def _get_authenticated_url(self) -> str:
        """Inject GitHub/GitLab tokens into HTTPS URLs if available."""
        url = self.raw_repo_url.strip()
        parsed = urlparse(url)

        if parsed.scheme in ("http", "https"):
            if "github.com" in parsed.netloc and settings.github_token:
                netloc = f"x-access-token:{settings.github_token}@{parsed.hostname}"
                if parsed.port:
                    netloc += f":{parsed.port}"
                return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
            elif "gitlab" in parsed.netloc and settings.gitlab_token:
                netloc = f"oauth2:{settings.gitlab_token}@{parsed.hostname}"
                if parsed.port:
                    netloc += f":{parsed.port}"
                return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

        return url

    async def clone_repository(self, depth: int = 50) -> bool:
        """Clone repository into the sandbox and configure git author."""
        self.sandbox.setup()
        auth_url = self._get_authenticated_url()

        # Check if already cloned
        if (self.sandbox.workspace_path / ".git").exists():
            self._exclude_agent_artifacts()
            res = await self.sandbox.run_command("git fetch origin")
            return res.success

        # If the directory has any leftover files but no .git, clean it up so git clone . succeeds
        if self.sandbox.workspace_path.exists():
            for item in self.sandbox.workspace_path.iterdir():
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)

        # Clone into current directory with --no-single-branch so all remote branches are tracked
        clone_cmd = f"git clone --depth {depth} --no-single-branch {shlex.quote(auth_url)} ."
        res = await self.sandbox.run_command(clone_cmd)
        if not res.success:
            return False

        # Set default git config for the sandbox repo
        await self.sandbox.run_command('git config user.name "issueforge"')
        await self.sandbox.run_command('git config user.email "issueforge-bot@local.dev"')
        await self.sandbox.run_command('git config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"')
        self._exclude_agent_artifacts()
        return True

    def _exclude_agent_artifacts(self) -> None:
        """Keep agent handoff documents out of the commit via `.git/info/exclude`.

        Local and untracked by design: the file is never pushed, and git ignores
        the entries for any artifact the repository already tracks.
        """
        exclude_file = self.sandbox.workspace_path / ".git" / "info" / "exclude"
        try:
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
            lines = {line.strip() for line in existing.splitlines()}
            wanted = [f"/{name}" for name in AGENT_ARTIFACT_FILES] + list(DEPENDENCY_ARTIFACTS)
            missing = [entry for entry in wanted if entry not in lines]
            if not missing:
                return
            block = "" if not existing or existing.endswith("\n") else "\n"
            block += "# Issueforge agent artifacts (never committed)\n" + "\n".join(missing) + "\n"
            exclude_file.write_text(existing + block, encoding="utf-8")
        except OSError as e:
            # Worst case the artifacts get committed; that must not abort the clone.
            logger.warning("Could not write .git/info/exclude: %s", e)

    async def create_working_branch(self) -> bool:
        """Checkout base branch and create working branch."""
        await self.sandbox.run_command(f"git checkout {shlex.quote(self.base_branch)}")
        res = await self.sandbox.run_command(f"git checkout -B {shlex.quote(self.working_branch)}")
        return res.success

    async def get_remote_branches(self) -> List[str]:
        """
        Fetch remote branches from origin and return clean branch names.
        Examples: ['main', 'develop', 'staging', 'feat/login']
        """
        await self.sandbox.run_command('git config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*" 2>/dev/null; git fetch --all --prune')
        res = await self.sandbox.run_command("git branch -r")
        branches: List[str] = []
        if res.success and res.stdout:
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or "->" in line:
                    continue
                # strip 'origin/' prefix
                if line.startswith("origin/"):
                    b_name = line.replace("origin/", "", 1).strip()
                    if b_name and b_name not in branches and not b_name.startswith("HEAD"):
                        branches.append(b_name)

        # Fallback if no remote branches discovered (e.g. empty/local repo)
        if not branches:
            branches = [self.base_branch or "main"]
        
        # Sort so common default branches come first
        priority_branches = ["main", "master", "develop", "dev", "staging"]
        sorted_branches = []
        for p in priority_branches:
            if p in branches:
                sorted_branches.append(p)
        for b in branches:
            if b not in sorted_branches:
                sorted_branches.append(b)

        return sorted_branches

    async def get_changed_file_paths(self) -> List[str]:
        """Workspace-relative paths of every modified, added or renamed file.

        Uses `git status --porcelain` rather than `git diff --name-only` so newly created
        untracked files are included — the Coder creates those routinely.
        """
        res = await self.sandbox.run_command("git status --porcelain", emit_events=False)
        paths: List[str] = []
        for line in res.stdout.splitlines():
            entry = line[3:].strip() if len(line) > 3 else ""
            if not entry:
                continue
            # Renames are reported as "old -> new"; the new path is what changed.
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1].strip()
            entry = entry.strip('"')
            if entry and entry not in paths:
                paths.append(entry)
        return paths

    async def get_diff(self) -> Tuple[str, str, List[FileDiff]]:
        """
        Get git diff against base branch or HEAD.
        Returns (diff_stat, full_diff_text, list_of_file_diffs).
        """
        # `git diff HEAD` ignores untracked files, so files the Coder created would be
        # missing from the diff the Reviewer and operator see. Intent-to-add puts them
        # in the index as empty entries without staging their content.
        await self.sandbox.run_command("git add -A --intent-to-add")

        # Diff stat
        stat_res = await self.sandbox.run_command("git diff --stat HEAD")
        diff_stat = stat_res.stdout.strip()

        # Full unified diff
        diff_res = await self.sandbox.run_command("git diff -U3 HEAD")
        diff_text = diff_res.stdout

        # Parse per-file diffs
        file_diffs: List[FileDiff] = []
        if diff_text:
            diff_chunks = re.split(r"^diff --git ", diff_text, flags=re.MULTILINE)
            for chunk in diff_chunks:
                if not chunk.strip():
                    continue
                header = chunk.split("\n")[0]
                # Match "a/path b/path"
                match = re.search(r"a/(.+?)\s+b/(.+)", header)
                file_path = match.group(2) if match else "unknown"
                is_new = "new file mode" in chunk
                is_del = "deleted file mode" in chunk
                file_diffs.append(
                    FileDiff(
                        file_path=file_path,
                        diff_text="diff --git " + chunk,
                        is_new=is_new,
                        is_deleted=is_del
                    )
                )

        return diff_stat, diff_text, file_diffs

    async def commit_changes(self, message: str) -> bool:
        """Stage all changes and commit."""
        await self.sandbox.run_command("git add -A")
        # Check if there is anything to commit
        status_res = await self.sandbox.run_command("git status --porcelain")
        if not status_res.stdout.strip():
            return True  # No changes to commit

        # Double quotes alone still expand $(...) and backticks; the message is LLM/issue text.
        commit_res = await self.sandbox.run_command(f"git commit -m {shlex.quote(message)}")
        return commit_res.success

    async def push_working_branch(self) -> bool:
        """Push the working branch to remote."""
        auth_url = self._get_authenticated_url()
        # Set origin URL with auth
        await self.sandbox.run_command(f"git remote set-url origin {shlex.quote(auth_url)}")
        # Lease, not --force: overwrite only our own earlier push of this branch, never
        # commits someone else pushed to it since the clone.
        res = await self.sandbox.run_command(f"git push -u origin {shlex.quote(self.working_branch)} --force-with-lease")
        return res.success

    async def abort_merge(self) -> bool:
        """Abort an in-progress merge."""
        res = await self.sandbox.run_command("git merge --abort")
        return res.success

