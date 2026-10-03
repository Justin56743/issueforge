from unittest.mock import AsyncMock, patch
import pytest

from issueforge.agents.reviewer import ReviewerAgent
from issueforge.core.models import PlatformType, ReviewSummary, Task, TaskType, TestResult
from issueforge.core.sandbox import NativeSandbox


def create_mock_task():
    return Task(
        id="test-reviewer-task",
        title="Add JWT auth",
        description="Implement token auth",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        working_branch="forge/issue-1"
    )


@pytest.mark.asyncio
async def test_reviewer_with_agy_runner(tmp_path):
    sandbox = NativeSandbox("test-reviewer-sandbox")
    sandbox.setup()

    task = create_mock_task()
    reviewer = ReviewerAgent(task=task, sandbox=sandbox)

    mock_json = """{
  "summary": "Implemented JWT validation middleware.",
  "risk_assessment": "Low",
  "test_verification": "All 10 tests passed.",
  "files_changed": ["auth.py"],
  "suggested_commit_message": "feat(auth): add JWT validation",
  "suggested_pr_title": "feat: add JWT authentication",
  "suggested_pr_body": "## Summary\\nAdded JWT auth"
}"""

    test_res = TestResult(
        passed=True,
        total_tests=10,
        passed_tests=10,
        failed_tests=0,
        test_runner="pytest",
        stdout="10 passed in 0.2s",
        stderr=""
    )

    with patch("issueforge.agents.reviewer.AgySessionRunner.run_prompt", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (True, f"```json\n{mock_json}\n```")

        summary = await reviewer.execute(
            diff_stat="1 file changed, 20 insertions(+)",
            diff_content="+ def verify_token(): pass",
            test_result=test_res
        )

        assert isinstance(summary, ReviewSummary)
        assert summary.risk_assessment == "Low"
        assert summary.suggested_pr_title == "feat: add JWT authentication"

        # Verify REVIEW.md was created in sandbox workspace
        review_file = sandbox.workspace_path / "REVIEW.md"
        assert review_file.exists()
        content = review_file.read_text(encoding="utf-8")
        assert "Code Review & Security Assessment" in content
        assert "**Risk Level:** Low" in content

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_reviewer_fallback_to_llm(tmp_path):
    sandbox = NativeSandbox("test-reviewer-fallback")
    sandbox.setup()

    task = create_mock_task()
    reviewer = ReviewerAgent(task=task, sandbox=sandbox)

    test_res = TestResult(
        passed=True,
        total_tests=5,
        passed_tests=5,
        failed_tests=0,
        test_runner="pytest",
        stdout="5 passed",
        stderr=""
    )

    fallback_json = """{
  "summary": "Fallback summary review.",
  "risk_assessment": "Medium",
  "test_verification": "5 tests passing.",
  "files_changed": ["utils.py"],
  "suggested_commit_message": "fix: update utils",
  "suggested_pr_title": "fix: update utils",
  "suggested_pr_body": "Overview of fallback"
}"""

    from types import SimpleNamespace
    mock_llm_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=fallback_json))]
    )

    with patch("issueforge.agents.reviewer.AgySessionRunner.run_prompt", side_effect=Exception("agy CLI unavailable")), \
         patch("issueforge.agents.reviewer.call_llm_with_fallback", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = mock_llm_resp

        summary = await reviewer.execute(
            diff_stat="1 file changed",
            diff_content="+ x = 1",
            test_result=test_res
        )

        assert summary.risk_assessment == "Medium"
        assert summary.suggested_commit_message == "fix: update utils"

        review_file = sandbox.workspace_path / "REVIEW.md"
        assert review_file.exists()
        assert "**Risk Level:** Medium" in review_file.read_text(encoding="utf-8")

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_failed_review_reports_unknown_risk_not_low():
    """When neither agy nor the API produced a review, "Low" told the operator a diff
    nobody looked at was safe to push."""
    sandbox = NativeSandbox("test-reviewer-unavailable")
    sandbox.setup()
    reviewer = ReviewerAgent(task=create_mock_task(), sandbox=sandbox)

    with patch("issueforge.agents.reviewer.AgySessionRunner.run_prompt", new=AsyncMock(return_value=(False, ""))), \
         patch("issueforge.agents.reviewer.call_llm_with_fallback", new=AsyncMock(side_effect=RuntimeError("quota"))):
        summary = await reviewer.execute("1 file changed", "diff --git a/x b/x", TestResult(passed=True))

    assert summary.risk_assessment.startswith("Unknown")
    sandbox.cleanup()
