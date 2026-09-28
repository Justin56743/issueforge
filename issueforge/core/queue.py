import asyncio
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from issueforge.agents.engine import agent_engine
from issueforge.config import settings
from issueforge.core.database import find_existing_task, get_task, save_event, save_task
from issueforge.core.events import event_bus
from issueforge.core.models import AgentRole, EventType, PlatformType, Task, TaskCreateRequest, TaskStatus, TaskType, utc_now
from issueforge.core.sandbox import NativeSandbox


class TaskQueue:
    """Manages task lifecycle, background worker execution, and transitions."""

    def __init__(self):
        self._running_tasks: Dict[str, asyncio.Task] = {}

    def generate_task_id(self, task_type: TaskType, identifier: Optional[int] = None) -> str:
        prefix = "task"
        if task_type == TaskType.ISSUE:
            prefix = "issue"
        elif task_type == TaskType.PULL_REQUEST:
            prefix = "pr"
        elif task_type == TaskType.SPRINT_ITEM:
            prefix = "sprint"

        unique_suffix = uuid.uuid4().hex[:6]
        if identifier is not None:
            return f"{prefix}-{identifier}-{unique_suffix}"
        return f"{prefix}-{unique_suffix}"

    async def create_and_enqueue_task(
        self,
        title: str,
        description: str,
        repo_url: str,
        base_branch: str = "main",
        task_type: TaskType = TaskType.ISSUE,
        platform: PlatformType = PlatformType.GITHUB,
        issue_number: Optional[int] = None,
        pr_number: Optional[int] = None,
        sender: Optional[str] = None,
        custom_instructions: Optional[str] = None,
        auto_approve: bool = False,
        target_branch_candidates: Optional[List[str]] = None,
        selected_target_branch: Optional[str] = None
    ) -> Task:
        """Create a new task in DB and notify Telegram."""
        # Deduplication Guard: Return existing active task if already ingested
        if issue_number is not None or pr_number is not None:
            existing = await find_existing_task(
                repo_url=repo_url,
                platform=platform,
                issue_number=issue_number,
                pr_number=pr_number
            )
            if existing and existing.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                return existing

        task_id = self.generate_task_id(task_type, issue_number or pr_number)
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        if "/" in repo_url:
            parts = repo_url.rstrip("/").replace(".git", "").split("/")
            if len(parts) >= 2:
                repo_name = f"{parts[-2]}/{parts[-1]}"

        working_branch = f"forge/{task_id}"

        target_candidates = list(target_branch_candidates) if target_branch_candidates else []
        if base_branch and base_branch not in target_candidates:
            target_candidates.insert(0, base_branch)
        if selected_target_branch and selected_target_branch not in target_candidates:
            target_candidates.append(selected_target_branch)

        task = Task(
            id=task_id,
            title=title,
            description=description,
            task_type=task_type,
            platform=platform,
            repo_url=repo_url,
            repo_name=repo_name,
            base_branch=base_branch,
            target_branch_candidates=target_candidates,
            selected_target_branch=selected_target_branch or base_branch,
            target_branch_confirmed=bool(selected_target_branch),
            working_branch=working_branch,
            status=TaskStatus.APPROVED if auto_approve else TaskStatus.PENDING_APPROVAL,
            issue_number=issue_number,
            pr_number=pr_number,
            sender=sender,
            custom_instructions=custom_instructions
        )

        await save_task(task)
        await event_bus.emit_log(
            task_id=task.id,
            message=f"📥 New task ingested from {platform.value}: '{task.title}'",
            event_type=EventType.STATUS_CHANGE
        )

        if auto_approve:
            self.start_execution_background(task.id)
        else:
            # Send initial approval message to Telegram
            from issueforge.bot.telegram_bot import telegram_manager
            await telegram_manager.send_new_task_alert(task)

        return task

    def start_execution_background(self, task_id: str) -> asyncio.Task:
        """Launch the multi-LLM pipeline in the background."""
        if task_id in self._running_tasks and not self._running_tasks[task_id].done():
            return self._running_tasks[task_id]

        loop_task = asyncio.create_task(self._run_task_safe(task_id))
        self._running_tasks[task_id] = loop_task
        return loop_task

    async def _run_task_safe(self, task_id: str) -> None:
        try:
            await agent_engine.execute_task_pipeline(task_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await event_bus.emit_log(
                task_id=task_id,
                message=f"Fatal task execution error: {str(e)}",
                event_type=EventType.ERROR
            )
        finally:
            if task_id in self._running_tasks:
                del self._running_tasks[task_id]

    async def cancel_task(self, task_id: str, reason: Optional[str] = None) -> Optional[Task]:
        """Stop running task pipeline and terminate any sandbox subprocess."""
        task = await get_task(task_id)
        if not task:
            return None

        # 1. Kill any active sandbox process group
        NativeSandbox.terminate_task_process(task_id)

        # 2. Cancel running background asyncio task
        if task_id in self._running_tasks:
            bg_task = self._running_tasks[task_id]
            if not bg_task.done():
                bg_task.cancel()
            del self._running_tasks[task_id]

        # 3. Update task in DB
        cancel_reason = reason or "Cancelled by user."
        task.status = TaskStatus.CANCELLED
        task.error_message = cancel_reason
        task.completed_at = utc_now()
        await save_task(task)

        # 4. Emit log event
        await event_bus.emit_log(
            task_id=task.id,
            message=f"🛑 Task stopped and cancelled: {cancel_reason}",
            event_type=EventType.STATUS_CHANGE
        )

        return task

    async def approve_task(self, task_id: str, custom_instructions: Optional[str] = None) -> Optional[Task]:
        """User approves task on Telegram or Web Dashboard."""
        task = await get_task(task_id)
        if not task:
            return None

        task.status = TaskStatus.APPROVED
        if custom_instructions:
            task.custom_instructions = custom_instructions
        await save_task(task)

        await event_bus.emit_log(
            task_id=task.id,
            message="✅ Task approved by user! Initiating Jetson codespace...",
            event_type=EventType.STATUS_CHANGE
        )

        self.start_execution_background(task.id)
        return task

    async def retry_task(self, task_id: str, custom_instructions: Optional[str] = None) -> Optional[Task]:
        """Retry a failed or halted task pipeline with a fresh run attempt."""
        task = await get_task(task_id)
        if not task:
            return None

        # Clean any previous running process or background asyncio task
        NativeSandbox.terminate_task_process(task_id)
        if task_id in self._running_tasks:
            bg_task = self._running_tasks[task_id]
            if not bg_task.done():
                bg_task.cancel()
            del self._running_tasks[task_id]

        task.status = TaskStatus.APPROVED
        task.error_message = None
        if custom_instructions:
            task.custom_instructions = custom_instructions
        await save_task(task)

        attempt_num = len(task.runs) + 1
        await event_bus.emit_log(
            task_id=task.id,
            message=f"🔄 Retrying pipeline (Initiating Run #{attempt_num})...",
            event_type=EventType.STATUS_CHANGE
        )

        self.start_execution_background(task.id)
        return task

    async def reject_task(self, task_id: str, reason: Optional[str] = None) -> Optional[Task]:
        """User rejects task."""
        task = await get_task(task_id)
        if not task:
            return None

        task.status = TaskStatus.REJECTED
        task.error_message = reason or "Rejected by user."
        task.completed_at = utc_now()
        await save_task(task)

        await event_bus.emit_log(
            task_id=task.id,
            message=f"❌ Task rejected: {task.error_message}",
            event_type=EventType.STATUS_CHANGE
        )
        return task

    async def confirm_and_push(self, task_id: str, target_branch: Optional[str] = None) -> Optional[Task]:
        """User confirms changes; push branch and open PR."""
        task = await get_task(task_id)
        if not task:
            return None

        return await agent_engine.push_and_create_pr(task_id, target_branch=target_branch)

    async def set_target_branch(self, task_id: str, branch_name: str) -> Optional[Task]:
        """Set the target branch explicitly for a task."""
        task = await get_task(task_id)
        if not task:
            return None

        clean_branch = branch_name.strip()
        if not clean_branch:
            return task

        if not task.target_branch_candidates:
            task.target_branch_candidates = []
        if clean_branch not in task.target_branch_candidates:
            task.target_branch_candidates.append(clean_branch)

        task.selected_target_branch = clean_branch
        # An explicit pick is what the AWAITING_BRANCH_SELECTION gate is waiting for.
        task.target_branch_confirmed = True

        resuming = task.status == TaskStatus.AWAITING_BRANCH_SELECTION
        if resuming:
            task.status = TaskStatus.APPROVED

        await save_task(task)
        await event_bus.emit_log(
            task_id=task.id,
            message=f"🌿 Target branch set to `{clean_branch}`.",
            event_type=EventType.STATUS_CHANGE
        )

        if resuming:
            await event_bus.emit_log(
                task_id=task.id,
                message=f"▶️ Resuming pipeline against `{clean_branch}`.",
                event_type=EventType.STATUS_CHANGE
            )
            self.start_execution_background(task.id)
        return task

    async def submit_question_answer(
        self,
        task_id: str,
        question_id: str,
        answer_text: str,
        selected_option: Optional[str] = None
    ) -> Optional[Task]:
        """Operator answers a clarification question from the Orchestrator."""
        from issueforge.core.database import answer_question
        ans = await answer_question(task_id, question_id, answer_text, selected_option)
        if not ans:
            return None

        task = await get_task(task_id)
        if not task:
            return None

        # Append answer to custom instructions so Planner and Coder have it
        instruction_addon = f"\n\n[Operator Answer to '{ans.question}']: {ans.answer or ans.selected_option}"
        task.custom_instructions = (f"{task.custom_instructions or ''}{instruction_addon}").strip()
        task.status = TaskStatus.APPROVED
        await save_task(task)

        await event_bus.emit_log(
            task_id=task.id,
            message=f"💡 Answer received: '{ans.answer or ans.selected_option}'. Resuming pipeline...",
            event_type=EventType.STATUS_CHANGE
        )

        # Resume background execution
        self.start_execution_background(task.id)
        return task

    async def request_revision(self, task_id: str, revision_notes: str) -> Optional[Task]:
        """User requests revision with custom notes."""
        task = await get_task(task_id)
        if not task:
            return None

        task.status = TaskStatus.REVISION_REQUESTED
        task.custom_instructions = (
            f"{task.custom_instructions or ''}\n\nRevision Requested: {revision_notes}"
        ).strip()
        await save_task(task)

        await event_bus.emit_log(
            task_id=task.id,
            message=f"🔄 Revision requested: {revision_notes}",
            event_type=EventType.STATUS_CHANGE
        )

        self.start_execution_background(task.id)
        return task

    async def steer_task(self, task_id: str, directive: str) -> Optional[Task]:
        """Inject operator mid-flight steering directive into running task memory, terminal, and event bus."""
        task = await get_task(task_id)
        if not task:
            return None

        clean_directive = directive.strip()
        if not clean_directive:
            return task

        task.custom_instructions = (
            f"{task.custom_instructions or ''}\n\n[Operator Mid-Flight Steering Directive]:\n{clean_directive}"
        ).strip()
        await save_task(task)

        from issueforge.core.task_dossier import TaskDossierManager
        TaskDossierManager.sync_dossier(task)

        active_run = task.active_run_id or "run-1"
        # 1. Emit user prompt event
        await event_bus.emit_log(
            task_id=task.id,
            run_id=active_run,
            message=f"💬 Operator Prompt: {clean_directive}",
            event_type=EventType.STEP,
            data={"type": "user_prompt", "directive": clean_directive, "steering": clean_directive}
        )

        # 2. Emit active agent acknowledgment reply
        ack_msg = f"🤖 Agent: Directive received. Incorporating '{clean_directive[:60]}' into current execution..."
        await event_bus.emit_log(
            task_id=task.id,
            run_id=active_run,
            message=ack_msg,
            role=AgentRole.ORCHESTRATOR.value,
            event_type=EventType.LOG,
            data={"type": "agent_reply", "ack": ack_msg}
        )

        # 3. Broadcast bidirectional conversation to terminal session
        from issueforge.core.terminal_manager import terminal_manager
        session = terminal_manager.get_session(task.id, active_run)
        convo_banner = (
            f"\r\n\x1b[1;36m💬 [OPERATOR PROMPT]\x1b[0m \x1b[1;37m{clean_directive}\x1b[0m\r\n"
            f"\x1b[1;32m🤖 [AGENT REPLY]\x1b[0m Directive received. Updating workspace and execution strategy...\r\n\r\n"
        )
        await session.broadcast_text(convo_banner)

        return task


task_queue = TaskQueue()

