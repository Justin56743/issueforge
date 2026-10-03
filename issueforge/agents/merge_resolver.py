import logging
import re
import shlex
from pathlib import Path
from typing import List, Optional, Tuple

from issueforge.agents.agy_runner import AgySessionRunner
from issueforge.agents.llm import call_llm_with_fallback, setup_llm_api_keys
from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.models import AgentRole, EventType, Task
from issueforge.core.sandbox import NativeSandbox, bytecode_free_env
from issueforge.graph.blast_radius import BlastRadius, analyze_workspace

logger = logging.getLogger("issueforge.agents.merge_resolver")

CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")

MERGE_RESOLVER_SYSTEM_PROMPT = """You are a Senior Systems Integration Engineer resolving a 3-way Git merge conflict.

Rules:
1. `<<<<<<< HEAD` is OUR working branch; `>>>>>>>` is the INCOMING target branch.
2. Intelligently reconcile BOTH sides. Preserve new functions, imports and bugfixes from
   each branch. Only drop one side when the two are genuinely mutually exclusive.
3. Remove ALL conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`, `|||||||`).
4. Change nothing outside the conflicted regions.
5. Return ONLY the complete, clean file contents. No explanation, no markdown fences.
"""


class MergeConflictResolver:
    """Resolves merge conflicts against a target branch inside a task's sandbox.

    Runs every git command through `NativeSandbox.run_command`, so the whole merge is
    captured in the run's audit log and streams to the dashboard like any other stage —
    and so it is killed with the process group when the operator cancels.

    Safety contract: the workspace is never left mid-merge. Every failure path aborts.
    """

    def __init__(self, sandbox: NativeSandbox, task: Optional[Task] = None):
        self.sandbox = sandbox
        self.task = task
        self.model = settings.reviewer_model
        self.resolved_files: List[str] = []
        self._target_branch: str = ""
        # Set when the probe could not tell whether the merge is clean.
        self.probe_error: Optional[str] = None

    # ------------------------------------------------------------------ helpers

    @property
    def _task_id(self) -> Optional[str]:
        return self.task.id if self.task else None

    @property
    def _run_id(self) -> Optional[str]:
        return self.task.active_run_id if self.task else None

    async def _emit(self, message: str, event_type: EventType = EventType.STEP) -> None:
        if not self._task_id:
            return
        try:
            await event_bus.emit_log(
                task_id=self._task_id,
                message=message,
                role=AgentRole.REVIEWER.value,
                event_type=event_type,
                run_id=self._run_id,
            )
        except Exception:
            pass

    async def _git(self, command: str, timeout: int = 120):
        return await self.sandbox.run_command(command, timeout=timeout, emit_events=False)

    async def abort_merge(self) -> None:
        """Return the workspace to a clean pre-merge state. Safe to call unconditionally."""
        await self._git("git merge --abort")

    # ------------------------------------------------------------------ probing

    async def probe_for_conflicts(self, target_branch: str) -> bool:
        """Dry-run merge the target branch. Returns True if conflicts were found.

        On a clean merge the trial is aborted, leaving the branch exactly as the Coder
        and Tester left it — integrating target changes is a separate decision, not a
        side effect of checking.

        When conflicts ARE found the workspace is deliberately left mid-merge so the
        conflicted files can be read; callers must finish via `execute_resolution_pipeline`
        or `abort_merge`.
        """
        self.probe_error = None
        fetch = await self._git(f"git fetch origin {shlex.quote(target_branch)}")
        if not fetch.success:
            # Merging a stale origin/<target> would "pass" against code that is not
            # what the PR will actually merge into.
            self.probe_error = f"could not fetch `{target_branch}`: {(fetch.stderr or fetch.stdout).strip()[:300]}"
            return False
        result = await self._git(f"git merge --no-commit --no-ff {shlex.quote('origin/' + target_branch)}")
        combined = f"{result.stdout}\n{result.stderr}"

        if "CONFLICT" in combined:
            logger.warning("Merge conflict detected against origin/%s", target_branch)
            return True

        if not result.success:
            # Refused for some other reason (unrelated histories, missing ref, dirty tree).
            # Not a conflict we can resolve, and not proof of a clean merge either.
            logger.info("Merge probe against origin/%s did not apply: %s", target_branch, combined.strip()[:300])
            self.probe_error = combined.strip()[:300]
            await self.abort_merge()
            return False

        await self.abort_merge()
        return False

    async def list_conflicted_files(self) -> List[Path]:
        result = await self._git("git diff --name-only --diff-filter=U")
        return [
            self.sandbox.workspace_path / line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]

    # ------------------------------------------------------------------ resolution

    @staticmethod
    def strip_code_fences(text: str) -> str:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9_+-]*\n", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def has_conflict_markers(text: str) -> bool:
        return any(
            line.startswith(marker)
            for line in (text or "").splitlines()
            for marker in CONFLICT_MARKERS
        )

    async def resolve_single_file(self, file_path: Path) -> bool:
        """Reconcile one conflicted file via the LLM and stage it."""
        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Binary conflicts cannot be reconciled textually.
            logger.warning("Cannot read conflicted file %s: %s", file_path, exc)
            return False

        if not self.has_conflict_markers(raw_text):
            return True

        rel_path = file_path.relative_to(self.sandbox.workspace_path).as_posix()
        await self._emit(f"🔀 Reconciling merge conflict in `{rel_path}`...")
        resolved = self.strip_code_fences(await self._request_resolution(rel_path, raw_text) or "")

        if not resolved:
            logger.warning("Resolver returned empty content for %s", rel_path)
            return False
        if self.has_conflict_markers(resolved):
            # Refusing this is the point of the gate: staging a file with markers in it
            # would commit a syntactically broken file.
            logger.warning("Resolver left conflict markers in %s", rel_path)
            return False

        file_path.write_text(resolved, encoding="utf-8")
        stage = await self._git(f"git add -- {shlex.quote(rel_path)}")
        if not stage.success:
            logger.warning("Failed to stage resolved file %s: %s", rel_path, stage.stderr)
            return False

        self.resolved_files.append(rel_path)
        return True

    async def _request_resolution(self, rel_path: str, raw_text: str) -> Optional[str]:
        """Ask for a reconciliation: local `agy` session first, hosted API as fallback.

        Follows the project-wide agy-first policy — the local CLI keeps the work and the
        source on the Jetson, and the API is only reached when agy is unavailable or
        returns nothing.
        """
        setup_llm_api_keys()
        user_prompt = (
            f"Target branch: {self._target_branch}\n"
            f"Working branch: {self.task.working_branch if self.task else 'unknown'}\n"
            f"File: {rel_path}\n\nContent with conflict markers:\n\n{raw_text}"
        )

        try:
            runner = AgySessionRunner(
                task_id=self._task_id or "merge-resolver",
                run_id=self._run_id or self.sandbox.run_id,
                sandbox_dir=self.sandbox.workspace_path,
                role=AgentRole.REVIEWER,
            )
            success, text = await runner.run_prompt(
                system_instructions=MERGE_RESOLVER_SYSTEM_PROMPT,
                task_prompt=user_prompt,
                effort="low",
                timeout_seconds=120,
            )
            if success and text and text.strip():
                return text.strip()
        except Exception as exc:
            logger.debug("agy merge resolution unavailable, falling back to API: %s", exc)

        try:
            response = await call_llm_with_fallback(
                primary_model=self.model,
                messages=[
                    {"role": "system", "content": MERGE_RESOLVER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                task_id=self._task_id,
                role=AgentRole.REVIEWER.value,
                run_id=self._run_id,
                temperature=0.1,
                max_tokens=8000,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.error("Merge resolution fallback failed for %s: %s", rel_path, exc)
            return None

    # ------------------------------------------------------------------ verification

    async def verify_resolution_integrity(self) -> Tuple[bool, str, BlastRadius]:
        """Syntax-check the merged tree and report the blast radius of the merge.

        A conflict resolution is an LLM edit to code nobody reviewed, so it gets the same
        compile gate the Tester applies. Blast radius is advisory context for the operator.
        """
        radius = analyze_workspace(self.sandbox.workspace_path, self.resolved_files)

        has_python = any(f.endswith(".py") for f in self.resolved_files)
        if not has_python:
            return True, "No Python files in the merge; syntax check skipped.", radius

        syntax = await self.sandbox.run_command(
            "python3 -m compileall -q .",
            timeout=120,
            env_vars=bytecode_free_env(self.sandbox),
            emit_events=False,
        )
        if not syntax.success:
            detail = (syntax.stderr or syntax.stdout).strip()[-800:]
            return False, f"Syntax verification failed after conflict resolution:\n{detail}", radius
        return True, "Syntax verification passed.", radius

    # ------------------------------------------------------------------ orchestration

    async def execute_resolution_pipeline(self, target_branch: str) -> Tuple[bool, str]:
        """Probe, resolve, verify, and finalize the merge commit.

        Returns (ok, message). `ok` is True when the branch is safe to push — either it
        merged cleanly or every conflict was reconciled and verified. Any failure aborts
        the merge, so the caller's branch is always left in its pre-merge state. When
        `resolved_files` is non-empty the branch now holds LLM-written code, and the caller
        must get it re-confirmed before pushing.
        """
        self.resolved_files = []
        self._target_branch = target_branch

        if not await self.probe_for_conflicts(target_branch):
            if self.probe_error:
                # Fail closed: an unverified merge is not a clean one.
                return False, f"Merge check against `{target_branch}` could not run: {self.probe_error}"
            return True, f"Pre-flight merge check clean against `{target_branch}`."

        conflicted = await self.list_conflicted_files()
        if not conflicted:
            await self.abort_merge()
            return True, f"Conflict reported against `{target_branch}` but no unmerged files found."

        await self._emit(
            f"🔀 {len(conflicted)} conflicted file(s) against `{target_branch}`. Reconciling...",
            event_type=EventType.STEP,
        )

        for path in conflicted:
            try:
                ok = await self.resolve_single_file(path)
            except Exception as exc:
                logger.error("Conflict resolution failed for %s: %s", path, exc)
                ok = False
            if not ok:
                await self.abort_merge()
                rel = path.name
                return False, f"Could not automatically resolve conflict in `{rel}`. Merge aborted; resolve by hand."

        verified, detail, radius = await self.verify_resolution_integrity()
        if not verified:
            await self.abort_merge()
            return False, detail

        commit = await self._git(
            "git commit -m "
            + shlex.quote(f"chore(merge): resolve conflicts with {target_branch} via Issueforge AST Resolver")
        )
        if not commit.success:
            await self.abort_merge()
            return False, f"Merge commit failed after resolution: {(commit.stderr or commit.stdout).strip()[:300]}"

        summary = (
            f"Resolved {len(self.resolved_files)} conflicted file(s) against `{target_branch}`. "
            f"{detail} {radius.summary()}"
        )
        await self._emit(f"✅ {summary}")
        return True, summary
