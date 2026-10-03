import asyncio
import logging
import shlex
from datetime import datetime
from typing import Optional

from issueforge.agents.orchestrator import SupervisoryOrchestrator
from issueforge.config import settings
from issueforge.core.database import get_task, save_event, save_task
from issueforge.core.events import event_bus
from issueforge.core.models import EventType, PipelineRun, PlatformType, Task, TaskEvent, TaskStatus, TaskSubtask, utc_now
from issueforge.core.sandbox import NativeSandbox
from issueforge.core.caching import ArtifactCacheManager
from issueforge.core.task_dossier import TaskDossierManager
from issueforge.git.github_client import GitHubClient
from issueforge.git.gitlab_client import GitLabClient
from issueforge.agents.merge_resolver import MergeConflictResolver
from issueforge.git.repo_manager import GitRepoManager
from issueforge.graph.blast_radius import analyze_workspace
from issueforge.vault.canvas_builder import CanvasStage
from issueforge.vault.canvas_telemetry import mark as canvas_mark
from issueforge.vault.canvas_telemetry import stage_for_status
from issueforge.vault.canvas_watcher import canvas_watchers

logger = logging.getLogger("issueforge.agents.engine")

# Run statuses that mean "waiting for the operator", not "finished". A pipeline launched
# while the latest run is in one of these resumes that run.
PAUSED_RUN_STATUSES = (TaskStatus.AWAITING_BRANCH_SELECTION, TaskStatus.AWAITING_INPUT)


class IssueforgeAgentEngine:
    """Orchestrates the multi-LLM autonomous agent pipeline under the Supervisory Orchestrator."""

    def __init__(self):
        self.github_client = GitHubClient()
        self.gitlab_client = GitLabClient()

    async def fetch_issue_context(self, task: Task) -> None:
        """Fetch existing comments, participants, and discussion context from GitHub or GitLab."""
        try:
            if not task.collaborators.author:
                task.collaborators.author = task.sender or "operator"

            if task.platform == PlatformType.GITHUB and task.issue_number:
                owner, repo = self.github_client.parse_repo_owner_and_name(task.repo_name)
                comments = await self.github_client.list_issue_comments(owner, repo, task.issue_number)
                if comments:
                    participants = set(task.collaborators.participants)
                    lines = []
                    for c in comments[-10:]:
                        login = c.get("user", {}).get("login")
                        if login:
                            participants.add(f"@{login}")
                        lines.append(f"- @{login}: {c.get('body')}")
                    task.comments_context = "\n".join(lines)
                    task.collaborators.participants = sorted(list(participants))
            elif task.platform == PlatformType.GITLAB and task.issue_number:
                project_path = self.gitlab_client.parse_project_path(task.repo_url)
                notes = await self.gitlab_client.list_issue_notes(project_path, task.issue_number)
                if notes:
                    user_notes = [n for n in notes if not n.get("system")]
                    participants = set(task.collaborators.participants)
                    lines = []
                    for n in user_notes[-10:]:
                        uname = n.get("author", {}).get("username")
                        if uname:
                            participants.add(f"@{uname}")
                        lines.append(f"- @{uname}: {n.get('body')}")
                    task.comments_context = "\n".join(lines)
                    task.collaborators.participants = sorted(list(participants))
        except Exception as e:
            await event_bus.emit_log(
                task_id=task.id,
                message=f"Notice: Could not fetch discussion comments ({str(e)})",
                event_type=EventType.LOG
            )

    async def post_start_comment(self, task: Task) -> None:
        """Post an automated notification on Git issue when engineering starts."""
        msg = f"🛠️ **Issueforge Autonomous Agent** started working on this task inside an isolated native sandbox on NVIDIA Jetson Orin.\nWorking branch: `{task.working_branch}`"
        try:
            if task.platform == PlatformType.GITHUB and task.issue_number:
                owner, repo = self.github_client.parse_repo_owner_and_name(task.repo_name)
                await self.github_client.post_issue_comment(owner, repo, task.issue_number, msg)
            elif task.platform == PlatformType.GITLAB and task.issue_number:
                project_path = self.gitlab_client.parse_project_path(task.repo_url)
                await self.gitlab_client.post_issue_note(project_path, task.issue_number, msg)
        except Exception:
            pass

    async def post_completion_comment(self, task: Task) -> None:
        """Post a summary comment on Git issue when work is pushed/completed."""
        msg = (
            f"🎉 **Issueforge Orchestrator Completed Implementation!**\n\n"
            f"**PR/Branch URL:** {task.pr_url}\n"
            f"**Verification:** {task.test_summary or 'Verified'}\n\n"
            f"**Summary:**\n{task.review_summary.summary if task.review_summary else 'Changes applied.'}"
        )
        try:
            if task.platform == PlatformType.GITHUB and task.issue_number:
                owner, repo = self.github_client.parse_repo_owner_and_name(task.repo_name)
                await self.github_client.post_issue_comment(owner, repo, task.issue_number, msg)
            elif task.platform == PlatformType.GITLAB and task.issue_number:
                project_path = self.gitlab_client.parse_project_path(task.repo_url)
                await self.gitlab_client.post_issue_note(project_path, task.issue_number, msg)
        except Exception:
            pass

    async def execute_task_pipeline(self, task_id: str, max_test_retries: int = 3) -> Task:
        """Run the Supervisory Orchestrator -> Planner -> Coder -> Tester (repair) -> Reviewer pipeline."""
        task = await get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found.")

        last_run = task.runs[-1] if task.runs else None
        if last_run and last_run.status in PAUSED_RUN_STATUSES:
            # Resuming from a HITL pause (branch gate or question): continue in the same
            # run and workspace instead of recording a phantom attempt and cloning again.
            current_run = last_run
            current_run.status = TaskStatus.PLANNING
            attempt_number = current_run.attempt_number
            run_id = current_run.run_id
        else:
            # Allocate run number and isolate in a dedicated sandbox attempt folder
            attempt_number = len(task.runs) + 1
            run_id = f"run-{attempt_number}"
            current_run = PipelineRun(
                run_id=run_id,
                attempt_number=attempt_number,
                status=TaskStatus.PLANNING,
                started_at=utc_now(),
                sandbox_dir=f"sandboxes/{run_id}"
            )
            task.runs.append(current_run)
        task.active_run_id = run_id
        await save_task(task)
        await TaskDossierManager.sync_dossier_async(task)
        await canvas_mark(task, CanvasStage.ISSUE, "INGESTED", f"Run #{attempt_number}")

        sandbox = NativeSandbox(task.id, run_id=run_id)
        git_mgr = GitRepoManager(
            sandbox=sandbox,
            repo_url=task.repo_url,
            base_branch=task.base_branch,
            working_branch=task.working_branch,
        )

        # Reverse steering: operator notes dropped on task_dag.canvas are queued onto the
        # task. Torn down in the `finally` below so no watcher outlives its run.
        await canvas_watchers.start(task.id, run_id=run_id)

        try:
            # 1. Clone & prepare workspace
            await event_bus.emit_log(
                task_id=task.id,
                message=f"🚀 Initializing native sandbox on Jetson Orin (Run #{attempt_number}) at: `{sandbox.workspace_path}`",
                event_type=EventType.STEP,
                run_id=current_run.run_id
            )
            await event_bus.emit_log(
                task_id=task.id,
                message="📥 Cloning repository into sandbox workspace...",
                event_type=EventType.STEP,
                run_id=current_run.run_id
            )
            cloned = await git_mgr.clone_repository()
            if not cloned:
                current_run.failure_stage = "GIT"
                raise RuntimeError("Failed to clone git repository into sandbox.")

            # 2. Discover remote branches dynamically
            await canvas_mark(task, CanvasStage.GATE, "DISCOVERING", "Probing remote branches")
            remote_branches = await git_mgr.get_remote_branches()
            try:
                if task.platform == PlatformType.GITLAB:
                    project_path = self.gitlab_client.parse_project_path(task.repo_url)
                    api_branches = await self.gitlab_client.get_project_branches(project_path)
                    for ab in api_branches:
                        if ab not in remote_branches:
                            remote_branches.append(ab)
                elif task.platform == PlatformType.GITHUB:
                    owner, repo = self.github_client.parse_repo_owner_and_name(task.repo_name)
                    api_branches = await self.github_client.get_repo_branches(owner, repo)
                    for ab in api_branches:
                        if ab not in remote_branches:
                            remote_branches.append(ab)
            except Exception as bex:
                logger.warning(f"Failed to query remote API branches: {bex}")

            if task.selected_target_branch and task.selected_target_branch not in remote_branches:
                remote_branches.append(task.selected_target_branch)

            task.target_branch_candidates = remote_branches
            if not task.selected_target_branch:
                task.selected_target_branch = task.base_branch if task.base_branch in remote_branches else (remote_branches[0] if remote_branches else "main")
            await save_task(task)

            # HITL target branch gate. Candidates are only discoverable after the clone,
            # so this is the first point a meaningful question can be asked. A branch that
            # was merely defaulted from base_branch at creation is not an operator choice,
            # which is what target_branch_confirmed distinguishes.
            if not task.target_branch_confirmed:
                task.status = TaskStatus.AWAITING_BRANCH_SELECTION
                current_run.status = TaskStatus.AWAITING_BRANCH_SELECTION
                await save_task(task)
                await TaskDossierManager.sync_dossier_async(task)
                await canvas_mark(
                    task, CanvasStage.GATE, "AWAITING_INPUT",
                    f"Select a target branch ({len(remote_branches)} candidates, suggested: {task.selected_target_branch})",
                )
                await event_bus.emit_log(
                    task_id=task.id,
                    message=(
                        f"Status changed to AWAITING_BRANCH_SELECTION - choose a target branch "
                        f"(suggested `{task.selected_target_branch}`) on the dashboard or in Telegram."
                    ),
                    event_type=EventType.STATUS_CHANGE,
                    run_id=current_run.run_id,
                )
                from issueforge.bot.telegram_bot import telegram_manager
                await telegram_manager.send_branch_selection(task)
                return task
            # Auto-resolved, not an operator pause — the interactive gate is at confirmation.
            await canvas_mark(
                task, CanvasStage.GATE, "RESOLVED",
                f"Target: {task.selected_target_branch} ({len(remote_branches)} candidates)",
            )
            await TaskDossierManager.sync_dossier_async(task)

            # 3. Fetch discussion comments & post start note
            await self.fetch_issue_context(task)
            await self.post_start_comment(task)

            await event_bus.emit_log(
                task_id=task.id,
                message=f"🌿 Creating working branch `{task.working_branch}` (Target: `{task.selected_target_branch}`)...",
                event_type=EventType.STEP,
                run_id=current_run.run_id
            )
            await git_mgr.create_working_branch()

            # 4. Supervisory Orchestrator Triage & Clarification Check
            orchestrator = SupervisoryOrchestrator(sandbox=sandbox, task=task)
            needs_clarification, question = await orchestrator.triage_task()

            if needs_clarification and question:
                task.status = TaskStatus.AWAITING_INPUT
                current_run.status = TaskStatus.AWAITING_INPUT
                await save_task(task)
                await TaskDossierManager.sync_dossier_async(task)
                await canvas_mark(task, CanvasStage.GATE, "AWAITING_INPUT", question.question)
                await event_bus.emit_log(
                    task_id=task.id,
                    message="Status changed to AWAITING_INPUT - Waiting for user answer on Web Dashboard or Telegram.",
                    event_type=EventType.STATUS_CHANGE,
                    run_id=current_run.run_id
                )
                from issueforge.bot.telegram_bot import telegram_manager
                await telegram_manager.send_question_alert(task, question)
                return task

            # 5. Retrieve Harness Learned Skills & Patterns + Previous Run Context
            harness_memory = await orchestrator.retrieve_harness_learnings()

            # Inject previous failure context if this is a retry attempt
            failed_runs = [r for r in task.runs[:-1] if r.status == TaskStatus.FAILED]
            if failed_runs:
                last_failed = failed_runs[-1]
                harness_memory += (
                    f"\n\n[Previous Run #{last_failed.attempt_number} Failure Context]:\n"
                    f"- Failure Stage: {last_failed.failure_stage}\n"
                    f"- Error Message: {last_failed.error_message}\n"
                    f"- Test Results: {last_failed.test_summary}\n"
                    f"Action Required: Directly fix and avoid the above errors in this run."
                )

            # 6. Planning Phase
            task.status = TaskStatus.PLANNING
            current_run.status = TaskStatus.PLANNING
            await save_task(task)
            TaskDossierManager.sync_dossier(task)
            await event_bus.emit_log(
                task_id=task.id,
                message="Status changed to PLANNING",
                event_type=EventType.STATUS_CHANGE,
                run_id=current_run.run_id
            )
            await canvas_mark(task, CanvasStage.PLANNER, "RUNNING", "Generating implementation plan")

            # Reuse a plan the previous attempt already proved viable. Applied here, after
            # the clone — clone_repository wipes the workspace, so an earlier write is lost.
            cached_plan = ArtifactCacheManager.apply_plan_cache(
                task, current_run.run_id, sandbox.workspace_path
            )
            if cached_plan:
                source_run = ArtifactCacheManager.describe_source(task, current_run.run_id)
                plan = cached_plan
                await event_bus.emit_log(
                    task_id=task.id,
                    message=f"♻️ Reusing the verified plan from `{source_run}` — skipping re-planning.",
                    event_type=EventType.STEP,
                    run_id=current_run.run_id,
                )
                await canvas_mark(task, CanvasStage.PLANNER, "DONE", f"Plan reused from {source_run}")
            else:
                plan = await orchestrator.run_planner(memory_context=harness_memory)
            task.plan = plan

            # Extract subtasks checklist from plan
            parsed_subtasks = []
            for idx, line in enumerate(plan.splitlines()):
                line_clean = line.strip()
                if line_clean.startswith("- [ ]") or line_clean.startswith("- [x]") or any(line_clean.startswith(f"{i}.") for i in range(1, 10)):
                    title = line_clean.lstrip("- [ ]x0123456789. ")
                    if title and len(title) > 3:
                        parsed_subtasks.append(TaskSubtask(id=f"st-{idx+1}", title=title, completed=False))
            if parsed_subtasks:
                task.subtasks = parsed_subtasks

            await save_task(task)
            await TaskDossierManager.sync_dossier_async(task)
            await canvas_mark(
                task, CanvasStage.PLANNER, "DONE", f"{len(task.subtasks)} subtasks planned"
            )

            # 7. Coding Phase (with Differential Code Repair Context)
            task.status = TaskStatus.CODING
            current_run.status = TaskStatus.CODING
            await save_task(task)
            TaskDossierManager.sync_dossier(task)
            await event_bus.emit_log(
                task_id=task.id,
                message="Status changed to CODING",
                event_type=EventType.STATUS_CHANGE,
                run_id=current_run.run_id
            )
            await canvas_mark(task, CanvasStage.CODER, "RUNNING", "Applying file modifications")

            test_repair_context = None
            if attempt_number > 1 and failed_runs:
                last_failed = failed_runs[-1]
                if last_failed.failure_stage == "TESTER" and (last_failed.error_message or last_failed.test_summary):
                    test_repair_context = f"Previous Run #{last_failed.attempt_number} Test Failure:\n{last_failed.error_message or ''}\n{last_failed.test_summary or ''}\nFix these specific test regressions."

            modified_files = await orchestrator.run_coder(plan=plan, test_feedback=test_repair_context)

            # Check if Coder actually applied any file modifications
            diff_stat, diff_content, _ = await git_mgr.get_diff()
            if not modified_files and not diff_content.strip():
                current_run.failure_stage = "CODER"
                await canvas_mark(task, CanvasStage.CODER, "FAILED", "No code changes produced")
                raise RuntimeError("Coder Agent failed to produce or apply any code changes. Stopping pipeline.")
            await canvas_mark(
                task, CanvasStage.CODER, "DONE",
                diff_stat or f"{len(modified_files)} file(s) modified",
            )

            # 8. Testing & Iterative Repair Phase
            task.status = TaskStatus.TESTING

            current_run.status = TaskStatus.TESTING
            await save_task(task)
            TaskDossierManager.sync_dossier(task)
            await event_bus.emit_log(
                task_id=task.id,
                message="Status changed to TESTING",
                event_type=EventType.STATUS_CHANGE,
                run_id=current_run.run_id
            )
            await canvas_mark(task, CanvasStage.TESTER, "RUNNING", "Detecting and running test suite")

            test_result = await orchestrator.run_tester()

            retry_count = 0
            while not test_result.passed and retry_count < max_test_retries:
                retry_count += 1
                await event_bus.emit_log(
                    task_id=task.id,
                    message=f"🔁 Test suite failed. Triggering Coder repair iteration ({retry_count}/{max_test_retries})...",
                    event_type=EventType.STEP,
                    run_id=current_run.run_id
                )
                await canvas_mark(
                    task, CanvasStage.TESTER, "REPAIR", f"Attempt {retry_count}/{max_test_retries}"
                )
                await canvas_mark(
                    task, CanvasStage.CODER, "REPAIR", f"Repairing failing tests ({retry_count})"
                )
                await orchestrator.run_coder(plan=plan, test_feedback=test_result.failure_details or test_result.stderr)
                test_result = await orchestrator.run_tester()

            if test_result.total_tests == 0 and test_result.passed:
                task.test_summary = f"Tests: Passed (0/0 - verified syntax) using `{test_result.test_runner}`"
            else:
                task.test_summary = (
                    f"Tests: {'Passed' if test_result.passed else 'Failed'} "
                    f"({test_result.passed_tests}/{test_result.total_tests}) using `{test_result.test_runner}`"
                )
            current_run.test_summary = task.test_summary
            await canvas_mark(
                task,
                CanvasStage.TESTER,
                "PASSED" if test_result.passed else "FAILED",
                task.test_summary,
            )

            # Strict guard: If tests did not pass after repair iterations, halt immediately!
            if not test_result.passed:
                task.status = TaskStatus.FAILED
                task.error_message = (
                    f"Automated test verification failed after {max_test_retries} repair iterations "
                    f"({test_result.failed_tests}/{test_result.total_tests or 'all'} tests failed). Stopping pipeline."
                )
                current_run.status = TaskStatus.FAILED
                current_run.failure_stage = "TESTER"
                current_run.error_message = task.error_message
                current_run.completed_at = utc_now()
                if current_run.started_at:
                    current_run.duration_seconds = (current_run.completed_at - current_run.started_at).total_seconds()

                await save_task(task)
                TaskDossierManager.sync_dossier(task)
                NativeSandbox.terminate_task_process(task.id)
                await event_bus.emit_log(
                    task_id=task.id,
                    message=f"🛑 {task.error_message}",
                    event_type=EventType.ERROR,
                    run_id=current_run.run_id
                )
                await event_bus.emit_log(
                    task_id=task.id,
                    message="Status changed to FAILED",
                    event_type=EventType.STATUS_CHANGE,
                    run_id=current_run.run_id
                )
                await canvas_mark(task, CanvasStage.TESTER, "FAILED", task.error_message)
                from issueforge.bot.telegram_bot import telegram_manager
                await telegram_manager.send_task_failure(task)
                return task

            # 8b. Blast radius (advisory only — the full suite already ran above).
            # Surfaced to the operator and the Reviewer; never used to narrow test scope,
            # because a missed import edge would silently skip a real regression test.
            blast_radius = None
            try:
                changed = await git_mgr.get_changed_file_paths()
                blast_radius = analyze_workspace(
                    sandbox.workspace_path, changed, repo_name=task.repo_name
                )
                await canvas_mark(task, CanvasStage.TESTER, "PASSED", f"{task.test_summary} | {blast_radius.summary()}")
                await event_bus.emit_log(
                    task_id=task.id,
                    message=f"🕸️ {blast_radius.summary()}",
                    event_type=EventType.STEP,
                    run_id=current_run.run_id,
                    data={"kind": "blast_radius", **blast_radius.to_dict()},
                )
            except Exception as bex:
                logger.warning("Blast radius analysis skipped: %s", bex)

            # 9. Extract Git Diff
            diff_stat, diff_content, _ = await git_mgr.get_diff()
            if not diff_content.strip():
                current_run.failure_stage = "GIT"
                raise RuntimeError("No git modifications found in workspace after coding. Stopping pipeline.")

            task.diff_stat = diff_stat
            task.diff_content = diff_content
            current_run.diff_stat = diff_stat

            # 10. Review & Summarization Phase
            await event_bus.emit_log(
                task_id=task.id,
                message="🔍 Reviewer Agent evaluating code diff and security impact...",
                event_type=EventType.STEP,
                run_id=current_run.run_id
            )
            await canvas_mark(task, CanvasStage.REVIEWER, "RUNNING", "Assessing diff and risk")
            review_summary = await orchestrator.run_reviewer(
                diff_stat=diff_stat,
                diff_content=diff_content,
                test_result=test_result,
                blast_radius=blast_radius,
            )
            task.review_summary = review_summary

            # 11. Distill Skill into Harness Memory
            await orchestrator.record_task_learning(plan=plan, diff_stat=diff_stat, test_result=test_result)

            # Mark subtasks as completed
            for st in task.subtasks:
                st.completed = True

            # 12. Transition to Awaiting Confirmation & Branch Selection
            task.status = TaskStatus.AWAITING_CONFIRMATION
            current_run.status = TaskStatus.AWAITING_CONFIRMATION
            current_run.completed_at = utc_now()
            if current_run.started_at:
                current_run.duration_seconds = (current_run.completed_at - current_run.started_at).total_seconds()

            await save_task(task)
            await TaskDossierManager.sync_dossier_async(task)
            await canvas_mark(
                task, CanvasStage.REVIEWER, "AWAITING_CONFIRMATION",
                f"Risk: {review_summary.risk_assessment} | Target: {task.selected_target_branch}",
            )
            await event_bus.emit_log(
                task_id=task.id,
                message=f"Status changed to AWAITING_CONFIRMATION - Target branch `{task.selected_target_branch}` selected. Ready for review & confirmation.",
                event_type=EventType.STATUS_CHANGE,
                run_id=current_run.run_id
            )

            # Notify Telegram Bot if enabled
            from issueforge.bot.telegram_bot import telegram_manager
            await telegram_manager.send_review_confirmation(task)

            return task

        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            task.error_message = "Task execution was stopped by user."
            task.completed_at = utc_now()
            current_run.status = TaskStatus.CANCELLED
            current_run.completed_at = utc_now()
            if current_run.started_at:
                current_run.duration_seconds = (current_run.completed_at - current_run.started_at).total_seconds()
            await save_task(task)
            await TaskDossierManager.sync_dossier_async(task)
            await canvas_mark(
                task, stage_for_status(current_run.status), "CANCELLED", "Stopped by operator"
            )
            NativeSandbox.terminate_task_process(task.id)
            await event_bus.emit_log(
                task_id=task.id,
                message="🛑 Task pipeline cancelled by user.",
                event_type=EventType.STATUS_CHANGE,
                run_id=current_run.run_id
            )
            raise
        except Exception as e:
            prev_stage = task.status.value if task.status else "UNKNOWN"
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            current_run.status = TaskStatus.FAILED
            if not current_run.failure_stage:
                current_run.failure_stage = prev_stage
            current_run.error_message = str(e)
            current_run.completed_at = utc_now()
            if current_run.started_at:
                current_run.duration_seconds = (current_run.completed_at - current_run.started_at).total_seconds()
            await save_task(task)
            await TaskDossierManager.sync_dossier_async(task)
            await canvas_mark(
                task, stage_for_status(TaskStatus(prev_stage) if prev_stage in TaskStatus.__members__ else TaskStatus.PENDING_APPROVAL),
                "ERROR", str(e),
            )
            NativeSandbox.terminate_task_process(task.id)
            await event_bus.emit_log(
                task_id=task.id,
                message=f"🛑 Pipeline halted: {str(e)}",
                event_type=EventType.ERROR,
                run_id=current_run.run_id
            )
            await event_bus.emit_log(
                task_id=task.id,
                message=f"Status changed to FAILED - Pipeline halted: {str(e)}",
                event_type=EventType.STATUS_CHANGE,
                run_id=current_run.run_id
            )
            from issueforge.bot.telegram_bot import telegram_manager
            await telegram_manager.send_task_failure(task)
            return task
        finally:
            # Runs on every path out of the block above, including the CancelledError re-raise.
            await canvas_watchers.stop(task.id)


    async def _fail_push(self, task: Task, message: str, stage: str) -> Task:
        """Record a failed push on the task and its run, and tell the operator."""
        task.status = TaskStatus.FAILED
        task.error_message = message
        for r in task.runs:
            if r.run_id == task.active_run_id:
                r.status = TaskStatus.FAILED
                r.failure_stage = stage
                r.error_message = message
                r.completed_at = utc_now()
                break
        await save_task(task)
        await TaskDossierManager.sync_dossier_async(task)
        await canvas_mark(task, CanvasStage.MERGE, "FAILED", message)
        await event_bus.emit_log(
            task_id=task.id,
            message=f"🛑 {message}",
            event_type=EventType.ERROR,
            run_id=task.active_run_id,
        )
        from issueforge.bot.telegram_bot import telegram_manager
        await telegram_manager.send_task_failure(task)
        return task

    async def push_and_create_pr(self, task_id: str, target_branch: Optional[str] = None) -> Task:
        """Commit, push branch to remote, and open PR / MR targeted against selected branch."""
        task = await get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found.")

        if target_branch:
            task.selected_target_branch = target_branch

        effective_target = task.selected_target_branch or task.base_branch or "main"

        task.status = TaskStatus.PUSHING
        await save_task(task)
        TaskDossierManager.sync_dossier(task)
        await canvas_mark(task, CanvasStage.REVIEWER, "PUSHING", f"Target: {effective_target}")
        await event_bus.emit_log(
            task_id=task.id,
            message=f"🚀 Pushing changes and creating PR targeting `{effective_target}`...",
            event_type=EventType.STATUS_CHANGE,
            run_id=task.active_run_id
        )
        try:
            return await self._push(task, effective_target)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Without this a crash mid-push left the task in PUSHING forever.
            logger.exception("Push failed for %s", task.id)
            return await self._fail_push(task, f"Push failed: {e}", "PUSHING")

    async def _push(self, task: Task, effective_target: str) -> Task:
        sandbox = NativeSandbox(task.id, run_id=task.active_run_id)
        git_mgr = GitRepoManager(
            sandbox=sandbox,
            repo_url=task.repo_url,
            base_branch=effective_target,
            working_branch=task.working_branch,
        )

        commit_msg = (
            task.review_summary.suggested_commit_message
            if task.review_summary
            else f"feat: implement {task.title}"
        )
        pr_title = (
            task.review_summary.suggested_pr_title
            if task.review_summary
            else f"feat: {task.title}"
        )
        pr_body = (
            task.review_summary.suggested_pr_body
            if task.review_summary
            else f"Automated implementation for {task.title}\n\n{task.test_summary}"
        )
        if task.issue_number:
            pr_body = f"{pr_body}\n\nCloses #{task.issue_number}"

        # 1. Commit changes
        if not await git_mgr.commit_changes(commit_msg):
            return await self._fail_push(task, "Failed to commit the changes in the sandbox.", "GIT")

        # AST Merge Guard — the target branch may have advanced while the agents worked.
        # Runs here because it needs a clean working tree, which only exists after commit.
        await canvas_mark(task, CanvasStage.MERGE, "RUNNING", f"Checking merge with {effective_target}")
        resolver = MergeConflictResolver(sandbox=sandbox, task=task)
        merge_ok, merge_detail = await resolver.execute_resolution_pipeline(effective_target)
        if not merge_ok:
            return await self._fail_push(task, f"Merge guard: {merge_detail}", "MERGE_CONFLICT")

        if resolver.resolved_files:
            # The resolver committed LLM-written code the operator never approved. Show
            # what the PR will now contain and ask again; the next confirm finds the
            # target already merged and pushes.
            return await self._request_merge_reconfirmation(task, sandbox, effective_target, merge_detail, resolver.resolved_files)

        await canvas_mark(task, CanvasStage.MERGE, "DONE", merge_detail)
        await event_bus.emit_log(
            task_id=task.id,
            message=f"🔀 {merge_detail}",
            event_type=EventType.STEP,
            run_id=task.active_run_id,
        )

        if not await git_mgr.push_working_branch():
            return await self._fail_push(task, "Failed to push working branch to remote git repository.", "GIT")

        # 4. Create Pull/Merge Request. The issue is closed by `Closes #N` when the PR is
        # merged, not here: pushing a branch is not the change landing.
        pr_url = None
        if task.platform == PlatformType.GITHUB:
            owner, repo = self.github_client.parse_repo_owner_and_name(task.repo_name)
            pr_res = await self.github_client.create_pull_request(
                owner=owner,
                repo=repo,
                title=pr_title,
                head=task.working_branch,
                base=effective_target,
                body=pr_body
            )
            if pr_res and "html_url" in pr_res:
                pr_url = pr_res["html_url"]
                task.pr_number = pr_res.get("number")
            # Update labels if suggested
            if task.review_summary and task.review_summary.suggested_labels and task.issue_number:
                await self.github_client.update_issue_labels(owner, repo, task.issue_number, task.review_summary.suggested_labels)

        elif task.platform == PlatformType.GITLAB:
            project_path = self.gitlab_client.parse_project_path(task.repo_url)
            mr_res = await self.gitlab_client.create_merge_request(
                project_path=project_path,
                title=pr_title,
                source_branch=task.working_branch,
                target_branch=effective_target,
                description=pr_body
            )
            if mr_res and "web_url" in mr_res:
                pr_url = mr_res["web_url"]
                task.pr_number = mr_res.get("iid")
            # Update labels if suggested
            if task.review_summary and task.review_summary.suggested_labels and task.issue_number:
                await self.gitlab_client.update_issue_metadata(project_path, task.issue_number, labels=task.review_summary.suggested_labels)

        task.pr_url = pr_url or f"{task.repo_url}/tree/{task.working_branch}"
        task.status = TaskStatus.COMPLETED
        task.completed_at = utc_now()

        # Update active run record
        for r in task.runs:
            if r.run_id == task.active_run_id:
                r.status = TaskStatus.COMPLETED
                r.completed_at = utc_now()
                break

        await save_task(task)
        await TaskDossierManager.sync_dossier_async(task)
        await canvas_mark(
            task, CanvasStage.REVIEWER, "MERGED", task.pr_url or f"pushed to {effective_target}"
        )


        # Post completion comment to Git issue/thread
        await self.post_completion_comment(task)

        await event_bus.emit_log(
            task_id=task.id,
            message=f"🎉 Task completed! PR/Branch targeting `{effective_target}` available at: {task.pr_url}",
            event_type=EventType.STATUS_CHANGE,
            data={"pr_url": task.pr_url, "target_branch": effective_target},
            run_id=task.active_run_id
        )

        from issueforge.bot.telegram_bot import telegram_manager
        await telegram_manager.send_task_completed(task)

        return task

    async def _request_merge_reconfirmation(
        self, task: Task, sandbox: NativeSandbox, target: str, merge_detail: str, resolved_files: list
    ) -> Task:
        ref = shlex.quote(f"origin/{target}")
        stat = await sandbox.run_command(f"git diff --stat {ref} HEAD", emit_events=False)
        full = await sandbox.run_command(f"git diff -U3 {ref} HEAD", emit_events=False)
        task.diff_stat = stat.stdout.strip()
        task.diff_content = full.stdout
        task.status = TaskStatus.AWAITING_CONFIRMATION
        await save_task(task)
        await TaskDossierManager.sync_dossier_async(task)
        message = (
            f"🔀 {merge_detail} The branch now contains LLM-resolved merge code that was not "
            f"part of the reviewed diff — review the updated diff and confirm again to push."
        )
        await canvas_mark(task, CanvasStage.MERGE, "AWAITING_CONFIRMATION", message)
        await event_bus.emit_log(
            task_id=task.id,
            message=message,
            event_type=EventType.STATUS_CHANGE,
            data={"kind": "merge_resolution", "resolved_files": resolved_files},
            run_id=task.active_run_id,
        )
        from issueforge.bot.telegram_bot import telegram_manager
        await telegram_manager.send_review_confirmation(task)
        return task


agent_engine = IssueforgeAgentEngine()

