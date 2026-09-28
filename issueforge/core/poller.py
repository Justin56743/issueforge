import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from issueforge.config import settings
from issueforge.core.database import list_tasks, normalize_repo_url
from issueforge.core.events import event_bus
from issueforge.core.models import EventType, PlatformType, Task, TaskType
from issueforge.core.queue import task_queue
from issueforge.git.github_client import GitHubClient
from issueforge.git.gitlab_client import GitLabClient


class PollerService:
    """Periodically polls GitLab Todos and GitHub Notifications using Personal Access Tokens."""

    def __init__(self):
        self.gitlab_client = GitLabClient()
        self.github_client = GitHubClient()
        self.last_sync_time: Optional[datetime] = None
        self.last_sync_count: int = 0
        self.last_error: Optional[str] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._sync_lock = asyncio.Lock()

    async def sync_gitlab_todos(self) -> List[Task]:
        """Fetch pending GitLab Todos, assigned issues, and assigned/reviewer MRs for authenticated user."""
        if not settings.is_gitlab_configured:
            return []

        # Load existing tasks to prevent duplicate ingestion
        existing_tasks = await list_tasks(limit=500)
        existing_keys = set()
        for t in existing_tasks:
            if t.platform == PlatformType.GITLAB:
                num = t.issue_number if t.issue_number is not None else t.pr_number
                existing_keys.add(f"{normalize_repo_url(t.repo_url)}::{num}")

        new_tasks: List[Task] = []
        project_cache: Dict[int, Dict[str, Any]] = {}
        branch_cache: Dict[int, List[str]] = {}

        async def get_project_info(proj_id: int, fallback_project: Optional[Dict[str, Any]] = None) -> Tuple[str, str, List[str]]:
            if proj_id in project_cache:
                p = project_cache[proj_id]
                branches = branch_cache.get(proj_id, [])
            else:
                p = await self.gitlab_client.get_project(proj_id)
                if not p and fallback_project:
                    p = fallback_project
                if p:
                    project_cache[proj_id] = p
                try:
                    branches = await self.gitlab_client.get_project_branches(str(proj_id))
                except Exception:
                    branches = []
                branch_cache[proj_id] = branches
            if p:
                return p.get("http_url_to_repo") or p.get("web_url", ""), p.get("default_branch", "main"), branches
            return "", "main", []

        # 1. Ingest Pending Todos (mentions, assignments, review requests)
        try:
            todos = await self.gitlab_client.get_pending_todos(per_page=50)
            for todo in todos:
                try:
                    target = todo.get("target", {})
                    target_type = todo.get("target_type", "")
                    iid = target.get("iid") or target.get("id")

                    project = todo.get("project", {})
                    repo_url = project.get("http_url_to_repo") or project.get("web_url", "")
                    base_branch = project.get("default_branch", "main")
                    branches: List[str] = []

                    if project.get("id"):
                        repo_url, base_branch, branches = await get_project_info(project["id"], fallback_project=project)

                    if not repo_url:
                        continue

                    key = f"{normalize_repo_url(repo_url)}::{iid}"
                    if key in existing_keys:
                        continue

                    task_type = TaskType.PULL_REQUEST if target_type == "MergeRequest" else TaskType.ISSUE
                    title = target.get("title") or todo.get("body") or "GitLab Work Item"
                    description = target.get("description") or todo.get("body") or "Imported from GitLab Todos."
                    sender = todo.get("author", {}).get("username", "gitlab")

                    task = await task_queue.create_and_enqueue_task(
                        title=title,
                        description=description,
                        repo_url=repo_url,
                        base_branch=base_branch,
                        task_type=task_type,
                        platform=PlatformType.GITLAB,
                        issue_number=iid if task_type == TaskType.ISSUE else None,
                        pr_number=iid if task_type == TaskType.PULL_REQUEST else None,
                        sender=sender,
                        target_branch_candidates=branches
                    )

                    existing_keys.add(key)
                    new_tasks.append(task)

                    if settings.auto_mark_todo_done and todo.get("id"):
                        await self.gitlab_client.mark_todo_as_done(todo["id"])
                        await event_bus.emit_log(
                            task_id=task.id,
                            message=f"Marked GitLab Todo #{todo['id']} as done.",
                            event_type=EventType.LOG
                        )
                except Exception as e:
                    print(f"[Poller Error] Error processing GitLab todo: {e}")
        except Exception as e:
            print(f"[Poller Error] Failed fetching GitLab todos: {e}")

        # 2. Ingest Open Assigned Issues (in case todo was cleared or pre-existing)
        try:
            assigned_issues = await self.gitlab_client.get_assigned_issues(per_page=50)
            for issue in assigned_issues:
                try:
                    iid = issue.get("iid")
                    proj_id = issue.get("project_id")
                    if not iid or not proj_id:
                        continue

                    repo_url, base_branch, branches = await get_project_info(proj_id)
                    if not repo_url:
                        continue

                    key = f"{normalize_repo_url(repo_url)}::{iid}"
                    if key in existing_keys:
                        continue

                    title = issue.get("title", "GitLab Assigned Issue")
                    description = issue.get("description", "") or "Imported from GitLab assigned issues."
                    sender = issue.get("author", {}).get("username", "gitlab")

                    task = await task_queue.create_and_enqueue_task(
                        title=title,
                        description=description,
                        repo_url=repo_url,
                        base_branch=base_branch,
                        task_type=TaskType.ISSUE,
                        platform=PlatformType.GITLAB,
                        issue_number=iid,
                        sender=sender,
                        target_branch_candidates=branches
                    )

                    existing_keys.add(key)
                    new_tasks.append(task)
                except Exception as e:
                    print(f"[Poller Error] Error processing assigned issue: {e}")
        except Exception as e:
            print(f"[Poller Error] Failed fetching assigned issues: {e}")

        # 3. Ingest Open Assigned & Review-Requested Merge Requests
        try:
            mrs = await self.gitlab_client.get_assigned_merge_requests(per_page=50)
            user = await self.gitlab_client.get_current_user()
            if user and user.get("id"):
                rev_mrs = await self.gitlab_client.get_reviewer_merge_requests(user["id"], per_page=50)
                mrs.extend(rev_mrs)

            for mr in mrs:
                try:
                    iid = mr.get("iid")
                    proj_id = mr.get("project_id")
                    if not iid or not proj_id:
                        continue

                    repo_url, default_branch, branches = await get_project_info(proj_id)
                    if not repo_url:
                        continue

                    key = f"{normalize_repo_url(repo_url)}::{iid}"
                    if key in existing_keys:
                        continue

                    base_branch = mr.get("target_branch", default_branch)
                    title = f"Review/Fix MR: {mr.get('title', 'GitLab Merge Request')}"
                    description = mr.get("description", "") or "Imported from GitLab merge requests."
                    sender = mr.get("author", {}).get("username", "gitlab")

                    task = await task_queue.create_and_enqueue_task(
                        title=title,
                        description=description,
                        repo_url=repo_url,
                        base_branch=base_branch,
                        task_type=TaskType.PULL_REQUEST,
                        platform=PlatformType.GITLAB,
                        pr_number=iid,
                        sender=sender,
                        target_branch_candidates=branches
                    )

                    existing_keys.add(key)
                    new_tasks.append(task)
                except Exception as e:
                    print(f"[Poller Error] Error processing assigned MR: {e}")
        except Exception as e:
            print(f"[Poller Error] Failed fetching assigned MRs: {e}")

        return new_tasks

    async def sync_github_notifications(self) -> List[Task]:
        """Fetch assigned GitHub issues, ingest as tasks."""
        if not settings.is_github_configured:
            return []

        issues = await self.github_client.get_user_assigned_issues(per_page=30)
        if not issues:
            return []

        existing_tasks = await list_tasks(limit=200)
        existing_keys = set()
        for t in existing_tasks:
            if t.platform == PlatformType.GITHUB:
                num = t.issue_number if t.issue_number is not None else t.pr_number
                existing_keys.add(f"{normalize_repo_url(t.repo_url)}::{num}")

        new_tasks: List[Task] = []

        for issue in issues:
            try:
                num = issue.get("number")
                repo_data = issue.get("repository", {})
                repo_url = repo_data.get("clone_url") or repo_data.get("html_url") or issue.get("repository_url", "")
                base_branch = repo_data.get("default_branch", "main")

                key = f"{normalize_repo_url(repo_url)}::{num}"
                if key in existing_keys:
                    continue

                is_pr = "pull_request" in issue
                task_type = TaskType.PULL_REQUEST if is_pr else TaskType.ISSUE
                title = issue.get("title", "GitHub Work Item")
                description = issue.get("body", "") or "Imported from GitHub assigned issues."
                sender = issue.get("user", {}).get("login", "github")

                task = await task_queue.create_and_enqueue_task(
                    title=title,
                    description=description,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    task_type=task_type,
                    platform=PlatformType.GITHUB,
                    issue_number=None if is_pr else num,
                    pr_number=num if is_pr else None,
                    sender=sender
                )

                existing_keys.add(key)
                new_tasks.append(task)

            except Exception as e:
                print(f"[Poller Error] Error processing GitHub issue: {e}")

        return new_tasks

    async def sync_all(self) -> Dict[str, Any]:
        """Trigger comprehensive on-demand or scheduled synchronization."""
        if self._sync_lock.locked():
            return {
                "success": True,
                "message": "Sync already in progress. Concurrent sync skipped.",
                "gitlab_count": 0,
                "github_count": 0,
                "total_new": 0,
                "synced_at": datetime.now(timezone.utc).isoformat()
            }

        async with self._sync_lock:
            try:
                gl_tasks = await self.sync_gitlab_todos()
                gh_tasks = await self.sync_github_notifications()
                total_new = len(gl_tasks) + len(gh_tasks)

                self.last_sync_time = datetime.now(timezone.utc)
                self.last_sync_count = total_new
                self.last_error = None

                return {
                    "success": True,
                    "gitlab_count": len(gl_tasks),
                    "github_count": len(gh_tasks),
                    "total_new": total_new,
                    "synced_at": self.last_sync_time.isoformat()
                }
            except Exception as e:
                self.last_error = str(e)
                return {
                    "success": False,
                    "error": str(e),
                    "synced_at": datetime.now(timezone.utc).isoformat()
                }

    async def _scheduler_loop(self) -> None:
        """Background loop running periodic sync every poll_interval_hours (e.g. 8h = 3x daily)."""
        # Run an initial sync 10 seconds after server boot
        await asyncio.sleep(10)
        await self.sync_all()

        while True:
            interval_sec = max(int(settings.poll_interval_hours * 3600), 300)
            await asyncio.sleep(interval_sec)
            try:
                await self.sync_all()
            except Exception as e:
                print(f"[Poller Scheduler Error] {e}")

    def start_scheduler(self) -> None:
        """Start the async scheduler loop."""
        if settings.poll_enabled and self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def stop_scheduler(self) -> None:
        """Stop the async scheduler loop."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            self._scheduler_task = None

    def get_status(self) -> Dict[str, Any]:
        return {
            "poll_enabled": settings.poll_enabled,
            "poll_interval_hours": settings.poll_interval_hours,
            "auto_mark_todo_done": settings.auto_mark_todo_done,
            "last_sync_time": self.last_sync_time.isoformat() if self.last_sync_time else None,
            "last_sync_count": self.last_sync_count,
            "last_error": self.last_error,
            "scheduler_running": self._scheduler_task is not None and not self._scheduler_task.done()
        }


poller_service = PollerService()
