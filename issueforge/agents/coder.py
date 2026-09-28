from typing import List, Optional

from issueforge.agents.agy_runner import AgySessionRunner
from issueforge.agents.llm import call_llm_with_fallback, extract_json
from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.models import AgentRole, EventType, Task
from issueforge.core.sandbox import NativeSandbox


CODER_STRICT_INSTRUCTIONS = """You are the Senior Software Engineer & Coder Agent in Issueforge.
YOUR SOLE AND EXCLUSIVE RESPONSIBILITY:
1. Read `PLAN.md` in the workspace root.
2. Implement the exact code and documentation changes specified in `PLAN.md` and the task description.
3. Use file modification tools and commands to create, edit, and verify all necessary files in the workspace.

ENGINEERING SKILLS & PRINCIPLES:
- Channel `senior-dev` and `senior-backend-engineer`: Enforce strict security, input validation, defensive error handling, and optimal async concurrency.
- Channel `ponytail`: Enforce YAGNI, standard library first, shortest working solution, and deletion over addition. Remove dead code, redundant abstractions, and unused imports.
- Channel `frontend-tool` and `ui-development` when working on UI or web templates: Maintain cohesive styling, responsive layout, accessible HTML, and snappy interactivity.

CRITICAL CONSTRAINTS:
- ONLY implement what is explicitly requested in `PLAN.md` and the task requirements.
- Do NOT touch unrelated files or introduce speculative scaffolding.
- Ensure all created code and documentation is complete, production-ready, and non-placeholder.
- When finished, verify your changes with `git status` or `git diff --stat`.
"""

CODER_FALLBACK_PROMPT = """You are the Senior Software Engineer & Coder Agent in the Issueforge autonomous coding system.
Your goal is to implement the given plan and resolve all requirements of the task.

You must output a single valid JSON object formatted EXACTLY as follows:
{
  "explanation": "Brief explanation of the changes made",
  "files": [
    {
      "path": "relative/path/to/file.ext",
      "action": "write",
      "content": "Full new content of the file"
    }
  ]
}

Rules:
1. Always write complete, production-ready code with proper error handling, docstrings, and imports. Do NOT leave placeholders like 'TODO: implement' or '// ... existing code ...'.
2. If modifying an existing file, include the entire complete new content of the file.
3. If new test files are needed to test your changes, create them in the appropriate test directory.
"""


class CoderAgent:
    """Writes code and applies file modifications directly inside the native sandbox."""

    def __init__(self, sandbox: NativeSandbox, task: Task):
        self.sandbox = sandbox
        self.task = task
        self.model = settings.coder_model

    async def execute(self, plan: str, test_feedback: Optional[str] = None) -> List[str]:
        run_id = self.sandbox.run_id or "run-1"
        
        step_msg = "💻 Coder Agent implementing code changes..." if not test_feedback else "💻 Coder Agent applying fixes based on test failure feedback..."
        await event_bus.emit_log(
            task_id=self.task.id,
            run_id=run_id,
            message=step_msg,
            role=AgentRole.CODER.value,
            event_type=EventType.STEP
        )

        task_prompt = f"""Task: {self.task.title}
Description:
{self.task.description}

Architectural Plan:
{plan}
"""
        # The Coder previously never saw custom_instructions, so operator answers to
        # clarification questions and canvas steering directives never reached it.
        if self.task.custom_instructions:
            task_prompt += (
                f"\n\nOPERATOR INSTRUCTIONS & STEERING DIRECTIVES (honour these):\n"
                f"{self.task.custom_instructions}\n"
            )
        if test_feedback:
            task_prompt += f"\n\nPREVIOUS TEST FAILURE FEEDBACK (Fix these errors immediately):\n{test_feedback}\n"

        modified_files: List[str] = []

        # 1. Attempt autonomous agy CLI session inside sandbox
        try:
            agy_runner = AgySessionRunner(
                task_id=self.task.id,
                run_id=run_id,
                sandbox_dir=self.sandbox.workspace_path,
                role=AgentRole.CODER,
                model=self.model,
                log_file=self.sandbox.task_dir / "metadata" / run_id / "sandbox.log"
            )
            success, _ = await agy_runner.run_prompt(
                system_instructions=CODER_STRICT_INSTRUCTIONS,
                task_prompt=task_prompt,
                effort="high",
                timeout_seconds=600
            )

            # Query git status to inspect modified and untracked files
            res = await self.sandbox.run_command("git status --porcelain", timeout=30)
            if res.stdout:
                for line in res.stdout.splitlines():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2:
                        file_rel = parts[1]
                        if file_rel != "PLAN.md" and file_rel != "TEST_RESULTS.md":
                            modified_files.append(file_rel)

            if modified_files:
                await event_bus.emit_log(
                    task_id=self.task.id,
                    run_id=run_id,
                    message=f"💻 Coder completed changes to {len(modified_files)} file(s): {', '.join(modified_files[:5])}",
                    role=AgentRole.CODER.value,
                    event_type=EventType.LOG,
                    data={"modified_files": modified_files}
                )
                return modified_files

        except Exception as e:
            await event_bus.emit_log(
                task_id=self.task.id,
                run_id=run_id,
                message=f"ℹ️ agy session failed ({e}). Attempting direct LLM coder fallback...",
                role=AgentRole.CODER.value,
                event_type=EventType.LOG
            )

        # 2. Fallback to direct single-shot LLM if agy made 0 file edits
        file_tree = self.sandbox.list_files(max_depth=4, max_files=40)
        existing_files_content = []
        for f in file_tree[:15]:
            try:
                content = self.sandbox.read_file(f)
                if len(content) < 3000:
                    existing_files_content.append(f"--- File: {f} ---\n{content}\n")
            except Exception:
                pass

        context_str = "\n".join(existing_files_content)
        fallback_prompt = f"""Task: {self.task.title}
Description:
{self.task.description}

Architectural Plan:
{plan}

Repository Files:
{', '.join(file_tree)}

Existing File Samples:
{context_str}
"""
        if test_feedback:
            fallback_prompt += f"\n\nPREVIOUS TEST FAILURE FEEDBACK:\n{test_feedback}\n"

        try:
            response = await call_llm_with_fallback(
                primary_model=self.model,
                messages=[
                    {"role": "system", "content": CODER_FALLBACK_PROMPT},
                    {"role": "user", "content": fallback_prompt}
                ],
                task_id=self.task.id,
                role=AgentRole.CODER.value,
                temperature=0.2,
                max_tokens=4000
            )
            raw_content = response.choices[0].message.content
            parsed = extract_json(raw_content)

            if parsed and "files" in parsed:
                for item in parsed.get("files", []):
                    path = item.get("path")
                    content = item.get("content", "")
                    if path and content:
                        self.sandbox.write_file(path, content)
                        modified_files.append(path)
                        await event_bus.emit_log(
                            task_id=self.task.id,
                            run_id=run_id,
                            message=f"✏️ Modified/Created file: {path}",
                            role=AgentRole.CODER.value,
                            event_type=EventType.LOG,
                            data={"path": path}
                        )
                explanation = parsed.get("explanation", "Applied changes")
                await event_bus.emit_log(
                    task_id=self.task.id,
                    run_id=run_id,
                    message=f"💻 Coder completed changes: {explanation}",
                    role=AgentRole.CODER.value,
                    event_type=EventType.LOG
                )
                return modified_files

        except Exception as e:
            await event_bus.emit_log(
                task_id=self.task.id,
                run_id=run_id,
                message=f"❌ Coder encountered error: {str(e)}",
                role=AgentRole.CODER.value,
                event_type=EventType.ERROR
            )
            raise RuntimeError(f"Coder Agent failed: {str(e)}") from e

        return modified_files
