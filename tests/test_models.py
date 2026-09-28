import pytest
from datetime import datetime
from issueforge.core.database import get_task, init_db, list_tasks, save_event, save_task
from issueforge.core.models import (
    AgentRole,
    EventType,
    PlatformType,
    ReviewSummary,
    Task,
    TaskEvent,
    TaskStatus,
    TaskType,
    TestResult,
)


@pytest.mark.asyncio
async def test_models_and_db_persistence():
    await init_db()

    task = Task(
        id="test-task-100",
        title="Implement OAuth2 Authentication",
        description="Add JWT token generation and validation middleware.",
        task_type=TaskType.ISSUE,
        platform=PlatformType.GITHUB,
        repo_url="https://github.com/org/repo.git",
        repo_name="org/repo",
        base_branch="main",
        working_branch="forge/issue-100",
        status=TaskStatus.PENDING_APPROVAL,
        issue_number=100,
        sender="anuj"
    )

    saved = await save_task(task)
    assert saved.id == "test-task-100"

    fetched = await get_task("test-task-100")
    assert fetched is not None
    assert fetched.title == "Implement OAuth2 Authentication"
    assert fetched.status == TaskStatus.PENDING_APPROVAL

    # Update with review summary
    review = ReviewSummary(
        summary="Added JWT token generation and validation middleware",
        risk_assessment="Low",
        test_verification="All unit tests passed",
        files_changed=["src/auth.py", "tests/test_auth.py"],
        suggested_commit_message="feat(auth): add JWT auth endpoints",
        suggested_pr_title="feat: JWT authentication support",
        suggested_pr_body="Adds JWT authentication support."
    )
    fetched.status = TaskStatus.AWAITING_CONFIRMATION
    fetched.review_summary = review
    await save_task(fetched)

    refetched = await get_task("test-task-100")
    assert refetched.status == TaskStatus.AWAITING_CONFIRMATION
    assert refetched.review_summary is not None
    assert refetched.review_summary.risk_assessment == "Low"

    # Save and verify event
    event = TaskEvent(
        task_id="test-task-100",
        event_type=EventType.LOG,
        role=AgentRole.ORCHESTRATOR,
        message="Created plan"
    )
    saved_event = await save_event(event)
    assert saved_event.id is not None

    # Test dynamic target branch persistence
    fetched.target_branch_candidates = ["main", "develop", "staging"]
    fetched.selected_target_branch = "develop"
    await save_task(fetched)

    refetched2 = await get_task("test-task-100")
    assert refetched2.selected_target_branch == "develop"
    assert "staging" in refetched2.target_branch_candidates

    # Test TaskQuestion persistence and answer
    from issueforge.core.database import answer_question, get_task_questions, save_question, save_learning, list_learnings, get_task_learnings
    from issueforge.core.models import TaskQuestion, TaskLearning, QuestionStatus

    q = TaskQuestion(
        id="q-100",
        task_id="test-task-100",
        question="Which database engine should be used?",
        options=["PostgreSQL", "SQLite", "MySQL"],
        status=QuestionStatus.PENDING
    )
    await save_question(q)
    questions = await get_task_questions("test-task-100")
    assert len(questions) == 1
    assert questions[0].question == "Which database engine should be used?"
    assert questions[0].status == QuestionStatus.PENDING

    answered = await answer_question("test-task-100", "q-100", "PostgreSQL", "PostgreSQL")
    assert answered is not None
    assert answered.status == QuestionStatus.ANSWERED
    assert answered.answer == "PostgreSQL"

    # Test TaskLearning persistence and retrieval
    learning = TaskLearning(
        id="learn-100",
        task_id="test-task-100",
        topic="OAuth2 JWT Integration",
        summary="Used PyJWT with Bearer authentication header",
        solution_pattern="Store tokens in httpOnly cookies or Authorization Bearer header",
        tags=["auth", "jwt", "oauth2"]
    )
    await save_learning(learning)

    all_learnings = await list_learnings()
    assert any(l.id == "learn-100" for l in all_learnings)

    task_learnings = await get_task_learnings("test-task-100")
    assert len(task_learnings) >= 1
    assert task_learnings[0].topic == "OAuth2 JWT Integration"

    # Test TaskCollaborators, Subtasks, and PipelineRun persistence
    from issueforge.core.models import PipelineRun, TaskCollaborators, TaskSubtask

    fetched.collaborators = TaskCollaborators(
        author="alice",
        assignees=["bob"],
        reviewers=["charlie"],
        participants=["@alice", "@bob", "@reviewer"]
    )
    fetched.subtasks = [
        TaskSubtask(id="st-1", title="Write JWT handler", completed=True),
        TaskSubtask(id="st-2", title="Run security tests", completed=False),
    ]
    fetched.runs = [
        PipelineRun(
            run_id="run-1",
            attempt_number=1,
            status=TaskStatus.FAILED,
            failure_stage="TESTER",
            error_message="1 test failed",
            duration_seconds=14.2
        ),
        PipelineRun(
            run_id="run-2",
            attempt_number=2,
            status=TaskStatus.COMPLETED,
            duration_seconds=18.5
        )
    ]
    fetched.active_run_id = "run-2"
    await save_task(fetched)

    refetched3 = await get_task("test-task-100")
    assert refetched3.collaborators.author == "alice"
    assert "@reviewer" in refetched3.collaborators.participants
    assert len(refetched3.subtasks) == 2
    assert refetched3.subtasks[0].completed is True
    assert len(refetched3.runs) == 2
    assert refetched3.runs[0].failure_stage == "TESTER"
    assert refetched3.runs[1].status == TaskStatus.COMPLETED
    assert refetched3.active_run_id == "run-2"


