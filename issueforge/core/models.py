import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Task and run ids become directory names under the vault. Anything other than one plain
# path segment ("..", "a/b", "") would let a request read, write or delete outside it.
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class UnsafeIdError(ValueError):
    """An id that cannot be used as a single path segment."""


def safe_path_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise UnsafeIdError(f"Invalid id: {value!r}")
    return value


class TaskStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PLANNING = "PLANNING"
    CODING = "CODING"
    TESTING = "TESTING"
    AWAITING_INPUT = "AWAITING_INPUT"
    AWAITING_BRANCH_SELECTION = "AWAITING_BRANCH_SELECTION"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    PUSHING = "PUSHING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REVISION_REQUESTED = "REVISION_REQUESTED"


class TaskType(str, Enum):
    ISSUE = "ISSUE"
    PULL_REQUEST = "PULL_REQUEST"
    SPRINT_ITEM = "SPRINT_ITEM"
    MANUAL = "MANUAL"


class PlatformType(str, Enum):
    GITHUB = "GITHUB"
    GITLAB = "GITLAB"
    MANUAL = "MANUAL"


class AgentRole(str, Enum):
    ORCHESTRATOR = "ORCHESTRATOR"
    PLANNER = "PLANNER"
    CODER = "CODER"
    TESTER = "TESTER"
    REVIEWER = "REVIEWER"


class EventType(str, Enum):
    LOG = "LOG"
    STEP = "STEP"
    DIFF = "DIFF"
    TEST_RUN = "TEST_RUN"
    TELEGRAM_MSG = "TELEGRAM_MSG"
    STATUS_CHANGE = "STATUS_CHANGE"
    QUESTION = "QUESTION"
    LEARNING = "LEARNING"
    ERROR = "ERROR"


class QuestionStatus(str, Enum):
    PENDING = "PENDING"
    ANSWERED = "ANSWERED"
    SKIPPED = "SKIPPED"


class TaskQuestion(BaseModel):
    id: str
    task_id: str
    question: str
    context: Optional[str] = None
    options: List[str] = Field(default_factory=list)
    selected_option: Optional[str] = None
    answer: Optional[str] = None
    status: QuestionStatus = QuestionStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    answered_at: Optional[datetime] = None


class TaskLearning(BaseModel):
    id: str
    task_id: Optional[str] = None
    topic: str
    summary: str
    solution_pattern: str
    tags: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class TestResult(BaseModel):
    __test__ = False  # Prevent pytest from treating this model as a test class
    passed: bool
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    test_runner: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    failure_details: Optional[str] = None


class FileDiff(BaseModel):
    file_path: str
    diff_text: str
    is_new: bool = False
    is_deleted: bool = False


class ReviewSummary(BaseModel):
    summary: str
    risk_assessment: str
    test_verification: str
    files_changed: List[str] = Field(default_factory=list)
    suggested_commit_message: str
    suggested_pr_title: str
    suggested_pr_body: str
    suggested_priority: Optional[str] = None
    suggested_labels: List[str] = Field(default_factory=list)


class TaskEvent(BaseModel):
    id: Optional[int] = None
    task_id: str
    run_id: Optional[str] = None
    event_type: EventType
    role: Optional[AgentRole] = None
    message: str
    data: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=utc_now)


class TaskCollaborators(BaseModel):
    author: Optional[str] = None
    assignees: List[str] = Field(default_factory=list)
    reviewers: List[str] = Field(default_factory=list)
    participants: List[str] = Field(default_factory=list)


class TaskSubtask(BaseModel):
    id: str
    title: str
    completed: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class PipelineRun(BaseModel):
    run_id: str
    attempt_number: int = 1
    status: TaskStatus = TaskStatus.APPROVED
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    failure_stage: Optional[str] = None
    error_message: Optional[str] = None
    test_summary: Optional[str] = None
    diff_stat: Optional[str] = None
    commit_hash: Optional[str] = None
    sandbox_dir: Optional[str] = None


class Task(BaseModel):
    id: str
    title: str
    description: str
    task_type: TaskType = TaskType.ISSUE
    platform: PlatformType = PlatformType.GITLAB
    repo_url: str
    repo_name: str
    base_branch: str = "main"
    working_branch: str
    status: TaskStatus = TaskStatus.PENDING_APPROVAL

    # Target branch dynamic selection
    target_branch_candidates: List[str] = Field(default_factory=list)
    selected_target_branch: Optional[str] = None
    # True only once a human explicitly chose the target branch. A branch that merely
    # happens to be set (defaulted from base_branch at creation) does not count — that
    # distinction is what makes the AWAITING_BRANCH_SELECTION gate meaningful.
    target_branch_confirmed: bool = False

    # External metadata & discussions
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    sender: Optional[str] = None
    pr_url: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    priority: Optional[str] = None
    comments_context: Optional[str] = None

    # Project Dossier & Collaborators
    collaborators: TaskCollaborators = Field(default_factory=TaskCollaborators)
    subtasks: List[TaskSubtask] = Field(default_factory=list)

    # Multi-Sandbox Run History
    runs: List[PipelineRun] = Field(default_factory=list)
    active_run_id: Optional[str] = None

    # Instructions and results
    custom_instructions: Optional[str] = None
    plan: Optional[str] = None
    diff_stat: Optional[str] = None
    diff_content: Optional[str] = None
    test_summary: Optional[str] = None
    review_summary: Optional[ReviewSummary] = None
    error_message: Optional[str] = None

    # Interactive Q&A
    questions: List[TaskQuestion] = Field(default_factory=list)

    # Telegram tracking
    telegram_message_id: Optional[int] = None
    telegram_chat_id: Optional[int] = None

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None



class TaskCreateRequest(BaseModel):
    title: str
    description: str
    repo_url: str
    base_branch: str = "main"
    selected_target_branch: Optional[str] = None
    task_type: TaskType = TaskType.MANUAL
    platform: PlatformType = PlatformType.GITLAB
    issue_number: Optional[int] = None
    custom_instructions: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    priority: Optional[str] = None

