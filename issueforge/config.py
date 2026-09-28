import os
from pathlib import Path
from typing import List, Optional, Union
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Ensure standard Linux CA bundle is picked up by OpenSSL / certifi if present
if "SSL_CERT_FILE" not in os.environ and Path("/etc/ssl/certs/ca-certificates.crt").is_file():
    os.environ["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Server settings
    app_name: str = "Issueforge"
    forge_host: str = Field(default="127.0.0.1", alias="FORGE_HOST")
    forge_port: int = Field(default=8000, alias="FORGE_PORT")
    forge_debug: bool = Field(default=False, alias="FORGE_DEBUG")
    forge_auth_token: Optional[str] = Field(default=None, alias="FORGE_AUTH_TOKEN")
    forge_base_url: str = Field(default="http://localhost:8000", alias="FORGE_BASE_URL")

    # Obsidian Vault & Storage settings
    # The vault root is the single directory Issueforge treats as an Obsidian vault. Unless
    # FORGE_TASKS_ROOT is given explicitly, the task dossiers live inside it (see
    # _derive_vault_paths below), which keeps the DB, the dossiers and the .obsidian
    # config from drifting into separate trees.
    forge_vault_root: Path = Field(
        default_factory=lambda: Path.home() / ".issueforge",
        alias="FORGE_VAULT_ROOT"
    )
    forge_tasks_root: Path = Field(
        default_factory=lambda: Path(".issueforge") / "tasks",
        alias="FORGE_TASKS_ROOT"
    )
    forge_workspace_root: Path = Field(
        default_factory=lambda: Path(".issueforge") / "workspaces",
        alias="FORGE_WORKSPACE_ROOT"
    )
    forge_db_path: Path = Field(
        default_factory=lambda: Path(".issueforge") / "issueforge.db",
        alias="FORGE_DB_PATH"
    )
    forge_execution_timeout_seconds: int = Field(default=600, alias="FORGE_EXECUTION_TIMEOUT_SECONDS")
    # Live Obsidian canvas telemetry, and the reverse-steering file watcher. The watcher is
    # disabled in tests so the suite never spawns real inotify watches.
    forge_canvas_enabled: bool = Field(default=True, alias="FORGE_CANVAS_ENABLED")
    forge_canvas_watch_enabled: bool = Field(default=True, alias="FORGE_CANVAS_WATCH_ENABLED")

    # Telegram settings
    telegram_enabled: bool = Field(default=False, alias="TELEGRAM_ENABLED")
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_ids_raw: str = Field(default="", alias="TELEGRAM_ALLOWED_USER_IDS")
    telegram_proxy: Optional[str] = Field(default=None, alias="TELEGRAM_PROXY")

    # GitHub settings
    github_token: Optional[str] = Field(default=None, alias="GITHUB_TOKEN")
    github_webhook_secret: Optional[str] = Field(default=None, alias="GITHUB_WEBHOOK_SECRET")

    # GitLab settings
    gitlab_url: str = Field(default="https://gitlab.com", alias="GITLAB_URL")
    gitlab_token: Optional[str] = Field(default=None, alias="GITLAB_TOKEN")
    gitlab_webhook_secret: Optional[str] = Field(default=None, alias="GITLAB_WEBHOOK_SECRET")

    # SSL & Network verification settings (defaults to system CA bundle if present)
    git_ssl_verify: Union[bool, str] = Field(
        default_factory=lambda: "/etc/ssl/certs/ca-certificates.crt" if Path("/etc/ssl/certs/ca-certificates.crt").is_file() else True,
        alias="GIT_SSL_VERIFY"
    )

    # Background Polling Settings (3x daily schedule + on-demand sync)
    poll_enabled: bool = Field(default=True, alias="POLL_ENABLED")
    poll_interval_hours: float = Field(default=8.0, alias="POLL_INTERVAL_HOURS")
    auto_mark_todo_done: bool = Field(default=True, alias="AUTO_MARK_TODO_DONE")

    # Multi-LLM Model configurations (Updated to active Gemini 3.x models)
    planner_model: str = Field(default="gemini/gemini-3.7-flash", alias="PLANNER_MODEL")
    coder_model: str = Field(default="gemini/gemini-3.8-flash", alias="CODER_MODEL")
    tester_model: str = Field(default="gemini/gemini-3.6-flash", alias="TESTER_MODEL")
    reviewer_model: str = Field(default="gemini/gemini-3.7-flash", alias="REVIEWER_MODEL")
    llm_fallback_models_raw: str = Field(
        default="gemini/gemini-3.7-flash,gemini/gemini-3.8-flash,gemini/gemini-3.6-flash,gemini/gemini-3.5-flash,gemini/gemini-flash-latest",
        alias="LLM_FALLBACK_MODELS"
    )

    # LLM API Keys
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    deepseek_api_key: Optional[str] = Field(default=None, alias="DEEPSEEK_API_KEY")
    ollama_api_base: str = Field(default="http://localhost:11434", alias="OLLAMA_API_BASE")

    @model_validator(mode="after")
    def _derive_vault_paths(self) -> "Settings":
        """Root the task dossiers inside the vault unless FORGE_TASKS_ROOT was set explicitly.

        `model_fields_set` covers values supplied by the environment *and* by .env, so an
        explicit FORGE_TASKS_ROOT always wins. This runs at construction only, so anything
        mutating `forge_vault_root` later (e.g. test fixtures) must set `forge_tasks_root` too.
        """
        vault = self.forge_vault_root.expanduser()
        if "forge_tasks_root" not in self.model_fields_set:
            self.forge_tasks_root = vault / "tasks"
        if "forge_workspace_root" not in self.model_fields_set:
            self.forge_workspace_root = vault / "workspaces"
        if "forge_db_path" not in self.model_fields_set:
            self.forge_db_path = vault / "issueforge.db"
        return self

    @property
    def obsidian_dir(self) -> Path:
        return self.forge_vault_root / ".obsidian"

    @property
    def knowledge_dir(self) -> Path:
        return self.forge_vault_root / "knowledge_vault"

    @property
    def graph_dir(self) -> Path:
        return self.forge_vault_root / "codebase_graph"

    @property
    def is_github_configured(self) -> bool:
        return bool(self.github_token and "your_github" not in self.github_token and self.github_token.strip())

    @property
    def is_gitlab_configured(self) -> bool:
        return bool(self.gitlab_token and "your_gitlab" not in self.gitlab_token and self.gitlab_token.strip())

    @property
    def default_model(self) -> str:
        return self.planner_model

    @property
    def llm_fallback_models(self) -> List[str]:
        if not self.llm_fallback_models_raw:
            return []
        return [m.strip() for m in self.llm_fallback_models_raw.split(",") if m.strip()]

    @property
    def telegram_allowed_user_ids(self) -> List[int]:
        if not self.telegram_allowed_user_ids_raw:
            return []
        ids = []
        for part in self.telegram_allowed_user_ids_raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    def ensure_directories(self) -> None:
        """Ensure necessary storage directories exist, falling back to local .issueforge if read-only."""
        # Resolved first: forge_tasks_root is normally a child of the vault root.
        try:
            vault_root = self.forge_vault_root.expanduser().resolve()
            vault_root.mkdir(parents=True, exist_ok=True)
            self.forge_vault_root = vault_root
        except (OSError, PermissionError):
            fallback_vault = (Path.cwd() / ".issueforge").resolve()
            fallback_vault.mkdir(parents=True, exist_ok=True)
            self.forge_vault_root = fallback_vault

        try:
            tasks_root = self.forge_tasks_root.expanduser().resolve()
            tasks_root.mkdir(parents=True, exist_ok=True)
            self.forge_tasks_root = tasks_root
        except (OSError, PermissionError):
            fallback_tasks = (Path.cwd() / ".issueforge" / "tasks").resolve()
            fallback_tasks.mkdir(parents=True, exist_ok=True)
            self.forge_tasks_root = fallback_tasks

        try:
            ws_root = self.forge_workspace_root.expanduser().resolve()
            ws_root.mkdir(parents=True, exist_ok=True)
            self.forge_workspace_root = ws_root
        except (OSError, PermissionError):
            fallback_ws = (Path.cwd() / ".issueforge" / "workspaces").resolve()
            fallback_ws.mkdir(parents=True, exist_ok=True)
            self.forge_workspace_root = fallback_ws

        try:
            db_p = self.forge_db_path.expanduser().resolve()
            db_p.parent.mkdir(parents=True, exist_ok=True)
            self.forge_db_path = db_p
        except (OSError, PermissionError):
            fallback_db = (Path.cwd() / ".issueforge" / "issueforge.db").resolve()
            fallback_db.parent.mkdir(parents=True, exist_ok=True)
            self.forge_db_path = fallback_db


settings = Settings()
