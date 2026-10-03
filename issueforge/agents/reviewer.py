from pathlib import Path
from typing import Any, List, Optional

from issueforge.agents.agy_runner import AgySessionRunner
from issueforge.agents.llm import call_llm_with_fallback, extract_json
from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.models import AgentRole, EventType, ReviewSummary, Task, TestResult
from issueforge.core.sandbox import NativeSandbox


REVIEWER_STRICT_INSTRUCTIONS = """You are the Principal Code Reviewer & Security Critic in Issueforge.
YOUR SOLE AND EXCLUSIVE RESPONSIBILITY:
1. Inspect the git diff and modified files in the workspace using tools (view_file, grep_search, list_dir).
2. Inspect `TEST_RESULTS.md` in the workspace root to confirm test outcomes.
3. Write a comprehensive review document into `REVIEW.md` in the workspace root formatted as:
   # Code Review & Security Assessment
   ## 1. Summary of Changes
   ## 2. Risk Assessment (Low, Medium, or High with rationale)
   ## 3. Test Verification Analysis
   ## 4. Suggested PR Title & Description
4. Output your final structured review as a valid JSON object matching this schema:
```json
{
  "summary": "Crisp 2-3 sentence overview of changes",
  "risk_assessment": "Low | Medium | High (with explanation)",
  "test_verification": "Summary of test suite validation results",
  "files_changed": ["list", "of", "modified", "files"],
  "suggested_commit_message": "feat(scope): concise title\\n\\nDetailed commit description",
  "suggested_pr_title": "Concise Pull Request Title",
  "suggested_pr_body": "## Overview\\nDescription of changes\\n\\n## Verification\\nTest results and validation"
}
```

ENGINEERING SKILLS & CRITIQUE GUIDELINES:
- Channel `senior-dev` and `senior-backend-engineer`: Rigorously scrutinize for security vulnerabilities, path traversal risks, leaked credentials, unhandled edge cases, and resource leaks.
- Channel `ponytail`: Scrutinize for over-engineering, dead code, redundant boilerplate, and unnecessary dependencies. Flag any speculative complexity.
- Channel `frontend-tool` and `ui-development` on frontend changes: Confirm UI responsiveness, clean design, and accessibility.
"""

REVIEWER_FALLBACK_PROMPT = """You are the Principal Code Reviewer & Security Critic in the Issueforge autonomous coding system.
Your job is to critically review the git diff and test results produced by the Coder and Tester agents.

Output a valid JSON object formatted EXACTLY as follows:
{
  "summary": "Crisp 2-3 sentence overview of what was implemented or fixed.",
  "risk_assessment": "Low | Medium | High (with 1 sentence explanation)",
  "test_verification": "Summary of test suite validation results",
  "files_changed": ["list", "of", "modified", "files"],
  "suggested_commit_message": "feat(scope): concise commit title\\n\\nDetailed commit description",
  "suggested_pr_title": "Concise Pull Request Title",
  "suggested_pr_body": "## Overview\\nDescription of changes\\n\\n## Verification\\nTest results and validation"
}
"""


class ReviewerAgent:
    """Evaluates code diffs, assesses risk, and prepares Telegram summaries and PR content."""

    def __init__(self, task: Task, sandbox: Optional[NativeSandbox] = None):
        self.task = task
        self.sandbox = sandbox
        self.model = settings.reviewer_model

    def _write_review_md(self, summary: ReviewSummary) -> None:
        """Persist REVIEW.md in the workspace and task dossier."""
        review_md = f"""# Code Review & Security Assessment

## 1. Summary of Changes
{summary.summary}

## 2. Risk Assessment
**Risk Level:** {summary.risk_assessment}

## 3. Test Verification
{summary.test_verification}

## 4. Suggested Pull Request
- **Title:** {summary.suggested_pr_title}
- **Commit Message:** `{summary.suggested_commit_message}`

### Description
{summary.suggested_pr_body}
"""
        if self.sandbox:
            try:
                (self.sandbox.workspace_path / "REVIEW.md").write_text(review_md, encoding="utf-8")
                dossier_review = self.sandbox.task_dir / "REVIEW.md"
                dossier_review.write_text(review_md, encoding="utf-8")
            except Exception:
                pass

    async def execute(self, diff_stat: str, diff_content: str, test_result: TestResult, blast_radius: Any = None) -> ReviewSummary:
        run_id = (self.sandbox.run_id if self.sandbox else None) or self.task.active_run_id or "run-1"

        await event_bus.emit_log(
            task_id=self.task.id,
            message="🔍 Reviewer Agent assessing diff quality, security, and preparing PR summary...",
            role=AgentRole.REVIEWER.value,
            event_type=EventType.STEP,
            run_id=run_id
        )

        truncated_diff = diff_content[:6000]
        if len(diff_content) > 6000:
            truncated_diff += "\n... (diff truncated for review)"

        # Static dependency analysis gives the reviewer downstream impact the diff alone
        # does not show — a one-line change to a hub module is not a one-line risk.
        blast_section = ""
        if blast_radius is not None and not getattr(blast_radius, "indeterminate", True):
            impacted = ", ".join(blast_radius.impacted_files[:15]) or "none"
            tests = ", ".join(blast_radius.suggested_tests[:10]) or "none"
            blast_section = (
                f"\n\nStatic Dependency Analysis (AST import graph):\n"
                f"- Downstream risk level: {blast_radius.risk_level}\n"
                f"- Impacted files ({len(blast_radius.impacted_files)}): {impacted}\n"
                f"- Related test files: {tests}\n"
                f"Weigh this when assessing risk: a small diff in a widely imported module "
                f"is higher risk than its line count suggests.\n"
            )

        user_prompt = f"""Task: {self.task.title}
Description: {self.task.description}

Git Diff Stat:
{diff_stat}

Git Diff Content:
{truncated_diff}

Test Execution Result:
Passed: {test_result.passed}
Runner: {test_result.test_runner}
Passed Tests: {test_result.passed_tests}
Failed Tests: {test_result.failed_tests}
{blast_section}"""

        # 1. Attempt autonomous agy CLI session inside sandbox workspace
        if self.sandbox:
            try:
                agy_runner = AgySessionRunner(
                    task_id=self.task.id,
                    run_id=run_id,
                    sandbox_dir=self.sandbox.workspace_path,
                    role=AgentRole.REVIEWER,
                    model=self.model,
                    log_file=self.sandbox.task_dir / "metadata" / run_id / "sandbox.log"
                )
                success, final_text = await agy_runner.run_prompt(
                    system_instructions=REVIEWER_STRICT_INSTRUCTIONS,
                    task_prompt=user_prompt,
                    effort="medium",
                    timeout_seconds=300
                )

                parsed = extract_json(final_text)
                if not parsed:
                    # Also check if REVIEW.md was created and contains JSON or structured summary
                    review_file = self.sandbox.workspace_path / "REVIEW.md"
                    if review_file.exists():
                        parsed = extract_json(review_file.read_text(encoding="utf-8"))

                if parsed:
                    summary = ReviewSummary(
                        summary=parsed.get("summary", "Implemented changes for task."),
                        risk_assessment=parsed.get("risk_assessment", "Unknown"),
                        test_verification=parsed.get("test_verification", f"Verified ({test_result.test_runner})"),
                        files_changed=parsed.get("files_changed", []),
                        suggested_commit_message=parsed.get("suggested_commit_message", f"feat: solve {self.task.title}"),
                        suggested_pr_title=parsed.get("suggested_pr_title", f"feat: {self.task.title}"),
                        suggested_pr_body=parsed.get("suggested_pr_body", f"Automated PR solving {self.task.title}\n\n{self.task.description}")
                    )
                    self._write_review_md(summary)
                    await event_bus.emit_log(
                        task_id=self.task.id,
                        message=f"🔍 Review complete via unified AGY runner! Risk: {summary.risk_assessment}",
                        role=AgentRole.REVIEWER.value,
                        event_type=EventType.LOG,
                        data={"summary": summary.model_dump()},
                        run_id=run_id
                    )
                    return summary
            except Exception as e:
                await event_bus.emit_log(
                    task_id=self.task.id,
                    run_id=run_id,
                    message=f"ℹ️ agy reviewer session unavailable ({e}). Falling back to direct LLM reviewer...",
                    role=AgentRole.REVIEWER.value,
                    event_type=EventType.LOG
                )

        # 2. Fallback to direct LLM call
        try:
            response = await call_llm_with_fallback(
                primary_model=self.model,
                messages=[
                    {"role": "system", "content": REVIEWER_FALLBACK_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                task_id=self.task.id,
                role=AgentRole.REVIEWER.value,
                run_id=run_id,
                temperature=0.2,
                max_tokens=2000
            )
            parsed = extract_json(response.choices[0].message.content)
            if parsed:
                summary = ReviewSummary(
                    summary=parsed.get("summary", "Implemented changes for task."),
                    risk_assessment=parsed.get("risk_assessment", "Unknown"),
                    test_verification=parsed.get("test_verification", "All tests passed."),
                    files_changed=parsed.get("files_changed", []),
                    suggested_commit_message=parsed.get("suggested_commit_message", f"feat: solve {self.task.title}"),
                    suggested_pr_title=parsed.get("suggested_pr_title", f"feat: {self.task.title}"),
                    suggested_pr_body=parsed.get("suggested_pr_body", f"Automated PR solving {self.task.title}\n\n{self.task.description}")
                )
                self._write_review_md(summary)
                await event_bus.emit_log(
                    task_id=self.task.id,
                    message=f"🔍 Review complete! Risk: {summary.risk_assessment}",
                    role=AgentRole.REVIEWER.value,
                    event_type=EventType.LOG,
                    data={"summary": summary.model_dump()},
                    run_id=run_id
                )
                return summary

        except Exception as e:
            await event_bus.emit_log(
                task_id=self.task.id,
                message=f"Reviewer notice: using default review summary ({str(e)})",
                role=AgentRole.REVIEWER.value,
                event_type=EventType.LOG,
                run_id=run_id
            )

        # Default fallback summary. No reviewer ran, so the risk is unassessed, not low:
        # the operator reads this field when deciding whether to push.
        summary = ReviewSummary(
            summary=f"Completed automated implementation for: {self.task.title}",
            risk_assessment="Unknown (automated review unavailable)",
            test_verification=f"Automated test runner executed. Passed: {test_result.passed}",
            files_changed=[],
            suggested_commit_message=f"feat: resolve {self.task.title}",
            suggested_pr_title=f"feat: {self.task.title}",
            suggested_pr_body=f"## Overview\n{self.task.description}\n\n## Verification\nPassed tests: {test_result.passed_tests}/{test_result.total_tests}"
        )
        self._write_review_md(summary)
        return summary
