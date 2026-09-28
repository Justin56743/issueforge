import hmac
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
import httpx

from issueforge.config import settings


class GitLabClient:
    """Client for interacting with GitLab REST API, Webhooks, and Todos."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or settings.gitlab_url).rstrip("/")
        self.token = token or settings.gitlab_token

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Issueforge-Autonomous-Agent/1.0"
        }
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        return headers

    def _client(self, timeout: float = 20.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers(), timeout=timeout, verify=settings.git_ssl_verify)

    @staticmethod
    def verify_webhook_token(token_header: Optional[str]) -> bool:
        """Verify GitLab X-Gitlab-Token header."""
        secret = settings.gitlab_webhook_secret
        if not secret:
            return True
        return hmac.compare_digest((token_header or "").encode(), secret.encode())

    @staticmethod
    def parse_project_path(repo_url: str) -> str:
        """Extract 'namespace/project' from GitLab repo URL."""
        clean = repo_url.rstrip("/").replace(".git", "")
        if "://" in clean:
            clean = clean.split("://", 1)[1]
            parts = clean.split("/", 1)
            if len(parts) > 1:
                return parts[1]
        return clean

    async def create_merge_request(
        self,
        project_path: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str
    ) -> Optional[Dict[str, Any]]:
        """Create a new Merge Request on GitLab."""
        encoded_project = quote_plus(project_path)
        url = f"{self.base_url}/api/v4/projects/{encoded_project}/merge_requests"
        payload = {
            "title": title,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "description": description,
            "remove_source_branch": False
        }
        async with self._client(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code in (200, 201):
                return resp.json()
            return None

    async def create_issue(
        self,
        project_path: str,
        title: str,
        description: str,
        labels: Optional[List[str]] = None,
        assignee_ids: Optional[List[int]] = None
    ) -> Optional[Dict[str, Any]]:
        """Create a new issue on GitLab."""
        if not self.token:
            return None
        encoded_project = quote_plus(project_path)
        url = f"{self.base_url}/api/v4/projects/{encoded_project}/issues"
        payload: Dict[str, Any] = {
            "title": title,
            "description": description
        }
        if labels:
            payload["labels"] = ",".join(labels)
        if assignee_ids:
            payload["assignee_ids"] = assignee_ids
        async with self._client(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code in (200, 201):
                return resp.json()
            return None

    async def post_issue_note(self, project_path: str, issue_iid: int, note: str) -> bool:
        """Post a comment/note on an issue."""
        encoded_project = quote_plus(project_path)
        url = f"{self.base_url}/api/v4/projects/{encoded_project}/issues/{issue_iid}/notes"
        payload = {"body": note}
        async with self._client(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code in (200, 201)

    async def list_issue_notes(self, project_path: str, issue_iid: int, per_page: int = 50) -> List[Dict[str, Any]]:
        """Fetch all discussion notes/comments for an issue."""
        if not self.token:
            return []
        encoded_project = quote_plus(project_path)
        url = f"{self.base_url}/api/v4/projects/{encoded_project}/issues/{issue_iid}/notes?per_page={per_page}&sort=asc"
        async with self._client(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def update_issue_metadata(
        self,
        project_path: str,
        issue_iid: int,
        labels: Optional[List[str]] = None,
        add_labels: Optional[List[str]] = None,
        remove_labels: Optional[List[str]] = None,
        state_event: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Update issue labels, state, or metadata on GitLab."""
        if not self.token:
            return None
        encoded_project = quote_plus(project_path)
        url = f"{self.base_url}/api/v4/projects/{encoded_project}/issues/{issue_iid}"
        payload: Dict[str, Any] = {}
        if labels is not None:
            payload["labels"] = ",".join(labels)
        if add_labels:
            payload["add_labels"] = ",".join(add_labels)
        if remove_labels:
            payload["remove_labels"] = ",".join(remove_labels)
        if state_event:
            payload["state_event"] = state_event

        async with self._client(timeout=15.0) as client:
            resp = await client.put(url, json=payload)
            if resp.status_code == 200:
                return resp.json()
            return None

    async def close_issue(self, project_path: str, issue_iid: int) -> bool:
        """Close an issue on GitLab."""
        res = await self.update_issue_metadata(project_path, issue_iid, state_event="close")
        return res is not None

    async def get_project_branches(self, project_path: str) -> List[str]:
        """Fetch remote branch names from GitLab API."""
        if not self.token:
            return []
        encoded = quote_plus(project_path)
        url = f"{self.base_url}/api/v4/projects/{encoded}/repository/branches"
        async with self._client(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return [b.get("name") for b in resp.json() if b.get("name")]
            return []

    async def get_pending_todos(self, per_page: int = 50) -> List[Dict[str, Any]]:
        """Fetch pending Todos for the authenticated user using Personal Access Token."""
        if not self.token:
            return []
        url = f"{self.base_url}/api/v4/todos?state=pending&per_page={per_page}"
        async with self._client(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def mark_todo_as_done(self, todo_id: int) -> bool:
        """Mark a specific Todo item as done in GitLab."""
        if not self.token:
            return False
        url = f"{self.base_url}/api/v4/todos/{todo_id}/mark_as_done"
        async with self._client(timeout=15.0) as client:
            resp = await client.post(url)
            return resp.status_code in (200, 201)

    async def get_project(self, project_id_or_path: Any) -> Optional[Dict[str, Any]]:
        """Fetch project details including clone url and default branch."""
        if not self.token:
            return None
        encoded = quote_plus(str(project_id_or_path))
        url = f"{self.base_url}/api/v4/projects/{encoded}"
        async with self._client(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return None

    async def get_current_user(self) -> Optional[Dict[str, Any]]:
        """Fetch currently authenticated GitLab user profile."""
        if not self.token:
            return None
        url = f"{self.base_url}/api/v4/user"
        async with self._client(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return None

    async def get_assigned_issues(self, per_page: int = 50) -> List[Dict[str, Any]]:
        """Fetch open issues assigned to authenticated user."""
        if not self.token:
            return []
        url = f"{self.base_url}/api/v4/issues?scope=assigned_to_me&state=opened&per_page={per_page}"
        async with self._client(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def get_assigned_merge_requests(self, per_page: int = 50) -> List[Dict[str, Any]]:
        """Fetch open merge requests assigned to authenticated user."""
        if not self.token:
            return []
        url = f"{self.base_url}/api/v4/merge_requests?scope=assigned_to_me&state=opened&per_page={per_page}"
        async with self._client(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def get_reviewer_merge_requests(self, user_id: int, per_page: int = 50) -> List[Dict[str, Any]]:
        """Fetch open merge requests where user is designated as reviewer."""
        if not self.token:
            return []
        url = f"{self.base_url}/api/v4/merge_requests?scope=all&state=opened&reviewer_id={user_id}&per_page={per_page}"
        async with self._client(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return []

