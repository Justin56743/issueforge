import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import JSON, Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from issueforge.config import settings
from issueforge.core.models import (
    AgentRole,
    EventType,
    PipelineRun,
    PlatformType,
    QuestionStatus,
    ReviewSummary,
    Task,
    TaskCollaborators,
    TaskEvent,
    TaskLearning,
    TaskQuestion,
    TaskStatus,
    TaskSubtask,
    TaskType,
    utc_now,
)


class Base(DeclarativeBase):
    pass


class TaskDB(Base):
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    task_type = Column(String(32), default=TaskType.ISSUE.value)
    platform = Column(String(32), default=PlatformType.GITHUB.value)
    repo_url = Column(String(512), nullable=False)
    repo_name = Column(String(255), nullable=False)
    base_branch = Column(String(128), default="main")
    working_branch = Column(String(128), nullable=False)
    status = Column(String(32), default=TaskStatus.PENDING_APPROVAL.value, index=True)

    # Dynamic target branch selection
    target_branch_candidates_json = Column(Text, nullable=True)
    selected_target_branch = Column(String(128), nullable=True)
    target_branch_confirmed = Column(Boolean, default=False)

    # External metadata & discussions
    issue_number = Column(Integer, nullable=True)
    pr_number = Column(Integer, nullable=True)
    sender = Column(String(128), nullable=True)
    pr_url = Column(String(512), nullable=True)
    labels_json = Column(Text, nullable=True)
    priority = Column(String(64), nullable=True)
    comments_context = Column(Text, nullable=True)

    # Project Dossier & Multi-Sandbox
    collaborators_json = Column(Text, nullable=True)
    subtasks_json = Column(Text, nullable=True)
    runs_json = Column(Text, nullable=True)
    active_run_id = Column(String(64), nullable=True)

    custom_instructions = Column(Text, nullable=True)
    plan = Column(Text, nullable=True)
    diff_stat = Column(Text, nullable=True)
    diff_content = Column(Text, nullable=True)
    test_summary = Column(Text, nullable=True)
    review_summary_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    telegram_message_id = Column(Integer, nullable=True)
    telegram_chat_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    completed_at = Column(DateTime, nullable=True)

    events = relationship("TaskEventDB", back_populates="task", cascade="all, delete-orphan")
    questions = relationship("TaskQuestionDB", back_populates="task", cascade="all, delete-orphan")

    def to_pydantic(self) -> Task:
        review_summary = None
        if self.review_summary_json:
            try:
                review_summary = ReviewSummary.model_validate_json(self.review_summary_json)
            except Exception:
                pass

        target_candidates = []
        if self.target_branch_candidates_json:
            try:
                target_candidates = json.loads(self.target_branch_candidates_json)
            except Exception:
                pass

        labels = []
        if self.labels_json:
            try:
                labels = json.loads(self.labels_json)
            except Exception:
                pass

        collaborators = TaskCollaborators()
        if self.collaborators_json:
            try:
                collaborators = TaskCollaborators.model_validate_json(self.collaborators_json)
            except Exception:
                pass

        subtasks = []
        if self.subtasks_json:
            try:
                raw_subs = json.loads(self.subtasks_json)
                subtasks = [TaskSubtask.model_validate(s) for s in raw_subs]
            except Exception:
                pass

        runs = []
        if self.runs_json:
            try:
                raw_runs = json.loads(self.runs_json)
                runs = [PipelineRun.model_validate(r) for r in raw_runs]
            except Exception:
                pass

        questions_pydantic = [q.to_pydantic() for q in (self.questions or [])]

        return Task(
            id=self.id,
            title=self.title,
            description=self.description,
            task_type=TaskType(self.task_type),
            platform=PlatformType(self.platform),
            repo_url=self.repo_url,
            repo_name=self.repo_name,
            base_branch=self.base_branch,
            working_branch=self.working_branch,
            status=TaskStatus(self.status),
            target_branch_candidates=target_candidates,
            selected_target_branch=self.selected_target_branch,
            target_branch_confirmed=bool(self.target_branch_confirmed),
            issue_number=self.issue_number,
            pr_number=self.pr_number,
            sender=self.sender,
            pr_url=self.pr_url,
            labels=labels,
            priority=self.priority,
            comments_context=self.comments_context,
            collaborators=collaborators,
            subtasks=subtasks,
            runs=runs,
            active_run_id=self.active_run_id,
            custom_instructions=self.custom_instructions,
            plan=self.plan,
            diff_stat=self.diff_stat,
            diff_content=self.diff_content,
            test_summary=self.test_summary,
            review_summary=review_summary,
            error_message=self.error_message,
            questions=questions_pydantic,
            telegram_message_id=self.telegram_message_id,
            telegram_chat_id=self.telegram_chat_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            completed_at=self.completed_at,
        )



class TaskEventDB(Base):
    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), ForeignKey("tasks.id"), nullable=False, index=True)
    run_id = Column(String(32), nullable=True, index=True)
    event_type = Column(String(32), default=EventType.LOG.value)
    role = Column(String(32), nullable=True)
    message = Column(Text, nullable=False)
    data_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    task = relationship("TaskDB", back_populates="events")

    def to_pydantic(self) -> TaskEvent:
        data = None
        if self.data_json:
            try:
                data = json.loads(self.data_json)
            except Exception:
                pass
        return TaskEvent(
            id=self.id,
            task_id=self.task_id,
            run_id=self.run_id,
            event_type=EventType(self.event_type),
            role=AgentRole(self.role) if self.role else None,
            message=self.message,
            data=data,
            created_at=self.created_at,
        )


class TaskQuestionDB(Base):
    __tablename__ = "task_questions"

    id = Column(String(64), primary_key=True, index=True)
    task_id = Column(String(64), ForeignKey("tasks.id"), nullable=False, index=True)
    question = Column(Text, nullable=False)
    context = Column(Text, nullable=True)
    options_json = Column(Text, nullable=True)
    selected_option = Column(String(255), nullable=True)
    answer = Column(Text, nullable=True)
    status = Column(String(32), default=QuestionStatus.PENDING.value)
    created_at = Column(DateTime, default=utc_now)
    answered_at = Column(DateTime, nullable=True)

    task = relationship("TaskDB", back_populates="questions")

    def to_pydantic(self) -> TaskQuestion:
        options = []
        if self.options_json:
            try:
                options = json.loads(self.options_json)
            except Exception:
                pass
        return TaskQuestion(
            id=self.id,
            task_id=self.task_id,
            question=self.question,
            context=self.context,
            options=options,
            selected_option=self.selected_option,
            answer=self.answer,
            status=QuestionStatus(self.status),
            created_at=self.created_at,
            answered_at=self.answered_at,
        )


class TaskLearningDB(Base):
    __tablename__ = "task_learnings"

    id = Column(String(64), primary_key=True, index=True)
    task_id = Column(String(64), nullable=True, index=True)
    topic = Column(String(255), nullable=False, index=True)
    summary = Column(Text, nullable=False)
    solution_pattern = Column(Text, nullable=False)
    tags_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    def to_pydantic(self) -> TaskLearning:
        tags = []
        if self.tags_json:
            try:
                tags = json.loads(self.tags_json)
            except Exception:
                pass
        return TaskLearning(
            id=self.id,
            task_id=self.task_id,
            topic=self.topic,
            summary=self.summary,
            solution_pattern=self.solution_pattern,
            tags=tags,
            created_at=self.created_at,
        )


class TaskGraphEdgeDB(Base):
    """A directional [[wikilink]] edge extracted from a task dossier's README.

    Indexed so the Obsidian-style graph can be traversed without walking the filesystem.
    `target_node` is a wikilink node name, not necessarily an existing file — unresolved
    targets are meaningful (two tasks pointing at the same issue or branch are related).
    """

    __tablename__ = "task_graph_edges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # No ForeignKey on purpose: dossier folders (and therefore graph edges) legitimately
    # exist for task ids absent from `tasks` — `list_all_dossiers` renders exactly those.
    # Matches the existing TaskLearningDB.task_id precedent.
    source_task_id = Column(String(64), nullable=False, index=True)
    target_node = Column(String(512), nullable=False, index=True)
    edge_type = Column(String(32), default="wikilink")
    created_at = Column(DateTime, default=utc_now)


engine = None
async_session_factory = None


def get_db_url() -> str:
    settings.ensure_directories()
    return f"sqlite+aiosqlite:///{settings.forge_db_path.resolve()}"


async def _migrate_sqlite_schema(conn) -> None:
    """Ensure all model columns exist in existing SQLite tables (automatic column addition)."""
    expected_columns = {
        "tasks": {
            "target_branch_candidates_json": "TEXT",
            "selected_target_branch": "VARCHAR(128)",
            "target_branch_confirmed": "BOOLEAN DEFAULT 0",
            "issue_number": "INTEGER",
            "pr_number": "INTEGER",
            "sender": "VARCHAR(128)",
            "pr_url": "VARCHAR(512)",
            "labels_json": "TEXT",
            "priority": "VARCHAR(64)",
            "comments_context": "TEXT",
            "collaborators_json": "TEXT",
            "subtasks_json": "TEXT",
            "runs_json": "TEXT",
            "active_run_id": "VARCHAR(64)",
            "custom_instructions": "TEXT",
            "plan": "TEXT",
            "diff_stat": "TEXT",
            "diff_content": "TEXT",
            "test_summary": "TEXT",
            "review_summary_json": "TEXT",
            "error_message": "TEXT",
            "telegram_message_id": "INTEGER",
            "telegram_chat_id": "INTEGER",
            "completed_at": "DATETIME",
        },
        "task_questions": {
            "context": "TEXT",
            "options_json": "TEXT",
            "selected_option": "VARCHAR(255)",
            "answer": "TEXT",
            "status": "VARCHAR(32)",
            "answered_at": "DATETIME",
        },
        "task_learnings": {
            "task_id": "VARCHAR(64)",
            "tags_json": "TEXT",
        },
        "task_events": {
            "run_id": "VARCHAR(32)",
        }
    }

    from sqlalchemy import text
    for table_name, cols in expected_columns.items():
        try:
            res = await conn.execute(text(f"PRAGMA table_info({table_name});"))
            existing_cols = {row[1] for row in res.fetchall()}
            for col_name, col_type in cols.items():
                if col_name not in existing_cols:
                    await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type};"))
        except Exception:
            pass


async def init_db() -> None:
    global engine, async_session_factory
    settings.ensure_directories()
    engine = create_async_engine(get_db_url(), echo=settings.forge_debug)
    async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_schema(conn)



async def close_db() -> None:
    global engine, async_session_factory
    if engine is not None:
        await engine.dispose()
        engine = None
        async_session_factory = None


async def get_session() -> AsyncSession:
    global async_session_factory
    if async_session_factory is None:
        await init_db()
    return async_session_factory()


async def save_task(task: Task) -> Task:
    async with await get_session() as session:
        async with session.begin():
            review_json = task.review_summary.model_dump_json() if task.review_summary else None
            candidates_json = json.dumps(task.target_branch_candidates) if task.target_branch_candidates else None
            labels_json = json.dumps(task.labels) if task.labels else None
            collaborators_json = task.collaborators.model_dump_json() if task.collaborators else None
            subtasks_json = json.dumps([s.model_dump(mode="json") for s in task.subtasks]) if task.subtasks else None
            runs_json = json.dumps([r.model_dump(mode="json") for r in task.runs]) if task.runs else None

            task_db = await session.get(TaskDB, task.id)
            if not task_db:
                task_db = TaskDB(
                    id=task.id,
                    title=task.title,
                    description=task.description,
                    task_type=task.task_type.value,
                    platform=task.platform.value,
                    repo_url=task.repo_url,
                    repo_name=task.repo_name,
                    base_branch=task.base_branch,
                    working_branch=task.working_branch,
                    status=task.status.value,
                    target_branch_candidates_json=candidates_json,
                    selected_target_branch=task.selected_target_branch,
                    target_branch_confirmed=task.target_branch_confirmed,
                    issue_number=task.issue_number,
                    pr_number=task.pr_number,
                    sender=task.sender,
                    pr_url=task.pr_url,
                    labels_json=labels_json,
                    priority=task.priority,
                    comments_context=task.comments_context,
                    collaborators_json=collaborators_json,
                    subtasks_json=subtasks_json,
                    runs_json=runs_json,
                    active_run_id=task.active_run_id,
                    custom_instructions=task.custom_instructions,
                    plan=task.plan,
                    diff_stat=task.diff_stat,
                    diff_content=task.diff_content,
                    test_summary=task.test_summary,
                    review_summary_json=review_json,
                    error_message=task.error_message,
                    telegram_message_id=task.telegram_message_id,
                    telegram_chat_id=task.telegram_chat_id,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                    completed_at=task.completed_at,
                )
                session.add(task_db)
            else:
                task_db.title = task.title
                task_db.description = task.description
                task_db.status = task.status.value
                task_db.base_branch = task.base_branch
                task_db.working_branch = task.working_branch
                task_db.target_branch_candidates_json = candidates_json
                task_db.selected_target_branch = task.selected_target_branch
                task_db.target_branch_confirmed = task.target_branch_confirmed
                task_db.labels_json = labels_json
                task_db.priority = task.priority
                task_db.comments_context = task.comments_context
                task_db.collaborators_json = collaborators_json
                task_db.subtasks_json = subtasks_json
                task_db.runs_json = runs_json
                task_db.active_run_id = task.active_run_id
                task_db.custom_instructions = task.custom_instructions
                task_db.plan = task.plan
                task_db.diff_stat = task.diff_stat
                task_db.diff_content = task.diff_content
                task_db.test_summary = task.test_summary
                task_db.review_summary_json = review_json
                task_db.error_message = task.error_message
                task_db.pr_url = task.pr_url
                task_db.telegram_message_id = task.telegram_message_id
                task_db.telegram_chat_id = task.telegram_chat_id
                task_db.updated_at = utc_now()
                task_db.completed_at = task.completed_at
        return task


async def get_task(task_id: str) -> Optional[Task]:
    async with await get_session() as session:
        from sqlalchemy.orm import selectinload
        stmt = select(TaskDB).where(TaskDB.id == task_id).options(selectinload(TaskDB.questions))
        result = await session.execute(stmt)
        task_db = result.scalar_one_or_none()
        if task_db:
            return task_db.to_pydantic()
        return None


async def list_tasks(limit: int = 50, offset: int = 0) -> List[Task]:
    async with await get_session() as session:
        from sqlalchemy.orm import selectinload
        stmt = select(TaskDB).options(selectinload(TaskDB.questions)).order_by(TaskDB.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return [row.to_pydantic() for row in result.scalars().all()]


def normalize_repo_url(url: Optional[str]) -> str:
    """Normalize git repository URLs to enable exact equality matching."""
    if not url:
        return ""
    clean = url.strip().rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    return clean


async def find_existing_task(
    repo_url: str,
    platform: PlatformType,
    issue_number: Optional[int] = None,
    pr_number: Optional[int] = None
) -> Optional[Task]:
    """Find an active/existing task matching the platform, repo, and issue/PR number."""
    if issue_number is None and pr_number is None:
        return None

    norm_target_url = normalize_repo_url(repo_url)
    async with await get_session() as session:
        from sqlalchemy.orm import selectinload
        stmt = select(TaskDB).options(selectinload(TaskDB.questions)).where(TaskDB.platform == platform.value)
        if issue_number is not None:
            stmt = stmt.where(TaskDB.issue_number == issue_number)
        if pr_number is not None:
            stmt = stmt.where(TaskDB.pr_number == pr_number)

        result = await session.execute(stmt)
        candidates = result.scalars().all()
        for c in candidates:
            if normalize_repo_url(c.repo_url) == norm_target_url:
                return c.to_pydantic()
    return None


async def delete_task(task_id: str) -> bool:
    """Delete a task, cascade its database records, and remove its folder and workspace."""
    import shutil
    try:
        task_dir = (settings.forge_tasks_root / task_id).resolve()
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
    except Exception:
        pass

    try:
        ws_dir = (settings.forge_workspace_root / task_id).resolve()
        if ws_dir.exists():
            shutil.rmtree(ws_dir, ignore_errors=True)
    except Exception:
        pass

    async with await get_session() as session:
        async with session.begin():
            await session.execute(delete(TaskEventDB).where(TaskEventDB.task_id == task_id))
            await session.execute(delete(TaskQuestionDB).where(TaskQuestionDB.task_id == task_id))
            await session.execute(delete(TaskLearningDB).where(TaskLearningDB.task_id == task_id))
            await session.execute(delete(TaskGraphEdgeDB).where(TaskGraphEdgeDB.source_task_id == task_id))
            res = await session.execute(delete(TaskDB).where(TaskDB.id == task_id))
            return res.rowcount > 0


async def replace_task_graph_edges(
    task_id: str, targets: List[str], edge_type: str = "wikilink"
) -> int:
    """Atomically replace this task's outgoing graph edges. Returns the edge count written."""
    unique_targets = sorted({t.strip() for t in targets if t and t.strip()})
    async with await get_session() as session:
        async with session.begin():
            await session.execute(
                delete(TaskGraphEdgeDB).where(
                    TaskGraphEdgeDB.source_task_id == task_id,
                    TaskGraphEdgeDB.edge_type == edge_type,
                )
            )
            session.add_all([
                TaskGraphEdgeDB(source_task_id=task_id, target_node=target, edge_type=edge_type)
                for target in unique_targets
            ])
    return len(unique_targets)


async def get_task_graph_edges(task_id: str) -> List[str]:
    """Nodes this task links out to."""
    async with await get_session() as session:
        result = await session.execute(
            select(TaskGraphEdgeDB.target_node)
            .where(TaskGraphEdgeDB.source_task_id == task_id)
            .order_by(TaskGraphEdgeDB.target_node)
        )
        return [row[0] for row in result.all()]


async def get_graph_backlinks(target_node: str) -> List[str]:
    """Task ids that link to the given node — the backlink half of the graph."""
    async with await get_session() as session:
        result = await session.execute(
            select(TaskGraphEdgeDB.source_task_id)
            .where(TaskGraphEdgeDB.target_node == target_node)
            .distinct()
        )
        return [row[0] for row in result.all()]


async def save_event(event: TaskEvent) -> TaskEvent:
    async with await get_session() as session:
        async with session.begin():
            event_db = TaskEventDB(
                task_id=event.task_id,
                run_id=event.run_id,
                event_type=event.event_type.value,
                role=event.role.value if event.role else None,
                message=event.message,
                data_json=json.dumps(event.data) if event.data else None,
                created_at=event.created_at,
            )
            session.add(event_db)
            await session.flush()
            event.id = event_db.id
        return event


async def get_task_events(task_id: str, run_id: Optional[str] = None) -> List[TaskEvent]:
    async with await get_session() as session:
        stmt = select(TaskEventDB).where(TaskEventDB.task_id == task_id)
        if run_id:
            stmt = stmt.where(TaskEventDB.run_id == run_id)
        stmt = stmt.order_by(TaskEventDB.created_at.asc())
        result = await session.execute(stmt)
        return [row.to_pydantic() for row in result.scalars().all()]


async def save_question(question: TaskQuestion) -> TaskQuestion:
    async with await get_session() as session:
        async with session.begin():
            q_db = await session.get(TaskQuestionDB, question.id)
            options_json = json.dumps(question.options) if question.options else None
            if not q_db:
                q_db = TaskQuestionDB(
                    id=question.id,
                    task_id=question.task_id,
                    question=question.question,
                    context=question.context,
                    options_json=options_json,
                    selected_option=question.selected_option,
                    answer=question.answer,
                    status=question.status.value,
                    created_at=question.created_at,
                    answered_at=question.answered_at,
                )
                session.add(q_db)
            else:
                q_db.question = question.question
                q_db.context = question.context
                q_db.options_json = options_json
                q_db.selected_option = question.selected_option
                q_db.answer = question.answer
                q_db.status = question.status.value
                q_db.answered_at = question.answered_at
        return question


async def get_task_questions(task_id: str) -> List[TaskQuestion]:
    async with await get_session() as session:
        stmt = select(TaskQuestionDB).where(TaskQuestionDB.task_id == task_id).order_by(TaskQuestionDB.created_at.asc())
        result = await session.execute(stmt)
        return [row.to_pydantic() for row in result.scalars().all()]


async def answer_question(task_id: str, question_id: str, answer_text: str, selected_option: Optional[str] = None) -> Optional[TaskQuestion]:
    async with await get_session() as session:
        async with session.begin():
            q_db = await session.get(TaskQuestionDB, question_id)
            if not q_db or q_db.task_id != task_id:
                return None
            q_db.answer = answer_text
            q_db.selected_option = selected_option or answer_text
            q_db.status = QuestionStatus.ANSWERED.value
            q_db.answered_at = utc_now()
            await session.flush()
            return q_db.to_pydantic()


async def save_learning(learning: TaskLearning) -> TaskLearning:
    async with await get_session() as session:
        async with session.begin():
            tags_json = json.dumps(learning.tags) if learning.tags else None
            l_db = await session.get(TaskLearningDB, learning.id)
            if not l_db:
                l_db = TaskLearningDB(
                    id=learning.id,
                    task_id=learning.task_id,
                    topic=learning.topic,
                    summary=learning.summary,
                    solution_pattern=learning.solution_pattern,
                    tags_json=tags_json,
                    created_at=learning.created_at,
                )
                session.add(l_db)
            else:
                l_db.topic = learning.topic
                l_db.summary = learning.summary
                l_db.solution_pattern = learning.solution_pattern
                l_db.tags_json = tags_json
        return learning


async def list_learnings(limit: int = 50, offset: int = 0) -> List[TaskLearning]:
    async with await get_session() as session:
        stmt = select(TaskLearningDB).order_by(TaskLearningDB.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return [row.to_pydantic() for row in result.scalars().all()]


async def get_task_learnings(task_id: str) -> List[TaskLearning]:
    async with await get_session() as session:
        stmt = select(TaskLearningDB).where(TaskLearningDB.task_id == task_id).order_by(TaskLearningDB.created_at.asc())
        result = await session.execute(stmt)
        return [row.to_pydantic() for row in result.scalars().all()]

