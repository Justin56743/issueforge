import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import typer
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from rich.console import Console
from rich.table import Table

from issueforge import __version__
from issueforge.bot.telegram_bot import telegram_manager
from issueforge.config import settings
from issueforge.core.database import init_db, list_tasks
from issueforge.core.models import TaskType
from issueforge.core.poller import poller_service
from issueforge.core.queue import task_queue
from issueforge.vault.manager import VaultManager
from issueforge.vault.migration import apply_migration, detect_legacy_dossiers, plan_migration
from issueforge.web.api import router as api_router
from issueforge.web.auth import load_or_create_token

console = Console()
cli = typer.Typer(help="Issueforge: Autonomous Multi-LLM Git Agent Orchestrator.")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"issueforge {__version__}")
        raise typer.Exit()


@cli.callback()
def _main(
    version: bool = typer.Option(False, "--version", callback=_print_version, is_eager=True, help="Show the version and exit."),
) -> None:
    pass

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def assert_safe_binding(host: str, token: Optional[str]) -> None:
    """Refuse to serve an unauthenticated dashboard on a reachable interface.

    The dashboard can run shell commands and write files in sandboxes, so an
    exposed instance without a token is remote code execution for anyone on
    the network.
    """
    if host in LOOPBACK_HOSTS or token:
        return
    raise SystemExit(
        f"Refusing to bind {host} without authentication.\n"
        f"The dashboard can execute shell commands, so exposing it unauthenticated\n"
        f"grants code execution to anyone who can reach the port.\n\n"
        f"Either bind loopback (the default), or set a token:\n"
        f"    FORGE_AUTH_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')\n"
    )


def warn_legacy_agit_config() -> None:
    """Point an upgrading Agit operator at the README before their old state is silently ignored."""
    legacy_keys = sorted(k for k in dotenv_values(".env") if k.startswith("AGIT_"))
    legacy_dir = Path.home() / ".agit"
    if not legacy_keys and not legacy_dir.exists():
        return
    found = ([f"{legacy_dir} exists"] if legacy_dir.exists() else []) + (
        [f".env still uses {', '.join(legacy_keys)} (ignored; rename to FORGE_*)"] if legacy_keys else []
    )
    console.print(
        f"[bold yellow]⚠️ Agit leftovers found: {'; '.join(found)}. "
        f"See \"Upgrading from Agit\" in the README.[/bold yellow]",
        soft_wrap=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    console.print("[bold green]⚡ Initializing Issueforge Core...[/bold green]")
    settings.ensure_directories()

    # Bootstrap the Obsidian vault. Non-fatal by design: a read-only or misconfigured vault
    # must not stop the dashboard from booting (same tolerance as the Telegram bot below).
    try:
        VaultManager().initialize_vault()
        console.print(f"[green]📓 Obsidian vault ready at {settings.forge_vault_root}[/green]")
    except Exception as e:
        console.print(f"[bold yellow]⚠️ Vault bootstrap skipped: {e}[/bold yellow]")

    try:
        legacy = detect_legacy_dossiers()
        if legacy:
            console.print(
                f"[bold yellow]⚠️ {len(legacy)} legacy dossier(s) found outside the vault "
                f"(now at {settings.forge_tasks_root}). Run `issueforge migrate-vault` to relocate them."
                f"[/bold yellow]"
            )
    except Exception:
        pass

    await init_db()

    if settings.telegram_enabled:
        console.print("[bold cyan]🤖 Starting Telegram HITL Bot...[/bold cyan]")
        try:
            await telegram_manager.start()
        except Exception as e:
            console.print(f"[bold yellow]⚠️ Telegram Bot failed to start: {e}. Web dashboard continues running.[/bold yellow]")
    else:
        console.print("[yellow]ℹ️ Telegram bot disabled or token not set. Set TELEGRAM_ENABLED=true in .env to enable.[/yellow]")

    if settings.poll_enabled:
        console.print(f"[bold cyan]🔄 Starting PAT Poller Scheduler (Interval: {settings.poll_interval_hours}h)...[/bold cyan]")
        poller_service.start_scheduler()

    yield

    # Shutdown
    console.print("[bold yellow]🛑 Shutting down Issueforge...[/bold yellow]")
    if settings.poll_enabled:
        poller_service.stop_scheduler()
    try:
        from issueforge.vault.canvas_watcher import canvas_watchers
        await canvas_watchers.stop_all()
    except Exception:
        pass
    if settings.telegram_enabled:
        await telegram_manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Issueforge Orchestrator",
        description="Autonomous Multi-LLM Git Agent & Telegram HITL Orchestrator",
        version=__version__,
        lifespan=lifespan
    )

    # Mount static assets
    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from issueforge.web.auth import TokenAuthMiddleware
    app.add_middleware(TokenAuthMiddleware)

    from fastapi.responses import JSONResponse
    from issueforge.core.models import UnsafeIdError

    @app.exception_handler(UnsafeIdError)
    async def unsafe_id_handler(request, exc: UnsafeIdError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # Include API and UI routes
    app.include_router(api_router)

    return app


app = create_app()


@cli.command()
def start(
    host: str = typer.Option(settings.forge_host, help="Host interface to bind on"),
    port: int = typer.Option(settings.forge_port, help="Port to listen on"),
    reload: bool = typer.Option(settings.forge_debug, help="Auto-reload on code changes")
):
    """Start the Issueforge Web Dashboard, Webhook Server, and Telegram Bot."""
    assert_safe_binding(host, settings.forge_auth_token)
    warn_legacy_agit_config()
    if not settings.forge_auth_token:
        # The Jupyter model: loopback still gets a token, so a hostile web page cannot drive the
        # dashboard. The env var carries it into uvicorn's reload subprocess, which re-imports settings.
        settings.forge_auth_token = load_or_create_token(settings.forge_vault_root.expanduser() / "auth_token")
        os.environ["FORGE_AUTH_TOKEN"] = settings.forge_auth_token
    console.print(f"[bold green]Starting Issueforge v{__version__} on http://{host}:{port}[/bold green]")
    console.print(f"Open: [bold]http://{host}:{port}/?token={settings.forge_auth_token}[/bold]", soft_wrap=True)
    uvicorn.run("issueforge.main:app", host=host, port=port, reload=reload)


@cli.command()
def status():
    """Display the current Issueforge system and task queue status."""
    async def _status():
        await init_db()
        tasks = await list_tasks(limit=10)
        table = Table(title=f"Issueforge Tasks Status (v{__version__})")
        table.add_column("Task ID", style="cyan")
        table.add_column("Repository", style="magenta")
        table.add_column("Title", style="white")
        table.add_column("Status", style="green")
        table.add_column("Created At", style="dim")

        for t in tasks:
            table.add_row(t.id, t.repo_name, t.title[:30], t.status.value, t.created_at.strftime("%Y-%m-%d %H:%M"))

        console.print(table)

    asyncio.run(_status())


@cli.command()
def trigger(
    repo: str = typer.Option(..., "--repo", "-r", help="Git repository URL"),
    title: str = typer.Option(..., "--title", "-t", help="Task title"),
    desc: str = typer.Option(..., "--desc", "-d", help="Task description"),
    branch: str = typer.Option("main", "--branch", "-b", help="Base branch"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="Automatically start without waiting for Telegram approval")
):
    """Trigger and enqueue a new task from the command line."""
    async def _trigger():
        await init_db()
        task = await task_queue.create_and_enqueue_task(
            title=title,
            description=desc,
            repo_url=repo,
            base_branch=branch,
            task_type=TaskType.MANUAL,
            auto_approve=auto_approve
        )
        console.print(f"[bold green]✅ Task enqueued successfully: {task.id}[/bold green]")
        console.print(f"Status: [cyan]{task.status.value}[/cyan]")
        console.print(f"View in Dashboard: http://localhost:{settings.forge_port}/tasks/{task.id}")

    asyncio.run(_trigger())


def write_env_file(
    path: Path,
    gemini_api_key: Optional[str],
    github_token: Optional[str],
    gitlab_token: Optional[str],
    telegram_bot_token: Optional[str],
    telegram_user_id: Optional[str] = None,
) -> None:
    """Write a minimal .env. Never overwrites an existing one. Creates with owner-only permissions (0o600)."""
    if path.exists():
        raise FileExistsError(f"{path} already exists; edit it by hand instead.")

    def line(key: str, value: Optional[str]) -> str:
        return f"{key}={value}" if value else f"# {key}="

    content = "\n".join(
        [
            "# Written by `issueforge init`. See .env.example for every option.",
            line("GEMINI_API_KEY", gemini_api_key),
            line("GITHUB_TOKEN", github_token),
            line("GITLAB_TOKEN", gitlab_token),
            line("TELEGRAM_BOT_TOKEN", telegram_bot_token),
            *(
                # The bot ignores everyone until the allowlist names someone.
                ["TELEGRAM_ENABLED=true", line("TELEGRAM_ALLOWED_USER_IDS", telegram_user_id)]
                if telegram_bot_token
                else []
            ),
            "",
            "# The dashboard can run shell commands. Keep it on loopback unless",
            "# you set FORGE_AUTH_TOKEN as well.",
            "FORGE_HOST=127.0.0.1",
            "FORGE_PORT=8000",
            "# FORGE_AUTH_TOKEN=",
            "",
        ]
    )

    # Create atomically with owner-only permissions
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


@cli.command()
def init() -> None:
    """Create a .env file in the current directory, interactively."""
    console.print("[bold]issueforge setup[/bold] — press Enter to skip any value.\n")
    def secret(label: str) -> Optional[str]:
        return typer.prompt(label, default="", show_default=False, hide_input=True) or None

    gemini = secret("Gemini API key")
    github = secret("GitHub token (optional)")
    gitlab = secret("GitLab token (optional)")
    telegram = secret("Telegram bot token (optional)")
    telegram_user = None
    if telegram:
        telegram_user = typer.prompt("Telegram user ID (optional)", default="", show_default=False) or None

    target = Path.cwd() / ".env"
    try:
        write_env_file(target, gemini, github, gitlab, telegram, telegram_user)
    except FileExistsError as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(code=1)

    console.print(f"[green]Wrote {target}[/green]")
    console.print("Start it with: [bold]issueforge start[/bold]")


@cli.command("migrate-vault")
def migrate_vault(
    source: Optional[Path] = typer.Option(None, "--source", help="Legacy tasks dir (default: ./.issueforge/tasks)"),
    apply: bool = typer.Option(False, "--apply", help="Actually migrate. Default is a dry run."),
    include_orphans: bool = typer.Option(False, "--include-orphans", help="Also move test-* fixture dossiers."),
):
    """Relocate task dossiers from a legacy ./.issueforge/tasks directory into the Obsidian vault.

    Dry run by default. Nothing is ever deleted: migrated sources are renamed to
    `<name>.migrated` so re-running is a no-op.
    """
    async def _run():
        settings.ensure_directories()
        VaultManager().initialize_vault()
        await init_db()
        report = await plan_migration(source=source, dest=settings.forge_tasks_root,
                                      include_orphans=include_orphans)
        if not report.planned:
            console.print("[green]✅ Nothing to migrate — no legacy dossiers found.[/green]")
        else:
            if apply:
                report = apply_migration(report)
            table = Table(title=("Migrated" if apply else "Migration plan (dry run)"))
            table.add_column("Dossier")
            table.add_column("Size", justify="right")
            table.add_column("In DB", justify="center")
            table.add_column("Action")
            table.add_column("Reason")
            for plan in report.planned:
                table.add_row(
                    plan.src.name,
                    f"{plan.size_bytes / 1024:.0f} KB",
                    "yes" if plan.in_db else "no",
                    plan.action,
                    plan.reason,
                )
            console.print(table)
            moved = len([p for p in report.planned if p.action == "move"])
            console.print(f"Destination: [cyan]{settings.forge_tasks_root}[/cyan]")
            if not apply:
                console.print(f"[yellow]{moved} dossier(s) would move. Re-run with --apply.[/yellow]")
            else:
                console.print(f"[green]✅ Migrated {moved} dossier(s).[/green]")

        if report.legacy_db:
            console.print(
                f"[yellow]ℹ️ Legacy database file at {report.legacy_db} "
                f"({report.legacy_db_bytes} bytes) was left untouched; the live DB is "
                f"{settings.forge_db_path}. Remove it by hand if it is empty.[/yellow]"
            )

    asyncio.run(_run())


@cli.command("sync-knowledge")
def sync_knowledge():
    """Write an Obsidian note for every harness learning already in the database."""

    async def _run():
        settings.ensure_directories()
        VaultManager().initialize_vault()
        await init_db()
        from issueforge.vault.knowledge_vault import KnowledgeVault, backfill_notes

        written = await backfill_notes()
        console.print(
            f"[green]✅ Wrote {written} knowledge note(s) to {KnowledgeVault().root}[/green]"
        )

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
