import json
from pathlib import Path
from typing import Dict, List, Optional

from issueforge.agents.agy_runner import AgySessionRunner
from issueforge.agents.llm import call_llm_with_fallback
from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.models import AgentRole, EventType, Task
from issueforge.core.sandbox import NativeSandbox


PLANNER_STRICT_INSTRUCTIONS = """You are the Principal Architecture & Planning Agent in Issueforge.
YOUR SOLE AND EXCLUSIVE RESPONSIBILITY:
1. Inspect the codebase using tools (view_file, grep_search, list_dir).
2. Formulate an actionable architectural plan and subtasks checklist.
3. WRITE your complete plan into `PLAN.md` in the root of the repository.

ENGINEERING SKILLS & PRINCIPLES:
- Channel `senior-dev` and `senior-backend-engineer`: Architect for security, optimal performance, clean module boundaries, and robust error recovery.
- Channel `ponytail`: Question unneeded complexity, eliminate speculative abstractions, reuse existing utilities, and favor the standard library.
- Channel `frontend-tool` and `ui-development` on frontend/UI tasks: Specify distinctive, accessible, responsive design requirements.

CRITICAL CONSTRAINTS:
- DO NOT modify existing source code files.
- DO NOT execute build or code-modifying commands.
- Your ONLY deliverable is creating the `PLAN.md` file in the workspace root.
- The PLAN.md must include:
  # Architectural Implementation Plan
  ## 1. Architectural Assessment
  ## 2. Target Files to Create / Modify
  ## 3. Subtasks Checklist
  - [ ] Subtask 1
  - [ ] Subtask 2
"""

PLANNER_FALLBACK_PROMPT = """You are the Lead Software Architect & Planner Agent in the Issueforge autonomous coding system.
Your job is to analyze the repository structure, read key source and config files, and produce a concise, actionable, step-by-step implementation plan to resolve the given task/issue.

Format your output as a clear markdown document containing:
1. Architectural Assessment (Context, dependencies, target components)
2. Step-by-Step Implementation Strategy (Exact files to create/modify and what logic to write)
3. Testing Strategy (How the changes will be validated and what test suites to execute)
4. Subtasks Checklist:
- [ ] Task 1
- [ ] Task 2

Keep the plan laser-focused and practical.
"""


class PlannerAgent:
    """Analyzes the repository and produces a step-by-step implementation plan."""

    def __init__(self, sandbox: NativeSandbox, task: Task):
        self.sandbox = sandbox
        self.task = task
        self.model = settings.planner_model

    async def execute(self) -> str:
        run_id = self.sandbox.run_id or "run-1"
        await event_bus.emit_log(
            task_id=self.task.id,
            run_id=run_id,
            message="🧠 Planner Agent analyzing repository architecture and planning changes...",
            role=AgentRole.PLANNER.value,
            event_type=EventType.STEP
        )

        task_prompt = f"""Task: {self.task.title}
Type: {self.task.task_type.value}
Description:
{self.task.description}

Custom User Instructions:
{self.task.custom_instructions or 'None'}
"""

        plan = ""
        # 1. Attempt autonomous agy CLI session inside sandbox
        try:
            agy_runner = AgySessionRunner(
                task_id=self.task.id,
                run_id=run_id,
                sandbox_dir=self.sandbox.workspace_path,
                role=AgentRole.PLANNER,
                model=self.model,
                log_file=self.sandbox.task_dir / "metadata" / run_id / "sandbox.log"
            )
            success, final_text = await agy_runner.run_prompt(
                system_instructions=PLANNER_STRICT_INSTRUCTIONS,
                task_prompt=task_prompt,
                effort="medium",
                timeout_seconds=300
            )

            # Check if PLAN.md was created on disk
            plan_file = self.sandbox.workspace_path / "PLAN.md"
            if plan_file.exists():
                plan = plan_file.read_text(encoding="utf-8").strip()
            elif final_text and len(final_text.strip()) > 50:
                plan = final_text.strip()
                plan_file.write_text(plan, encoding="utf-8")
        except Exception as e:
            await event_bus.emit_log(
                task_id=self.task.id,
                run_id=run_id,
                message=f"ℹ️ agy session failed or unavailable ({e}). Falling back to direct LLM planner...",
                role=AgentRole.PLANNER.value,
                event_type=EventType.LOG
            )

        # 2. Fallback to direct LLM if agy didn't produce plan
        if not plan:
            file_tree = self.sandbox.list_files(max_depth=4, max_files=60)
            tree_str = "\n".join(f"- {f}" for f in file_tree[:40])
            fallback_user_prompt = f"""Repository File Structure:
{tree_str}

{task_prompt}

Please formulate the comprehensive architectural plan to implement this task cleanly and correctly.
"""
            try:
                response = await call_llm_with_fallback(
                    primary_model=self.model,
                    messages=[
                        {"role": "system", "content": PLANNER_FALLBACK_PROMPT},
                        {"role": "user", "content": fallback_user_prompt}
                    ],
                    task_id=self.task.id,
                    role=AgentRole.PLANNER.value,
                    temperature=0.2,
                    max_tokens=2500
                )
                plan = response.choices[0].message.content.strip()
                if not plan:
                    raise ValueError("Planner returned an empty plan.")
                # Save PLAN.md
                plan_file = self.sandbox.workspace_path / "PLAN.md"
                plan_file.write_text(plan, encoding="utf-8")
            except Exception as e:
                await event_bus.emit_log(
                    task_id=self.task.id,
                    run_id=run_id,
                    message=f"❌ Planner Agent failed to formulate plan: {str(e)}",
                    role=AgentRole.PLANNER.value,
                    event_type=EventType.ERROR
                )
                raise RuntimeError(f"Planner Agent failed: {str(e)}") from e

        # Save to task dossier
        try:
            dossier_plan = self.sandbox.task_dir / "PLAN.md"
            dossier_plan.write_text(plan, encoding="utf-8")
        except Exception:
            pass

        await event_bus.emit_log(
            task_id=self.task.id,
            run_id=run_id,
            message=f"🧠 Planner generated plan:\n{plan[:250]}...",
            role=AgentRole.PLANNER.value,
            event_type=EventType.LOG,
            data={"plan": plan}
        )

        return plan
