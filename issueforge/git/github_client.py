import hashlib
import hmac
from typing import Any, Dict, List, Optional, Tuple
import httpx

from issueforge.config import settings


class GitHubClient:
    """Client for interacting with GitHub REST API, Webhooks, and Notifications."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or settings.github_token
        self.base_url = "https://api.github.com"

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Issueforge-Autonomous-Agent/1.0"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _client(self, timeout: float = 20.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers(), timeout=timeout, verify=settings.git_ssl_verify)

    @staticmethod
    def verify_webhook_signature(payload_bytes: bytes, signature_header: Optional[str]) -> bool:
        """Verify GitHub HMAC SHA-256 webhook signature."""
        secret = settings.github_webhook_secret
        if not secret:
            return True  # If no secret configured, skip verification
        if not signature_header or not signature_header.startswith("sha256="):
            return False

        expected_sig = signature_header.split("sha256=")[1]
        mac = hmac.new(secret.encode("utf-8"), msg=payload_bytes, digestmod=hashlib.sha256)
        return hmac.compare_digest(mac.hexdigest(), expected_sig)

    @staticmethod
    def parse_repo_owner_and_name(repo_url_or_full_name: str) -> Tuple[str, str]:
        """Extract (owner, repo_name) from URL or 'owner/repo' format."""
        clean = repo_url_or_full_name.rstrip("/").replace(".git", "")
        if "github.com/" in clean:
            parts = clean.split("github.com/")[1].split("/")
            return parts[0], parts[1]
        if "/" in clean:
            parts = clean.split("/")
            return parts[0], parts[1]
        return "", clean

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str
    ) -> Optional[Dict[str, Any]]:
        """Create a new Pull Request on GitHub."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls"
        payload = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "maintainer_can_modify": True
        }
        async with self._client(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code in (200, 201):
                return resp.json()
            return None

    async def post_issue_comment(self, owner: str, repo: str, issue_number: int, comment: str) -> bool:
        """Post a comment on an issue or PR."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        payload = {"body": comment}
        async with self._client(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code in (200, 201)

    async def list_issue_comments(self, owner: str, repo: str, issue_number: int, per_page: int = 50) -> List[Dict[str, Any]]:
        """Fetch comments for an issue or PR."""
        if not self.token:
            return []
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments?per_page={per_page}"
        async with self._client(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return []

    async def update_issue_labels(self, owner: str, repo: str, issue_number: int, labels: List[str]) -> bool:
        """Replace or set labels for an issue."""
        if not self.token:
            return False
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/labels"
        payload = {"labels": labels}
        async with self._client(timeout=15.0) as client:
            resp = await client.put(url, json=payload)
            return resp.status_code in (200, 201)

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> bool:
        """Close an issue on GitHub."""
        if not self.token:
            return False
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}"
        payload = {"state": "closed"}
        async with self._client(timeout=15.0) as client:
            resp = await client.patch(url, json=payload)
            return resp.status_code == 200

    async def get_repo_branches(self, owner: str, repo: str) -> List[str]:
        """Fetch remote branch names from GitHub API."""
        if not self.token:
            return []
        url = f"{self.base_url}/repos/{owner}/{repo}/branches?per_page=50"
        async with self._client(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return [b.get("name") for b in resp.json() if b.get("name")]
            return []

    async def get_user_assigned_issues(self, per_page: int = 30) -> List[Dict[str, Any]]:
        """Fetch open issues assigned to authenticated user."""
        if not self.token:
            return []
        url = f"{self.base_url}/user/issues?filter=assigned&state=open&per_page={per_page}"
        async with self._client(timeout=20.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            return []

