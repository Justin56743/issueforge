import pytest
from pathlib import Path
from issueforge.config import settings
from issueforge.core.database import close_db, init_db


@pytest.fixture(autouse=True)
async def isolate_test_environment(tmp_path, monkeypatch):
    """Ensure every test uses a temporary SQLite database, vault and workspace directory,
    completely protecting the production database, vault and workspaces from test pollution.
    """
    vault_root = (tmp_path / "vault").resolve()

    # forge_tasks_root must be patched explicitly: Settings._derive_vault_paths only runs at
    # construction, so patching the vault root alone would leave tasks_root on the real vault.
    monkeypatch.setattr(settings, "forge_vault_root", vault_root)
    monkeypatch.setattr(settings, "forge_tasks_root", vault_root / "tasks")
    monkeypatch.setattr(settings, "forge_workspace_root", vault_root / "workspaces")
    monkeypatch.setattr(settings, "forge_db_path", vault_root / "issueforge.db")
    # Never spawn real inotify watches from the test suite.
    monkeypatch.setattr(settings, "forge_canvas_watch_enabled", False)
    # `issueforge start` writes an auto-token into both of these; restore them after every test
    # (and keep a developer's FORGE_AUTH_TOKEN in .env out of the suite).
    monkeypatch.setattr(settings, "forge_auth_token", None)
    monkeypatch.delenv("FORGE_AUTH_TOKEN", raising=False)

    # ensure_directories() rewrites these fields in place with expanduser().resolve(). The
    # values above are already absolute and resolved, so that is value-identical; assert it
    # so a future change to ensure_directories fails loudly here instead of silently writing
    # into the operator's real vault.
    settings.ensure_directories()
    assert settings.forge_tasks_root.is_relative_to(tmp_path), "vault test isolation broke"
    assert not settings.forge_tasks_root.is_relative_to((Path.home() / ".issueforge").resolve())

    await close_db()
    await init_db()

    yield

    await close_db()
