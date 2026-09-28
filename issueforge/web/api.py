import asyncio
import json
import logging
import shlex
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from issueforge.config import settings
from issueforge.core.database import delete_task, get_task, get_task_events, list_tasks, save_task
from issueforge.core.events import event_bus
from issueforge.core.sandbox import NativeSandbox
from issueforge.core.models import (
    EventType,
    PlatformType,
    Task,
    TaskCreateRequest,
    TaskEvent,
    TaskStatus,
    TaskType,
)
from issueforge.core.poller import poller_service
from issueforge.core.queue import task_queue
from issueforge.git.github_client import GitHubClient
from issueforge.git.gitlab_client import GitLabClient

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ==========================================
# Webhook Ingestion Handlers
# ==========================================

@router.post("/api/webhooks/github")
async def handle_github_webhook(
    request: Request,
    x_github_event: str = Header(None),
    x_hub_signature_256: str = Header(None)
):
    body_bytes = await request.body()
    if not GitHubClient.verify_webhook_signature(body_bytes, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    repo_data = payload.get("repository", {})
    repo_url = repo_data.get("clone_url") or repo_data.get("html_url", "")
    default_branch = repo_data.get("default_branch", "main")
    sender = payload.get("sender", {}).get("login", "unknown")

    # 1. Issue Created
    if x_github_event == "issues" and payload.get("action") in ("opened", "reopened"):
        issue = payload.get("issue", {})
        task = await task_queue.create_and_enqueue_task(
            title=issue.get("title", "Untitled Issue"),
            description=issue.get("body", "") or "No description provided.",
            repo_url=repo_url,
            base_branch=default_branch,
            task_type=TaskType.ISSUE,
            platform=PlatformType.GITHUB,
            issue_number=issue.get("number"),
            sender=sender
        )
        return {"status": "ingested", "task_id": task.id}

    # 2. Pull Request Created
    elif x_github_event == "pull_request" and payload.get("action") in ("opened", "synchronize"):
        pr = payload.get("pull_request", {})
        task = await task_queue.create_and_enqueue_task(
            title=f"Review/Fix PR: {pr.get('title', 'Untitled PR')}",
            description=pr.get("body", "") or "No PR description.",
            repo_url=repo_url,
            base_branch=pr.get("base", {}).get("ref", default_branch),
            task_type=TaskType.PULL_REQUEST,
            platform=PlatformType.GITHUB,
            pr_number=pr.get("number"),
            sender=sender
        )
        return {"status": "ingested", "task_id": task.id}

    # 3. Comment with /forge command
    elif x_github_event == "issue_comment" and payload.get("action") == "created":
        comment = payload.get("comment", {})
        comment_body = comment.get("body", "").strip()
        if comment_body.startswith("/forge"):
            issue = payload.get("issue", {})
            instruction = comment_body.replace("/forge", "", 1).strip()
            task = await task_queue.create_and_enqueue_task(
                title=f"Comment Task: {issue.get('title')}",
                description=f"Issue context:\n{issue.get('body')}\n\nUser instruction:\n{instruction}",
                repo_url=repo_url,
                base_branch=default_branch,
                task_type=TaskType.ISSUE,
                platform=PlatformType.GITHUB,
                issue_number=issue.get("number"),
                sender=sender,
                custom_instructions=instruction
            )
            return {"status": "ingested", "task_id": task.id}

    return {"status": "ignored", "event": x_github_event}


@router.post("/api/webhooks/gitlab")
async def handle_gitlab_webhook(
    request: Request,
    x_gitlab_token: str = Header(None),
    x_gitlab_event: str = Header(None)
):
    if not GitLabClient.verify_webhook_token(x_gitlab_token):
        raise HTTPException(status_code=401, detail="Invalid GitLab webhook token.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON.")

    project = payload.get("project", {})
    repo_url = project.get("git_http_url") or project.get("web_url", "")
    default_branch = project.get("default_branch", "main")
    user = payload.get("user", {}).get("username", "unknown")
    object_kind = payload.get("object_kind")

    # Issue Hook
    if object_kind == "issue" and payload.get("object_attributes", {}).get("action") in ("open", "reopen"):
        attrs = payload.get("object_attributes", {})
        task = await task_queue.create_and_enqueue_task(
            title=attrs.get("title", "Untitled GitLab Issue"),
            description=attrs.get("description", "") or "No description.",
            repo_url=repo_url,
            base_branch=default_branch,
            task_type=TaskType.ISSUE,
            platform=PlatformType.GITLAB,
            issue_number=attrs.get("iid"),
            sender=user
        )
        return {"status": "ingested", "task_id": task.id}

    # Merge Request Hook
    elif object_kind == "merge_request" and payload.get("object_attributes", {}).get("action") in ("open", "reopen"):
        attrs = payload.get("object_attributes", {})
        task = await task_queue.create_and_enqueue_task(
            title=f"Review/Fix MR: {attrs.get('title')}",
            description=attrs.get("description", "") or "No MR description.",
            repo_url=repo_url,
            base_branch=attrs.get("target_branch", default_branch),
            task_type=TaskType.PULL_REQUEST,
            platform=PlatformType.GITLAB,
            pr_number=attrs.get("iid"),
            sender=user
        )
        return {"status": "ingested", "task_id": task.id}

    return {"status": "ignored", "kind": object_kind}


# ==========================================
# Task Management REST API
# ==========================================

@router.get("/api/tasks", response_model=List[Task])
async def get_tasks_api(limit: int = 50, offset: int = 0):
    return await list_tasks(limit=limit, offset=offset)


@router.post("/api/tasks", response_model=Task)
async def create_task_api(req: TaskCreateRequest):
    return await task_queue.create_and_enqueue_task(
        title=req.title,
        description=req.description,
        repo_url=req.repo_url,
        base_branch=req.base_branch,
        task_type=req.task_type,
        platform=req.platform,
        issue_number=req.issue_number,
        custom_instructions=req.custom_instructions
    )


@router.get("/api/tasks/{task_id}", response_model=Task)
async def get_task_api(task_id: str):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


@router.post("/api/tasks/{task_id}/approve")
async def approve_task_api(task_id: str, custom_instructions: Optional[str] = None):
    task = await task_queue.approve_task(task_id, custom_instructions=custom_instructions)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "approved", "task": task}


class RetryTaskRequest(BaseModel):
    custom_instructions: Optional[str] = None


@router.post("/api/tasks/{task_id}/retry")
async def retry_task_api(task_id: str, req: Optional[RetryTaskRequest] = None):
    instructions = req.custom_instructions if req else None
    task = await task_queue.retry_task(task_id, custom_instructions=instructions)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "retrying", "task": task}


@router.post("/api/tasks/{task_id}/reject")
async def reject_task_api(task_id: str, reason: Optional[str] = None):
    task = await task_queue.reject_task(task_id, reason=reason)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "rejected", "task": task}


@router.delete("/api/tasks/{task_id}")
async def delete_task_api(task_id: str):
    """Delete a task and its associated records."""
    deleted = await delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "deleted", "task_id": task_id}


class ConfirmTaskRequest(BaseModel):
    target_branch: Optional[str] = None


class TargetBranchRequest(BaseModel):
    branch: str


class AnswerQuestionRequest(BaseModel):
    answer: str
    selected_option: Optional[str] = None


class PostCommentRequest(BaseModel):
    comment: str


@router.post("/api/tasks/{task_id}/comment")
async def post_task_comment_api(task_id: str, req: PostCommentRequest):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    
    comment_text = req.comment.strip()
    if not comment_text:
        raise HTTPException(status_code=400, detail="Comment cannot be empty.")

    posted = False
    try:
        if task.platform == PlatformType.GITHUB and task.issue_number:
            owner, repo = GitHubClient().parse_repo_owner_and_name(task.repo_name)
            posted = await GitHubClient().post_issue_comment(owner, repo, task.issue_number, comment_text)
        elif task.platform == PlatformType.GITLAB and task.issue_number:
            project_path = GitLabClient().parse_project_path(task.repo_url)
            posted = await GitLabClient().post_issue_note(project_path, task.issue_number, comment_text)
    except Exception as e:
        logger.warning("Failed to post comment to remote Git provider: %s", e)

    author = "operator"
    new_entry = f"- @{author}: {comment_text}"
    task.comments_context = f"{task.comments_context}\n{new_entry}".strip() if task.comments_context else new_entry
    await save_task(task)
    from issueforge.core.task_dossier import TaskDossierManager
    TaskDossierManager.sync_dossier(task)
    return {"status": "posted", "posted_to_git": posted, "task": task}


@router.post("/api/tasks/{task_id}/subtasks/{subtask_id}/toggle")
async def toggle_task_subtask_api(task_id: str, subtask_id: str):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    found = False
    for st in task.subtasks:
        if st.id == subtask_id:
            st.completed = not st.completed
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Subtask not found.")
    await save_task(task)
    from issueforge.core.task_dossier import TaskDossierManager
    TaskDossierManager.sync_dossier(task)
    return {"status": "toggled", "subtasks": [st.model_dump() for st in task.subtasks]}


@router.post("/api/tasks/{task_id}/confirm")
async def confirm_task_api(task_id: str, req: Optional[ConfirmTaskRequest] = None):
    target_branch = req.target_branch if req else None
    task = await task_queue.confirm_and_push(task_id, target_branch=target_branch)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "confirmed_and_pushed", "task": task}


@router.post("/api/tasks/{task_id}/target-branch")
async def set_target_branch_api(task_id: str, req: TargetBranchRequest):
    task = await task_queue.set_target_branch(task_id, req.branch)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "target_branch_updated", "task": task}


@router.get("/api/tasks/{task_id}/questions")
async def get_task_questions_api(task_id: str):
    from issueforge.core.database import get_task_questions
    return await get_task_questions(task_id)


@router.post("/api/tasks/{task_id}/questions/{q_id}/answer")
async def answer_task_question_api(task_id: str, q_id: str, req: AnswerQuestionRequest):
    task = await task_queue.submit_question_answer(task_id, q_id, req.answer, req.selected_option)
    if not task:
        raise HTTPException(status_code=404, detail="Task or Question not found.")
    return {"status": "answered", "task": task}


@router.get("/api/learnings")
async def list_learnings_api(limit: int = 50):
    from issueforge.core.database import list_learnings
    return await list_learnings(limit=limit)


@router.get("/api/tasks/{task_id}/learnings")
async def get_task_learnings_api(task_id: str):
    from issueforge.core.database import get_task_learnings
    return await get_task_learnings(task_id)


class RevisionRequest(BaseModel):
    notes: str


class StopTaskRequest(BaseModel):
    reason: Optional[str] = "Stopped by user via dashboard."


@router.post("/api/tasks/{task_id}/stop")
@router.post("/api/tasks/{task_id}/cancel")
async def stop_task_api(task_id: str, req: Optional[StopTaskRequest] = None):
    reason = req.reason if req and req.reason else "Stopped by user via dashboard."
    task = await task_queue.cancel_task(task_id, reason=reason)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "cancelled", "task": task}


@router.post("/api/tasks/{task_id}/revise")
async def revise_task_api(task_id: str, req: RevisionRequest):
    task = await task_queue.request_revision(task_id, revision_notes=req.notes)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"status": "revision_started", "task": task}


@router.get("/api/tasks/{task_id}/events", response_model=List[TaskEvent])
async def get_task_events_api(task_id: str, run_id: Optional[str] = Query(None)):
    return await get_task_events(task_id, run_id=run_id)



# ==========================================
# PAT Poller Synchronization API
# ==========================================

@router.post("/api/sync")
async def trigger_sync_api():
    """Trigger on-demand synchronization of GitLab Todos and GitHub Notifications."""
    return await poller_service.sync_all()


@router.get("/api/sync/status")
async def get_sync_status_api():
    """Retrieve current synchronization status and scheduler health."""
    return poller_service.get_status()


# ==========================================
# Sandbox Workspace & Execution Stream API
# ==========================================

@router.get("/api/sandboxes")
async def list_sandboxes_api():
    """List all active or ended sandboxes with disk usage and command counts."""
    return NativeSandbox.list_all_sandboxes()


@router.get("/api/sandboxes/{task_id}")
async def get_sandbox_api(task_id: str, run_id: Optional[str] = None):
    """Retrieve metadata, disk size, and status of a specific sandbox and run."""
    if not run_id:
        task = await get_task(task_id)
        if task and task.active_run_id:
            run_id = task.active_run_id
        elif task and task.runs:
            run_id = task.runs[-1].run_id
    sb = NativeSandbox(task_id, run_id=run_id)
    return sb.get_stats()


@router.get("/api/tasks/{task_id}/runs")
async def get_task_runs_api(task_id: str):
    """Retrieve all pipeline runs and statuses for a task."""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task.id,
        "active_run_id": task.active_run_id or (task.runs[-1].run_id if task.runs else "run-1"),
        "runs": [r.model_dump(mode="json") for r in (task.runs or [])]
    }


@router.get("/api/sandboxes/{task_id}/log")
async def get_sandbox_log_api(task_id: str):
    """Retrieve full raw execution stream log for a sandbox."""
    sb = NativeSandbox(task_id)
    return PlainTextResponse(sb.get_sandbox_log(), media_type="text/plain; charset=utf-8")


@router.get("/api/sandboxes/{task_id}/history")
async def get_sandbox_history_api(task_id: str):
    """Retrieve structured command execution history for a sandbox."""
    return NativeSandbox(task_id).get_execution_history()


@router.delete("/api/sandboxes/{task_id}")
async def delete_sandbox_api(task_id: str):
    """Delete a sandbox workspace from disk."""
    sb = NativeSandbox(task_id)
    deleted = sb.delete_workspace()
    await event_bus.emit_log(
        task_id=task_id,
        message=f"🗑️ Sandbox workspace for {task_id} was deleted from disk.",
        event_type=EventType.LOG
    )
    return {"success": True, "deleted": deleted, "task_id": task_id}


# ==========================================
# Real-Time SSE Event Streaming
# ==========================================

@router.get("/api/events/stream")
async def stream_events(task_id: Optional[str] = None):
    """Server-Sent Events (SSE) endpoint for live terminal and status updates."""
    queue = event_bus.subscribe(task_id=task_id)

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                data = {
                    "task_id": event.task_id,
                    "event_type": event.event_type.value,
                    "role": event.role.value if event.role else None,
                    "message": event.message,
                    "data": event.data,
                    "created_at": event.created_at.isoformat(),
                    "run_id": event.run_id
                }
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(queue, task_id=task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==========================================
# Interactive Terminal WebSocket & History API
# ==========================================

@router.websocket("/ws/tasks/{task_id}/terminal")
async def terminal_websocket(websocket: WebSocket, task_id: str, run_id: Optional[str] = Query(None)):
    """Interactive WebSocket endpoint for streaming ANSI agy CLI sessions to xterm.js."""
    from issueforge.core.terminal_manager import terminal_manager
    from issueforge.web.auth import websocket_is_allowed

    if not websocket_is_allowed(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    task = await get_task(task_id)
    effective_run = run_id or (task.active_run_id if task else None) or "run-1"

    session = await terminal_manager.register_websocket(task_id, effective_run, websocket)

    try:
        while True:
            msg = await websocket.receive_text()
            if not msg:
                continue
            try:
                payload = json.loads(msg)
                msg_type = payload.get("type")
                if msg_type == "input":
                    data_str = payload.get("data", "")
                    session.write_input(data_str.encode("utf-8"))
                elif msg_type == "resize":
                    cols = int(payload.get("cols", 80))
                    rows = int(payload.get("rows", 24))
                    session.set_window_size(cols=cols, rows=rows)
            except json.JSONDecodeError:
                session.write_input(msg.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("Terminal websocket connection closed: %s", e)
    finally:
        terminal_manager.unregister_websocket(task_id, effective_run, websocket)


@router.websocket("/ws/tasks/{task_id}/sandbox-shell")
async def sandbox_shell_websocket(websocket: WebSocket, task_id: str, run_id: Optional[str] = Query(None)):
    """Interactive WebSocket endpoint spawning a direct bash PTY inside the sandbox workspace."""
    from issueforge.core.terminal_manager import shell_manager
    from issueforge.web.auth import websocket_is_allowed

    if not websocket_is_allowed(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    task = await get_task(task_id)
    effective_run = run_id or (task.active_run_id if task else None) or "run-1"
    from issueforge.core.task_dossier import TaskDossierManager
    sandbox_dir = (TaskDossierManager.get_task_dir(task_id) / "sandboxes" / effective_run).resolve()

    shell = await shell_manager.get_or_create_shell(task_id, effective_run, sandbox_dir)
    shell.websockets.add(websocket)

    try:
        while True:
            msg = await websocket.receive_text()
            if not msg:
                continue
            try:
                payload = json.loads(msg)
                msg_type = payload.get("type")
                if msg_type == "input":
                    data_str = payload.get("data", "")
                    shell.write_input(data_str.encode("utf-8"))
                elif msg_type == "resize":
                    cols = int(payload.get("cols", 80))
                    rows = int(payload.get("rows", 24))
                    shell.set_window_size(cols=cols, rows=rows)
            except json.JSONDecodeError:
                shell.write_input(msg.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("Sandbox shell websocket disconnected: %s", e)
    finally:
        shell.websockets.discard(websocket)


class SteerRequest(BaseModel):
    directive: str


@router.post("/api/tasks/{task_id}/steer")
async def steer_task_api(task_id: str, request: SteerRequest):
    """Inject operator mid-flight steering directive into running task memory and terminal."""
    directive_text = request.directive.strip()
    if not directive_text:
        raise HTTPException(status_code=400, detail="Directive cannot be empty")

    task = await task_queue.steer_task(task_id, directive_text)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    ack = f"Directive acknowledged. Incorporating '{directive_text[:60]}' into active agent plan."
    return {
        "success": True,
        "task_id": task.id,
        "directive": directive_text,
        "acknowledgment": ack
    }


@router.get("/api/tasks/{task_id}/files/diff")
async def get_file_diff_api(
    task_id: str,
    file_path: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None)
):
    """Retrieve git diff for a specific file or workspace in the sandbox."""
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    effective_run = run_id or task.active_run_id or "run-1"
    sandbox = NativeSandbox(task_id, run_id=effective_run)

    if not sandbox.workspace_path.exists():
        return {"task_id": task_id, "run_id": effective_run, "file_path": file_path, "diff": "", "is_new": False}

    cmd = "git diff -U3 HEAD"
    if file_path:
        clean_path = file_path.strip().lstrip("/")
        cmd += f" -- {shlex.quote(clean_path)}"

    res = await sandbox.run_command(cmd, timeout=15)
    diff_output = res.stdout or ""

    is_new = False
    if file_path and not diff_output.strip():
        full_p = sandbox.workspace_path / file_path.strip().lstrip("/")
        if full_p.is_file():
            stat_res = await sandbox.run_command(f"git status --porcelain -- {shlex.quote(file_path.strip().lstrip('/'))}", timeout=10)
            if "??" in (stat_res.stdout or ""):
                is_new = True
                try:
                    content = full_p.read_text(encoding="utf-8", errors="replace")
                    lines = [f"+{l}" for l in content.splitlines()]
                    diff_output = f"--- /dev/null\n+++ b/{file_path}\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(lines)
                except Exception:
                    pass

    return {
        "task_id": task_id,
        "run_id": effective_run,
        "file_path": file_path,
        "is_new": is_new,
        "diff": diff_output
    }


@router.get("/api/tasks/{task_id}/runs/{run_id}/terminal")
async def get_run_terminal_raw_api(task_id: str, run_id: str):
    """Retrieve raw ANSI terminal log for a specific pipeline run."""
    from issueforge.core.terminal_manager import terminal_manager
    raw_bytes = terminal_manager.get_raw_history(task_id, run_id)
    return Response(content=raw_bytes, media_type="text/plain; charset=utf-8")


# ==========================================
# Web UI Pages
# ==========================================

@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def dashboard_index(request: Request):
    tasks = await list_tasks(limit=30)
    pending_count = sum(1 for t in tasks if t.status == TaskStatus.PENDING_APPROVAL)
    running_count = sum(
        1 for t in tasks if t.status in (
            TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.TESTING,
            TaskStatus.APPROVED, TaskStatus.PUSHING, TaskStatus.REVISION_REQUESTED
        )
    )
    awaiting_confirm_count = sum(1 for t in tasks if t.status == TaskStatus.AWAITING_CONFIRMATION)
    completed_count = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "tasks": tasks,
            "pending_count": pending_count,
            "running_count": running_count,
            "awaiting_confirm_count": awaiting_confirm_count,
            "completed_count": completed_count,
            "settings": settings,
            "poller_status": poller_service.get_status(),
        }
    )


@router.get("/api/tasks/{task_id}/dossier")
async def get_task_dossier_api(task_id: str):
    """Retrieve task dossier JSON with team, checklist, and run history."""
    from issueforge.core.task_dossier import TaskDossierManager
    dossier = TaskDossierManager.get_task_dossier(task_id)
    if not dossier:
        task = await get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found.")
        TaskDossierManager.sync_dossier(task)
        dossier = TaskDossierManager.get_task_dossier(task_id)
    return dossier or {}


@router.get("/api/tasks/{task_id}/runs/{run_id}/log")
async def get_task_run_log_api(task_id: str, run_id: str):
    """Retrieve raw execution log for a specific pipeline run."""
    sb = NativeSandbox(task_id, run_id=run_id)
    return PlainTextResponse(sb.get_sandbox_log(), media_type="text/plain; charset=utf-8")


@router.get("/api/folders")
async def list_folders_api():
    """Retrieve all task folder dossiers with team, checklist, and run history."""
    from issueforge.core.task_dossier import TaskDossierManager
    return TaskDossierManager.list_all_dossiers()


@router.get("/api/tasks/{task_id}/dossier/readme")
async def get_task_readme_api(task_id: str, raw: str = Query("0")):
    """Retrieve markdown content of README.md for this task folder.

    YAML frontmatter is stripped for display unless `?raw=1`.
    """
    from issueforge.core.task_dossier import TaskDossierManager
    readme = TaskDossierManager.get_task_readme(task_id)
    if not readme:
        task = await get_task(task_id)
        if task:
            TaskDossierManager.sync_dossier(task)
            readme = TaskDossierManager.get_task_readme(task_id)
    if readme and raw not in ("1", "true"):
        readme = _strip_frontmatter(readme)
    return PlainTextResponse(readme or "No README dossier found.", media_type="text/markdown; charset=utf-8")


def _strip_frontmatter(text: str) -> str:
    """Drop a leading `---` YAML block, which Obsidian reads but the UI shows verbatim."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4:].lstrip("\n") if end != -1 else text


@router.get("/api/tasks/{task_id}/canvas")
async def get_task_canvas_api(task_id: str):
    """Retrieve the Obsidian execution DAG (task_dag.canvas) for this task."""
    from issueforge.core.task_dossier import TaskDossierManager
    from issueforge.vault.canvas_builder import CANVAS_FILENAME

    canvas_path = TaskDossierManager.get_task_dir(task_id) / CANVAS_FILENAME
    if not canvas_path.exists():
        raise HTTPException(status_code=404, detail="No canvas found for this task.")
    try:
        return json.loads(canvas_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Canvas is not valid JSON: {exc}")


class CanvasDirectiveRequest(BaseModel):
    text: str
    stage: Optional[str] = None


@router.post("/api/tasks/{task_id}/canvas/directive")
async def add_canvas_directive_api(task_id: str, req: CanvasDirectiveRequest):
    """Queue an operator steering directive without needing Obsidian.

    Same semantics as dropping a card on the canvas: the directive is appended to the
    task's custom_instructions and applies at the next planner/coder/tester turn, not to
    the currently running agent.
    """
    from issueforge.vault.canvas_builder import CanvasDAGBuilder, CanvasStage
    from issueforge.vault.canvas_watcher import apply_canvas_directive

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Directive text cannot be empty.")

    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    queued = await apply_canvas_directive(task_id, text, run_id=task.active_run_id)
    if not queued:
        raise HTTPException(status_code=400, detail="Directive could not be queued.")

    try:
        stage = CanvasStage(req.stage) if req.stage else CanvasStage.CODER
    except ValueError:
        stage = CanvasStage.CODER

    builder = CanvasDAGBuilder.for_task(task_id)
    builder.ensure_initialized(task.title)
    # acknowledged=True: we already queued it here, so the canvas watcher must not
    # re-detect this node and queue the same instruction a second time.
    node_id = builder.inject_steering_directive(text, target_stage=stage, acknowledged=True)

    return {"status": "queued", "node_id": node_id, "applies": "next agent turn"}


@router.get("/api/tasks/{task_id}/graph/edges")
async def get_task_graph_edges_api(task_id: str):
    """Retrieve the wikilink graph edges indexed from this task's dossier."""
    from issueforge.core.database import get_task_graph_edges

    return {"task_id": task_id, "edges": await get_task_graph_edges(task_id)}


@router.get("/api/tasks/{task_id}/runs/{run_id}/files")
async def list_run_files_api(task_id: str, run_id: str):
    """List files in the specific sandbox run workspace."""
    from issueforge.core.task_dossier import TaskDossierManager
    files = TaskDossierManager.list_run_files(task_id, run_id)
    return {"task_id": task_id, "run_id": run_id, "files": files}


@router.get("/api/tasks/{task_id}/runs/{run_id}/files/tree")
async def get_run_file_tree_api(task_id: str, run_id: str):
    """Retrieve structured recursive file tree with metadata and git status."""
    from issueforge.core.task_dossier import TaskDossierManager
    tree = TaskDossierManager.get_run_file_tree(task_id, run_id)
    return {"task_id": task_id, "run_id": run_id, "tree": tree}


@router.get("/api/tasks/{task_id}/runs/{run_id}/files/content")
async def get_sandbox_file_content_api(task_id: str, run_id: str, path: str = Query(...)):
    """Retrieve raw text content of a file in the sandbox workspace."""
    from issueforge.core.task_dossier import TaskDossierManager
    try:
        return TaskDossierManager.get_sandbox_file_content(task_id, run_id, path)
    except PermissionError as pe:
        raise HTTPException(status_code=403, detail=str(pe))
    except FileNotFoundError as fe:
        raise HTTPException(status_code=404, detail=str(fe))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class FileSaveRequest(BaseModel):
    path: str
    content: str


@router.post("/api/tasks/{task_id}/runs/{run_id}/files/content")
async def save_sandbox_file_content_api(task_id: str, run_id: str, req: FileSaveRequest):
    """Save updated content to a file in the sandbox workspace."""
    from issueforge.core.task_dossier import TaskDossierManager
    try:
        TaskDossierManager.save_sandbox_file_content(task_id, run_id, req.path, req.content)
        await event_bus.emit_log(
            task_id=task_id,
            run_id=run_id,
            message=f"✏️ Operator modified `{req.path}` via Web Studio Editor.",
            event_type=EventType.STEP
        )
        return {"success": True, "path": req.path}
    except PermissionError as pe:
        raise HTTPException(status_code=403, detail=str(pe))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/folders", response_class=HTMLResponse)
async def dashboard_folders(request: Request):
    """Task Dossiers & Project Folders page."""
    from issueforge.core.task_dossier import TaskDossierManager
    # Ensure dossiers exist for all DB tasks, and keep the wikilink graph index current.
    # sync_dossier_async debounces on a link fingerprint, so repeat page loads cost nothing.
    all_tasks = await list_tasks(limit=100)
    for t in all_tasks:
        await TaskDossierManager.sync_dossier_async(t)
    dossiers = TaskDossierManager.list_all_dossiers()
    return templates.TemplateResponse(
        request=request,
        name="task_folders.html",
        context={
            "request": request,
            "dossiers": dossiers,
            "settings": settings
        }
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)

async def dashboard_task_detail(request: Request, task_id: str):
    from issueforge.core.task_dossier import TaskDossierManager
    from issueforge.vault.canvas_builder import CANVAS_FILENAME

    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    active_run = task.active_run_id or (task.runs[-1].run_id if task.runs else "run-1")
    events = await get_task_events(task_id, run_id=active_run)
    if not events and not task.runs:
        events = await get_task_events(task_id)
    sandbox = NativeSandbox(task_id, run_id=active_run)
    sandbox_stats = sandbox.get_stats()
    sandbox_history = sandbox.get_execution_history()
    sandbox_log = sandbox.get_sandbox_log()
    from issueforge.core.database import get_task_learnings, get_task_questions
    from issueforge.core.task_dossier import TaskDossierManager
    questions = await get_task_questions(task_id)
    learnings = await get_task_learnings(task_id)
    dossier = TaskDossierManager.get_task_dossier(task_id)

    return templates.TemplateResponse(
        request=request,
        name="task_detail.html",
        context={
            "request": request,
            "task": task,
            "events": events,
            "questions": questions,
            "learnings": learnings,
            "dossier": dossier,
            "active_run": active_run,
            "sandbox_log": sandbox_log,
            "sandbox_stats": sandbox_stats,
            "sandbox_history": sandbox_history,
            "canvas_abs_path": str(
                TaskDossierManager.get_task_dir(task_id) / CANVAS_FILENAME
            ),
            "settings": settings
        }
    )


@router.get("/tasks/{task_id}/codespace", response_class=HTMLResponse)
async def dashboard_codespace(request: Request, task_id: str, run: Optional[str] = None):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    active_run = run or task.active_run_id or (task.runs[-1].run_id if task.runs else "run-1")
    return templates.TemplateResponse(
        request=request,
        name="codespace.html",
        context={
            "request": request,
            "task": task,
            "active_run": active_run,
            "settings": settings
        }
    )


@router.get("/sandboxes", response_class=HTMLResponse)
async def dashboard_sandboxes(request: Request):
    sandboxes = NativeSandbox.list_all_sandboxes()
    return templates.TemplateResponse(
        request=request,
        name="sandboxes.html",
        context={
            "request": request,
            "sandboxes": sandboxes,
            "settings": settings
        }
    )


@router.get("/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "settings": settings
        }
    )
