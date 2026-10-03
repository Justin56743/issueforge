import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from issueforge.agents.agy_runner import AgySessionRunner
from issueforge.agents.coder import CoderAgent
from issueforge.agents.llm import call_llm_with_fallback, extract_json
from issueforge.agents.planner import PlannerAgent
from issueforge.agents.reviewer import ReviewerAgent
from issueforge.agents.tester import TesterAgent
from issueforge.config import settings
from issueforge.core.database import (
    get_task_learnings,
    get_task_questions,
    list_learnings,
    save_event,
    save_learning,
    save_question,
    save_task,
)
from issueforge.core.events import event_bus
from issueforge.core.models import (
    AgentRole,
    EventType,
    QuestionStatus,
    ReviewSummary,
    Task,
    TaskEvent,
    TaskLearning,
    TaskQuestion,
    TaskStatus,
    TestResult,
    utc_now,
)
from issueforge.core.sandbox import NativeSandbox

logger = logging.getLogger(__name__)

ORCHESTRATOR_TRIAGE_PROMPT = """You are the Supervisory Orchestrator Agent ("Tech Lead") in the Issueforge autonomous engineering system on NVIDIA Jetson Orin.
Your duty is to inspect incoming task requirements, discussion comments, and codebase context to determine if the task has high ambiguity requiring human clarification before proceeding.

Respond with a JSON object in this format:
{
  "needs_clarification": false,
  "confidence_score": 0.95,
  "reasoning": "Explanation of task clarity",
  "suggested_priority": "Priority::Medium",
  "suggested_labels": ["feature", "backend"],
  "question": null
}

If `needs_clarification` is TRUE (only when genuinely ambiguous, missing critical requirements, or risky architecture choices):
{
  "needs_clarification": true,
  "confidence_score": 0.4,
  "reasoning": "Why human input is needed",
  "suggested_priority": "Priority::High",
  "suggested_labels": ["needs-info"],
  "question": {
    "question": "Clear, concise question for the developer",
    "context": "Context explaining why this decision matters",
    "options": ["Option A description", "Option B description", "Option C description"]
  }
}
"""

ORCHESTRATOR_LEARNING_PROMPT = """You are the Self-Evolving Knowledge Harvester in the Issueforge autonomous engineering system.
Your job is to analyze the completed task, code diff, and test results, and distill a reusable engineering pattern / learned skill for the harness memory.

Output a valid JSON object:
{
  "topic": "Concise topic title (e.g. FastAPI SSE Endpoint Pattern, Pytest Async Fixture Setup)",
  "summary": "1-2 sentence description of what was solved and how",
  "solution_pattern": "Actionable technical instructions, patterns, or caveats for future agents solving similar problems",
  "tags": ["fastapi", "sse", "streaming"]
}
"""

ORCHESTRATOR_OPERATOR_INSTRUCTION_PROMPT = """You are the Supervisory Orchestrator Agent in the Issueforge autonomous engineering system on Jetson Orin.
A software developer is giving you instructions via Telegram.
Your role is to understand their intent, extract engineering details, and determine what Issueforge should do.

You MUST respond strictly with a valid JSON object matching this schema:
{
  "intent": "CREATE_AND_RUN_TASK" | "TASK_ACTION" | "STEER_TASK" | "STATUS_QUERY" | "GENERAL_CHAT",
  "task_title": "Concise title for new task (if CREATE_AND_RUN_TASK)",
  "task_description": "Technical requirements and instructions (if CREATE_AND_RUN_TASK)",
  "target_branch": "Target branch name if specified (e.g. main, v1, test/v1, staging) or null",
  "referenced_task_id": "Exact task id if the user referenced one (e.g. issue-1, task-123) or null",
  "task_action": "APPROVE" | "CANCEL" | "RETRY" | "PUSH" | null,
  "steering_directive": "Mid-flight guidance or constraint for active running task (if STEER_TASK) or null",
  "reply_summary": "A natural, helpful markdown response to the developer explaining what action is taking place"
}

Guidelines:
- If the operator is giving mid-flight steering, advice, constraints, or directives for an in-progress task (e.g. 'steer task-123: focus on auth', 'tell coder to use PostgreSQL', 'pause and check routes'), set intent to "STEER_TASK".
- If the operator is asking to implement, build, fix, add, create, or modify code, set intent to "CREATE_AND_RUN_TASK". You will take over running the engineering sandbox and agents.
- If the operator mentions approving, retrying, canceling, or pushing an existing task, set intent to "TASK_ACTION" with appropriate task_action.
- If the operator asks about current status, progress, or what is running, set intent to "STATUS_QUERY".
- Keep reply_summary professional, concise, and engineering-focused.
"""


class SupervisoryOrchestrator:
    """
    Central cognitive Tech Lead overlooking task triage, operator Q&A,
    sub-agent delegation (Planner, Coder, Tester, Reviewer), and self-evolving knowledge.
    """

    def __init__(self, sandbox: NativeSandbox, task: Task):
        self.sandbox = sandbox
        self.task = task
        self.model = settings.planner_model

    async def retrieve_harness_learnings(self) -> str:
        """Query persistent harness memory for relevant skills and patterns."""
        try:
            all_learnings = await list_learnings(limit=20)
            if not all_learnings:
                return ""

            task_keywords = set((self.task.title + " " + self.task.description).lower().split())
            relevant = []
            for l in all_learnings:
                l_text = f"{l.topic} {l.summary} {' '.join(l.tags)}".lower()
                # Check for keyword overlap or include recent learnings
                if any(w in l_text for w in task_keywords if len(w) > 3):
                    relevant.append(l)

            if not relevant and all_learnings:
                relevant = all_learnings[:3]  # Use most recent if no strict match

            if relevant:
                formatted = ["=== HARNESS MEMORY (Learned Skills & Patterns) ==="]
                for item in relevant[:5]:
                    formatted.append(f"• Topic: {item.topic}\n  Summary: {item.summary}\n  Pattern: {item.solution_pattern}")
                return "\n\n".join(formatted) + "\n"
        except Exception as e:
            logger.warning("Failed to retrieve harness learnings: %s", e)
        return ""

    async def triage_task(self) -> Tuple[bool, Optional[TaskQuestion]]:
        """
        Inspect task for ambiguities.
        Returns (needs_clarification, optional_question).
        """
        # If user provided explicit instructions or has already answered questions, don't re-triage as ambiguous
        existing_questions = await get_task_questions(self.task.id)
        if any(q.status == QuestionStatus.ANSWERED for q in existing_questions):
            return False, None

        await event_bus.emit_log(
            task_id=self.task.id,
            message="👑 Supervisory Orchestrator triaging task requirements & comments...",
            role=AgentRole.ORCHESTRATOR.value,
            event_type=EventType.STEP,
            run_id=self.sandbox.run_id
        )

        user_prompt = f"""Task Title: {self.task.title}
Task Type: {self.task.task_type.value}
Platform: {self.task.platform.value}
Description:
{self.task.description}

Existing Discussion / Comments Context:
{self.task.comments_context or 'None'}

Custom Instructions:
{self.task.custom_instructions or 'None'}
"""

        parsed = None
        # 1. First attempt autonomous agy CLI session inside sandbox workspace
        try:
            agy_runner = AgySessionRunner(
                task_id=self.task.id,
                run_id=self.sandbox.run_id,
                sandbox_dir=self.sandbox.workspace_path,
                role=AgentRole.ORCHESTRATOR,
                model=self.model,
                log_file=self.sandbox.task_dir / "metadata" / self.sandbox.run_id / "sandbox.log"
            )
            success, final_text = await agy_runner.run_prompt(
                system_instructions=ORCHESTRATOR_TRIAGE_PROMPT,
                task_prompt=user_prompt,
                effort="low",
                timeout_seconds=120
            )
            if success and final_text:
                parsed = extract_json(final_text)
        except Exception as e:
            logger.debug("Agy triage session exception: %s", e)

        # 2. Fallback to direct LLM if agy triage didn't produce result
        if not parsed:
            try:
                response = await call_llm_with_fallback(
                    primary_model=self.model,
                    messages=[
                        {"role": "system", "content": ORCHESTRATOR_TRIAGE_PROMPT},
                        {"role": "user", "content": user_prompt}
                    ],
                    task_id=self.task.id,
                    role=AgentRole.ORCHESTRATOR.value,
                    run_id=self.sandbox.run_id,
                    temperature=0.2,
                    max_tokens=1500
                )
                parsed = extract_json(response.choices[0].message.content)
            except Exception as e:
                logger.warning("Orchestrator triage fallback exception: %s", e)

        if parsed:
            if parsed.get("suggested_priority") and not self.task.priority:
                self.task.priority = parsed.get("suggested_priority")
            if parsed.get("suggested_labels") and not self.task.labels:
                self.task.labels = parsed.get("suggested_labels", [])

            needs_clarification = parsed.get("needs_clarification", False)
            q_data = parsed.get("question")
            if needs_clarification and q_data and q_data.get("question"):
                question = TaskQuestion(
                    id=f"q-{uuid.uuid4().hex[:6]}",
                    task_id=self.task.id,
                    question=q_data.get("question"),
                    context=q_data.get("context"),
                    options=q_data.get("options", []),
                    status=QuestionStatus.PENDING
                )
                await save_question(question)
                await event_bus.emit_log(
                    task_id=self.task.id,
                    message=f"❓ Orchestrator posed question: {question.question}",
                    role=AgentRole.ORCHESTRATOR.value,
                    event_type=EventType.QUESTION,
                    data={"question": question.model_dump()},
                    run_id=self.sandbox.run_id
                )
                return True, question

        return False, None

    async def run_planner(self, memory_context: str = "") -> str:
        """Dispatch plan generation to PlannerAgent with harness memory."""
        planner = PlannerAgent(sandbox=self.sandbox, task=self.task)
        # Passed per call, never written into task.custom_instructions: that field is
        # persisted, so the memory block would be appended again on every run.
        return await planner.execute(memory_context=memory_context)

    async def run_coder(self, plan: str, test_feedback: Optional[str] = None) -> List[str]:
        """Dispatch implementation to CoderAgent."""
        coder = CoderAgent(sandbox=self.sandbox, task=self.task)
        return await coder.execute(plan=plan, test_feedback=test_feedback)

    async def run_tester(self) -> TestResult:
        """Dispatch test suite verification to TesterAgent."""
        tester = TesterAgent(sandbox=self.sandbox, task=self.task)
        return await tester.execute()

    async def run_reviewer(self, diff_stat: str, diff_content: str, test_result: TestResult, blast_radius: Any = None) -> ReviewSummary:
        """Dispatch diff review and risk assessment to ReviewerAgent."""
        reviewer = ReviewerAgent(task=self.task, sandbox=self.sandbox)
        return await reviewer.execute(
            diff_stat=diff_stat,
            diff_content=diff_content,
            test_result=test_result,
            blast_radius=blast_radius,
        )

    async def record_task_learning(self, plan: str, diff_stat: str, test_result: TestResult) -> Optional[TaskLearning]:
        """Distill solution patterns into harness memory using local AGY session."""
        user_prompt = f"""Task: {self.task.title}
Description: {self.task.description}
Plan: {plan[:1000]}
Diff Stat: {diff_stat}
Test Result: Passed ({test_result.test_runner})
"""
        parsed = None

        # 1. Primary: Run via local AGY session runner (CLI-based, no external API quota consumed)
        try:
            agy_runner = AgySessionRunner(
                sandbox_dir=self.sandbox.workspace_path,
                task_id=self.task.id,
                run_id=self.sandbox.run_id,
                role=AgentRole.ORCHESTRATOR,
                model=self.model
            )
            success, final_text = await agy_runner.run_prompt(
                system_instructions=ORCHESTRATOR_LEARNING_PROMPT,
                task_prompt=user_prompt,
                effort="low",
                timeout_seconds=60
            )
            if success and final_text:
                parsed = extract_json(final_text)
        except Exception as e:
            logger.debug("Agy learning session exception: %s", e)

        # 2. Secondary fallback: direct LLM if agy was unavailable
        if not parsed:
            try:
                response = await call_llm_with_fallback(
                    primary_model=self.model,
                    messages=[
                        {"role": "system", "content": ORCHESTRATOR_LEARNING_PROMPT},
                        {"role": "user", "content": user_prompt}
                    ],
                    task_id=self.task.id,
                    role=AgentRole.ORCHESTRATOR.value,
                    run_id=self.sandbox.run_id,
                    temperature=0.2,
                    max_tokens=1000
                )
                parsed = extract_json(response.choices[0].message.content)
            except Exception as e:
                logger.debug("Orchestrator learning fallback exception: %s", e)

        # 3. Deterministic distillation fallback if no LLM response produced
        if not parsed or not parsed.get("topic"):
            tags = [self.task.task_type.value.lower()]
            if self.task.labels:
                tags.extend(self.task.labels)
            parsed = {
                "topic": self.task.title[:60],
                "summary": f"Completed task: {self.task.title}.",
                "solution_pattern": "Applied verified code changes and test suite validation within the native sandbox.",
                "tags": tags
            }

        if parsed and parsed.get("topic"):
            learning = TaskLearning(
                id=f"learn-{uuid.uuid4().hex[:6]}",
                task_id=self.task.id,
                topic=parsed.get("topic"),
                summary=parsed.get("summary", "Solved engineering task."),
                solution_pattern=parsed.get("solution_pattern", "Applied modular code changes."),
                tags=parsed.get("tags", [])
            )
            await save_learning(learning)
            # Project the lesson into the Obsidian vault as a linked note. Best-effort:
            # the SQLite row remains the source of truth and the retrieval path.
            try:
                from issueforge.vault.knowledge_vault import write_lesson_note

                write_lesson_note(learning, task=self.task)
            except Exception as kex:
                logger.warning("Knowledge note not written: %s", kex)
            await event_bus.emit_log(
                task_id=self.task.id,
                message=f"📚 Harness recorded new skill: '{learning.topic}'",
                role=AgentRole.ORCHESTRATOR.value,
                event_type=EventType.LEARNING,
                data={"learning": learning.model_dump()},
                run_id=self.sandbox.run_id
            )
            return learning
        return None

    @classmethod
    async def interpret_operator_instruction(
        cls,
        instruction: str,
        recent_tasks: Optional[List[Task]] = None
    ) -> Dict[str, Any]:
        """Parse natural language operator instructions from Telegram/Chat into structured actions."""
        recent_context = ""
        if recent_tasks:
            lines = []
            for t in recent_tasks[:5]:
                target = t.selected_target_branch or t.base_branch or "main"
                lines.append(f"- {t.id}: '{t.title}' [status: {t.status.value}, branch: {target}]")
            recent_context = "\nRecent tasks in system:\n" + "\n".join(lines)

        user_prompt = f"Operator Instruction:\n{instruction}\n{recent_context}"
        import re

        # 1. Try AGY CLI first (no sandbox needed — use a dedicated workspace dir)
        try:
            operator_dir = settings.forge_workspace_root / "operator"
            operator_dir.mkdir(parents=True, exist_ok=True)
            agy_runner = AgySessionRunner(
                task_id="operator-cmd",
                run_id="op-1",
                sandbox_dir=operator_dir,
                role=AgentRole.ORCHESTRATOR,
                model=settings.default_model,
            )
            success, raw = await agy_runner.run_prompt(
                system_instructions=ORCHESTRATOR_OPERATOR_INSTRUCTION_PROMPT,
                task_prompt=user_prompt,
                effort="low",
                timeout_seconds=60
            )
            if raw:
                match = re.search(r"\{[\s\S]*\}", raw)
                if match:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, dict) and "intent" in parsed:
                        return parsed
        except Exception as e:
            logger.debug("AGY operator instruction parsing failed: %s", e)

        # 2. Fallback to direct LLM call
        try:
            response = await call_llm_with_fallback(
                primary_model=settings.default_model,
                messages=[
                    {"role": "system", "content": ORCHESTRATOR_OPERATOR_INSTRUCTION_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                role=AgentRole.ORCHESTRATOR.value,
                temperature=0.2,
                max_tokens=800
            )
            raw = response.choices[0].message.content
            match = re.search(r"\{[\s\S]*\}", raw)
            if match:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict) and "intent" in parsed:
                    return parsed
        except Exception as e:
            logger.warning("Orchestrator instruction parsing exception: %s", e)

        # Fallback heuristic parser
        lower = instruction.lower()

        def mentions(*phrases: str) -> bool:
            # Whole words only: as substrings, "skill" contained "kill" and cancelled the
            # latest task, and "restart" contained "start" and approved it.
            return re.search(r"\b(?:" + "|".join(map(re.escape, phrases)) + r")\b", lower) is not None

        words = instruction.split()
        ref_id = None
        if recent_tasks:
            for t in recent_tasks:
                if t.id in instruction:
                    ref_id = t.id
                    break
        if not ref_id:
            for w in words:
                cleaned = w.strip(".,;:!?()[]")
                if "issue-" in cleaned or "task-" in cleaned or "pr-" in cleaned or cleaned.startswith("test-") or cleaned.endswith("-task"):
                    ref_id = cleaned
                    break

        if mentions("approve", "start", "launch", "run issue", "run task"):
            return {
                "intent": "TASK_ACTION",
                "task_action": "APPROVE",
                "referenced_task_id": ref_id,
                "reply_summary": f"Approving and initiating sandbox for {ref_id or 'the active task'}."
            }
        elif mentions("cancel", "stop", "kill", "halt"):
            return {
                "intent": "TASK_ACTION",
                "task_action": "CANCEL",
                "referenced_task_id": ref_id,
                "reply_summary": f"Stopping and cancelling {ref_id or 'the active task'}."
            }
        elif mentions("retry", "rerun", "try again"):
            return {
                "intent": "TASK_ACTION",
                "task_action": "RETRY",
                "referenced_task_id": ref_id,
                "reply_summary": f"Retrying task {ref_id or 'the active task'} with a clean sandbox."
            }
        elif mentions("steer", "directive", "instruct", "guidance", "tell coder", "tell agent"):
            clean_directive = instruction
            for kw in ["steer", "directive", "guidance"]:
                if kw in lower:
                    idx = lower.find(kw)
                    clean_directive = instruction[idx + len(kw):].lstrip(": -")
                    break
            return {
                "intent": "STEER_TASK",
                "referenced_task_id": ref_id,
                "steering_directive": clean_directive or instruction,
                "reply_summary": f"Injected mid-flight steering directive for {ref_id or 'the active task'}: '{clean_directive or instruction}'."
            }
        elif mentions("status", "what is running", "what's running", "progress"):
            return {
                "intent": "STATUS_QUERY",
                "reply_summary": "Here is the current system status."
            }
        else:
            first_line = instruction.splitlines()[0] if instruction else "Developer Request"
            title = first_line[:70] + ("..." if len(first_line) > 70 else "")
            return {
                "intent": "CREATE_AND_RUN_TASK",
                "task_title": title,
                "task_description": instruction,
                "target_branch": None,
                "reply_summary": f"Created new task '{title}' and initiated engineering sandbox."
            }
