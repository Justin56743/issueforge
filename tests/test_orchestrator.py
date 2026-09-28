import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from issueforge.agents.orchestrator import SupervisoryOrchestrator
from issueforge.core.database import get_task, init_db, save_task, get_task_questions, get_task_learnings
from issueforge.core.models import (
    AgentRole,
    PlatformType,
    QuestionStatus,
    Task,
    TaskQuestion,
    TaskStatus,
    TaskType,
    TestResult,
)
from issueforge.core.queue import task_queue
from issueforge.core.sandbox import NativeSandbox


@pytest.mark.asyncio
async def test_orchestrator_triage_ambiguous(tmp_path):
    await init_db()
    sandbox = NativeSandbox("test-orch-ambiguous")
    sandbox.setup()

    task = Task(
        id="test-orch-ambiguous",
        title="Migrate database",
        description="Migrate our database system without breaking existing tables.",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        base_branch="main",
        working_branch="forge/orch-1",
        status=TaskStatus.APPROVED
    )
    await save_task(task)

    orchestrator = SupervisoryOrchestrator(sandbox=sandbox, task=task)

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(
            message=MagicMock(
                content="""{
  "needs_clarification": true,
  "confidence_score": 0.4,
  "reasoning": "Target database dialect and migration framework are unspecified",
  "suggested_priority": "Priority::High",
  "suggested_labels": ["database", "needs-info"],
  "question": {
    "question": "Which database dialect are we migrating to?",
    "context": "We need to choose between Alembic, Flyway, or raw SQL migrations",
    "options": ["PostgreSQL with Alembic", "MySQL with Flyway", "SQLite"]
  }
}"""
            )
        )
    ]

    with patch("issueforge.agents.orchestrator.AgySessionRunner.run_prompt", new=AsyncMock(return_value=(True, mock_llm_response.choices[0].message.content))), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_llm_response)):
        needs_clarification, question = await orchestrator.triage_task()
        assert needs_clarification is True
        assert question is not None
        assert "Which database dialect" in question.question
        assert len(question.options) == 3

    # Verify question saved in DB
    questions = await get_task_questions("test-orch-ambiguous")
    assert len(questions) == 1
    assert questions[0].id == question.id

    # Test answering question via task_queue
    updated_task = await task_queue.submit_question_answer(
        task_id="test-orch-ambiguous",
        question_id=question.id,
        answer_text="PostgreSQL with Alembic",
        selected_option="PostgreSQL with Alembic"
    )
    assert updated_task is not None
    assert "PostgreSQL with Alembic" in updated_task.custom_instructions

    # Re-triaging after question answered should NOT block
    needs_clarification2, _ = await orchestrator.triage_task()
    assert needs_clarification2 is False

    sandbox.cleanup()


@pytest.mark.asyncio
async def test_orchestrator_learning_and_memory(tmp_path):
    await init_db()
    sandbox = NativeSandbox("test-orch-learn")
    sandbox.setup()

    task = Task(
        id="test-orch-learn",
        title="Add FastAPI SSE Endpoint",
        description="Implement streaming SSE endpoint with proper keepalive headers.",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITLAB,
        repo_url="https://gitlab.com/org/repo.git",
        repo_name="org/repo",
        base_branch="main",
        working_branch="forge/orch-learn",
        status=TaskStatus.APPROVED
    )
    await save_task(task)

    orchestrator = SupervisoryOrchestrator(sandbox=sandbox, task=task)

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(
            message=MagicMock(
                content="""{
  "topic": "FastAPI SSE Streaming Pattern",
  "summary": "Implemented async generator with event-stream headers and keep-alive",
  "solution_pattern": "Use StreamingResponse with media_type='text/event-stream' and yield formatted data lines",
  "tags": ["fastapi", "sse", "streaming"]
}"""
            )
        )
    ]

    test_res = TestResult(
        passed=True,
        total_tests=3,
        passed_tests=3,
        failed_tests=0,
        test_runner="pytest"
    )

    with patch("issueforge.agents.orchestrator.AgySessionRunner.run_prompt", new=AsyncMock(return_value=(True, mock_llm_response.choices[0].message.content))), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_llm_response)):
        learning = await orchestrator.record_task_learning(
            plan="Architectural plan for SSE",
            diff_stat="1 file changed, 20 insertions",
            test_result=test_res
        )
        assert learning is not None
        assert learning.topic == "FastAPI SSE Streaming Pattern"

    # Query memory
    memory = await orchestrator.retrieve_harness_learnings()
    assert "FastAPI SSE Streaming Pattern" in memory

    sandbox.cleanup()
