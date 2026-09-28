import stat
from pathlib import Path


def test_agent_skills_ship_inside_the_package():
    import issueforge

    skills_dir = Path(issueforge.__file__).parent / "agents" / "skills"
    assert skills_dir.is_dir(), "skills must live inside the package to survive pip install"
    names = {p.name for p in skills_dir.iterdir() if p.is_dir()}
    assert {"ponytail", "senior-dev"} <= names


def test_templates_and_static_ship_inside_the_package():
    import issueforge

    root = Path(issueforge.__file__).parent
    assert (root / "web" / "templates" / "index.html").is_file()
    assert (root / "web" / "static" / "js" / "dashboard.js").is_file()


def test_init_writes_a_usable_env_file(tmp_path, monkeypatch):
    from issueforge.main import write_env_file

    target = tmp_path / ".env"
    write_env_file(
        target,
        gemini_api_key="test-key",
        github_token="ghp_example",
        gitlab_token=None,
        telegram_bot_token=None,
    )

    content = target.read_text()
    assert "GEMINI_API_KEY=test-key" in content
    assert "GITHUB_TOKEN=ghp_example" in content
    # Values the user skipped are written commented out, not as empty strings,
    # because an empty string overrides the setting default.
    assert "# GITLAB_TOKEN=" in content
    assert "FORGE_HOST=127.0.0.1" in content


def test_init_creates_env_with_owner_only_permissions(tmp_path):
    from issueforge.main import write_env_file

    target = tmp_path / ".env"
    write_env_file(
        target,
        gemini_api_key="test-key",
        github_token=None,
        gitlab_token=None,
        telegram_bot_token=None,
    )

    # Verify the file is created with owner-only permissions (0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_init_refuses_to_clobber_an_existing_env(tmp_path):
    import pytest

    from issueforge.main import write_env_file

    target = tmp_path / ".env"
    target.write_text("GEMINI_API_KEY=already-here\n")

    with pytest.raises(FileExistsError):
        write_env_file(target, gemini_api_key="new", github_token=None,
                       gitlab_token=None, telegram_bot_token=None)
    assert "already-here" in target.read_text()


def test_init_with_a_bot_token_enables_telegram_with_an_allowlist(tmp_path):
    from issueforge.main import write_env_file

    target = tmp_path / ".env"
    write_env_file(target, gemini_api_key=None, github_token=None, gitlab_token=None,
                   telegram_bot_token="123:abc", telegram_user_id="42")
    content = target.read_text()
    assert "TELEGRAM_ENABLED=true" in content
    assert "TELEGRAM_ALLOWED_USER_IDS=42" in content

    bare = tmp_path / "bare.env"
    write_env_file(bare, gemini_api_key=None, github_token=None, gitlab_token=None,
                   telegram_bot_token="123:abc")
    assert "# TELEGRAM_ALLOWED_USER_IDS=" in bare.read_text()


def test_init_command_hides_secrets_and_asks_for_the_telegram_user(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from issueforge.main import cli

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["init"], input="gem-secret\n\n\n123:bot-secret\n42\n")
    assert result.exit_code == 0, result.output
    assert "gem-secret" not in result.output and "bot-secret" not in result.output
    assert "Telegram user ID" in result.output
    content = (tmp_path / ".env").read_text()
    assert "GEMINI_API_KEY=gem-secret" in content
    assert "TELEGRAM_ALLOWED_USER_IDS=42" in content


def test_version_flag_prints_the_package_version():
    from typer.testing import CliRunner

    from issueforge import __version__
    from issueforge.main import cli

    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"issueforge {__version__}"
