# CLAUDE.md — Issueforge Project Context

## What is Issueforge?

Issueforge is an **autonomous, human-in-the-loop (HITL) Git engineering orchestrator** optimized for the NVIDIA Jetson Orin. It monitors GitHub/GitLab repos for issues, PRs, and work items, notifies the operator via Telegram & a web dashboard, then spins up isolated native workspaces and runs a multi-LLM agent pipeline (Planner → Coder → Tester → Reviewer) to implement changes. A Supervisory Orchestrator acts as "Tech Lead" coordinating the pipeline, asking clarifying questions, and managing branch/metadata decisions.

**Target deployment**: NVIDIA Jetson Nano Orin — no Docker, native Linux sandboxes only.

---

## Repository Layout

```
issueforge/                          # Python package root
├── __init__.py
├── main.py                    # CLI entrypoint (Typer), starts FastAPI + Telegram + Poller
├── config.py                  # Pydantic Settings from .env (Settings singleton)
│
├── agents/                    # Multi-LLM agent pipeline
│   ├── orchestrator.py        # Supervisory Orchestrator ("Tech Lead") — goal decomposition, Q&A, delegation
│   ├── planner.py             # Analyzes repo structure → step-by-step implementation plan
│   ├── coder.py               # Generates file modifications (structured JSON) → writes to sandbox
│   ├── tester.py              # Auto-detects test runners, runs tests, iterative repair loops
│   ├── reviewer.py            # Evaluates diffs, risk assessment, drafts PR descriptions
│   ├── merge_resolver.py      # MergeConflictResolver — 3-way conflict reconciliation
│   ├── engine.py              # Execution engine orchestrating the full pipeline lifecycle
│   ├── llm.py                 # LiteLLM wrapper with sequential model fallback router
│   ├── agy_runner.py          # AgySessionRunner — runs `agy` CLI inside sandbox with PTY
│   └── skills/                # Agent SKILL.md folders — shipped in the wheel, copied into each sandbox
│
├── core/                      # Core infrastructure
│   ├── models.py              # Pydantic models: Task, TaskStatus, TaskEvent, TaskQuestion, PipelineRun, etc.
│   ├── database.py            # Async SQLAlchemy + aiosqlite — task CRUD, events, questions, learnings
│   ├── sandbox.py             # Native sandbox workspaces — process groups, streaming, audit logs
│   ├── queue.py               # In-memory task queue with cancellation support
│   ├── poller.py              # PAT-based polling engine (GitLab Todos/Issues/MRs, GitHub Issues)
│   ├── events.py              # SSE event bus for real-time dashboard updates
│   ├── caching.py             # ArtifactCacheManager — plan reuse across retries
│   ├── task_dossier.py        # Permanent task dossier folders (.issueforge/tasks/{task_id}/)
│   └── terminal_manager.py    # PTY session manager for interactive xterm.js terminals
│
├── git/                       # Git platform integrations
│   ├── repo_manager.py        # Git operations: clone, branch, diff, push, remote branch discovery
│   ├── gitlab_client.py       # GitLab REST API (issues, MRs, comments, labels, metadata)
│   └── github_client.py       # GitHub REST API (issues, PRs, comments, labels)
│
├── bot/                       # Telegram HITL bot
│   ├── telegram_bot.py        # Bot lifecycle, polling, router setup (aiogram 3.x)
│   ├── handlers.py            # Callback query & command handlers (approve, reject, branch select, etc.)
│   └── messages.py            # Message formatting helpers for Telegram cards
│
├── graph/                     # Codebase dependency graph (Milestone 2)
│   ├── ast_parser.py          # CodebaseASTGraph — NetworkX import graph via stdlib `ast`
│   └── blast_radius.py        # Downstream impact, risk scoring, test selection
│
├── vault/                     # Obsidian vault subsystem (Milestone 1)
│   ├── manager.py             # VaultManager — vault bootstrap & .obsidian configuration
│   ├── linker.py              # VaultLinkIndexer — [[wikilink]] extraction & SQLite edge sync
│   ├── canvas_builder.py      # CanvasDAGBuilder — live task_dag.canvas execution DAG
│   ├── canvas_telemetry.py    # mark()/render() facade the engine calls per stage
│   ├── canvas_watcher.py      # CanvasSteeringWatcher — reverse steering from canvas notes
│   ├── knowledge_vault.py     # Harness learnings projected as linked Obsidian notes
│   └── migration.py           # Non-destructive legacy dossier migration
│
└── web/                       # FastAPI web layer
    ├── api.py                 # REST API routes + Jinja2 template rendering + SSE + WebSocket
    ├── auth.py                # Token gate: TokenAuthMiddleware, WebSocket token + Origin check, login token
    ├── static/                # CSS, JS, vendored libs (xterm.js, marked, mermaid)
    └── templates/             # Jinja2 HTML templates (dashboard, task detail, sandboxes, dossiers)

tests/                         # pytest + pytest-asyncio test suite
├── conftest.py                # Isolated temp SQLite DB + vault + workspace fixtures
├── test_webhooks.py
├── test_sandbox.py
├── test_agents.py
├── test_poller.py
├── test_models.py
├── test_orchestrator.py
├── test_telegram.py
├── test_git_manager.py
├── test_branch_and_git_metadata.py
├── test_multi_run_and_agy.py
├── test_task_dossier.py
├── test_terminal_websocket.py
├── test_obsidian_vault.py      # Vault bootstrap, wikilink indexing, dossier frontmatter
├── test_canvas_builder.py      # Canvas DAG, steering directives, telemetry
├── test_ast_blast_radius.py    # Import graph construction, downstream impact, risk bands
├── test_merge_resolver.py      # Conflict probe, reconciliation, abort-on-failure guarantees
├── test_knowledge_and_caching.py # Lesson notes, graph links, plan reuse across retries
├── test_auth.py                # Loopback binding, token gate, WebSocket Origin, login token
└── test_packaging.py           # Assets and skills inside the package, `init`, `--version`
```

---

## Tech Stack & Key Dependencies

| Layer        | Technology                                              |
|-------------|--------------------------------------------------------|
| Language     | Python ≥ 3.10                                          |
| Web framework| FastAPI + Uvicorn                                      |
| Templating   | Jinja2                                                 |
| Telegram bot | aiogram 3.x                                            |
| LLM gateway  | LiteLLM (Gemini 3.x Flash series primary, multi-provider fallback) |
| ORM / DB     | SQLAlchemy async + aiosqlite (SQLite)                  |
| Validation   | Pydantic v2 + pydantic-settings                       |
| HTTP client  | httpx (async)                                          |
| CLI          | Typer                                                  |
| Testing      | pytest + pytest-asyncio (`asyncio_mode = "auto"`)      |
| Terminal     | xterm.js (vendored) with PTY via `TerminalSessionManager` |

---

## Core Data Model

The central entity is **`Task`** (in `issueforge/core/models.py`):

- Tracks the full lifecycle via `TaskStatus` enum: `PENDING_APPROVAL → APPROVED → PLANNING → CODING → TESTING → AWAITING_CONFIRMATION → PUSHING → COMPLETED` (plus `FAILED`, `CANCELLED`, `REJECTED`, `AWAITING_INPUT`, `AWAITING_BRANCH_SELECTION`, `REVISION_REQUESTED`).
- **Multi-run support**: Each attempt is a `PipelineRun` (indexed `run-1`, `run-2`, …) stored in `Task.runs`. Failed runs never overwrite previous data.
- **Interactive Q&A**: `TaskQuestion` objects allow the Orchestrator to pause and ask the operator questions mid-flight.
- **Target branch selection**: `target_branch_candidates` + `selected_target_branch` — the system never defaults to pushing to `main`.

---

## Execution Flow

```
Webhook/Poller Ingestion
    ↓
Task created (PENDING_APPROVAL) → Telegram card + Dashboard
    ↓
Human approves → status = APPROVED
    ↓
Engine picks up task → Orchestrator analyzes & may ask questions (AWAITING_INPUT)
    ↓
Planner Agent (PLANNING) → implementation plan
    ↓
Coder Agent (CODING) → file modifications in native sandbox
    ↓
Tester Agent (TESTING) → auto-detect runner, execute tests, repair loop (up to 3 retries)
    ↓
Reviewer Agent → diff review, risk assessment, PR description draft
    ↓
Confirmation card (AWAITING_BRANCH_SELECTION → AWAITING_CONFIRMATION)
    ↓
Human confirms → PUSHING → git push + PR/MR creation → COMPLETED
```

---

## Configuration

All config via environment variables / `.env` file, loaded through `pydantic-settings` in `issueforge/config.py`. A singleton `settings` is importable from `issueforge.config`.

Key env vars:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_ENABLED` — Telegram HITL. An empty
  allowlist means the bot ignores everyone (fails closed)
- `FORGE_HOST` (default `127.0.0.1`) / `FORGE_PORT` — a non-loopback host refuses to start without
  `FORGE_AUTH_TOKEN`
- `FORGE_AUTH_TOKEN` — overrides the login token `issueforge start` otherwise creates at
  `<vault>/auth_token` (mode 0600)
- `GITHUB_WEBHOOK_SECRET` / `GITLAB_WEBHOOK_SECRET` — a webhook route skips the token only while its
  secret is set; without one the hook URL needs `?token=`
- `GITHUB_TOKEN` / `GITLAB_TOKEN` — platform PATs
- `GEMINI_API_KEY` (+ optional `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`)
- `PLANNER_MODEL`, `CODER_MODEL`, `TESTER_MODEL`, `REVIEWER_MODEL` — LiteLLM model identifiers
- `FORGE_VAULT_ROOT` — the Obsidian vault root (default: `~/.issueforge`). `FORGE_TASKS_ROOT`,
  `FORGE_WORKSPACE_ROOT` and `FORGE_DB_PATH` all derive from it unless set explicitly
- `FORGE_CANVAS_ENABLED` / `FORGE_CANVAS_WATCH_ENABLED` — canvas telemetry and the steering watcher

---

## Development Commands

```bash
# Install for development (this machine's .venv is uv-managed and has no pip: use `uv pip`)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # pytest lives only in the dev extra

# First run: write a minimal .env (never overwrites one), then start
issueforge init
issueforge start                   # binds 127.0.0.1:8000 and prints the ?token= login URL
issueforge --version

# Run tests (CI runs the same on Python 3.10 and 3.12)
pytest -q
pytest tests/test_poller.py -v
```

When testing or smoke-checking by hand, run `start`/`init` from a scratch directory with
`FORGE_VAULT_ROOT` pointed at scratch: the repository's own `.env` holds live credentials, and the
default vault holds the operator's real task history.

## Releasing

Published as `issueforge` on PyPI and at https://github.com/Justin56743/issueforge. The GitHub
`main` is an **orphan history** (it began as one squashed commit), unrelated to the private GitLab
history, so public work branches from `github/main`. The version lives only in
`issueforge/__init__.py`. To release: bump it, `uv build`, `uvx twine check dist/*`, push and wait
for CI, tag `vX.Y.Z`, then upload with `uv publish`. A PyPI version number can never be reused.

---

## Code Conventions

- **Async-first**: All I/O operations (DB, HTTP, LLM calls, subprocess) are async. Use `async/await` consistently.
- **Pydantic everywhere**: All data structures are Pydantic `BaseModel` subclasses. Use `Field(default_factory=...)` for mutable defaults.
- **Settings singleton**: Import `from issueforge.config import settings`. Never instantiate `Settings()` directly in modules.
- **LLM calls: agy first, then LiteLLM**: try an `AgySessionRunner` session, then fall back to `call_llm_with_fallback` in `issueforge/agents/llm.py`, which wraps `litellm.acompletion()` with automatic model fallback. Never call a provider SDK directly.
- **Task events**: Log significant actions as `TaskEvent` objects via the database. Use appropriate `EventType` and `AgentRole`.
- **Sandbox isolation**: Each task run gets its own directory under `.issueforge/tasks/{task_id}/sandboxes/run-{n}/`. Never share sandboxes across tasks.
- **Process groups**: Subprocesses use `start_new_session=True` and are killed via `os.killpg()` on cancellation.
- **No Docker**: The project explicitly avoids Docker. All isolation is via native filesystem directories on the Jetson.

## Testing Conventions

- Tests use `pytest-asyncio` with `asyncio_mode = "auto"` — just write `async def test_*` functions.
- `conftest.py` provides isolated fixtures: temp SQLite DB, temp workspace directory. Tests never touch production data.
- `TestResult` model has `__test__ = False` to prevent pytest collection conflicts.
- Mock external services (LLM, Git, Telegram) — don't make real API calls in tests.

---

## Important Architectural Notes

1. **Supervisory Orchestrator** (`issueforge/agents/orchestrator.py`) is the central coordinator. It triages tasks, asks clarifying questions, and delegates to specialist agents. It is NOT a simple linear script.

2. **Never push to `main` by default.** Enforced by a real HITL gate: after branch discovery
   the pipeline halts at `AWAITING_BRANCH_SELECTION` and notifies Telegram + the dashboard.
   `Task.target_branch_confirmed` is what makes this meaningful — a branch defaulted from
   `base_branch` at creation is *not* an operator choice. Confirming a branch
   (`TaskQueue.set_target_branch`) sets the flag and relaunches the run.

3. **Multi-run segregation**: Each pipeline retry creates a fresh `run-{n}` workspace. Previous failure context (stage, error, test summary) is passed to the LLM to avoid repeating mistakes, but the workspace itself is clean.

4. **Deduplication in poller**: The PAT poller uses `{repo_url}::{iid}` composite keys and URL normalization to prevent duplicate tasks. A mutex lock prevents concurrent sync operations.

5. **SSE for real-time updates**: The web dashboard uses Server-Sent Events (`/api/events/stream`) for live task status, log streaming, and terminal output.

6. **Vendored frontend assets**: xterm.js, marked.js, mermaid.js are vendored in `/static/vendor/` for offline reliability on the Jetson.

7. **The vault is the state layer, not a required UI** (see note 9 — the dashboard renders
   the canvas itself). `~/.issueforge` is a valid Obsidian vault: dossier READMEs carry YAML
   frontmatter plus `[[wikilinks]]`, and each task has a live `task_dag.canvas`. Free text
   interpolated into a README (descriptions, error messages, subtask titles) **must** go through
   `TaskDossierManager._escape_wikilinks`, or a stray `[[...]]` becomes a junk graph node.

8. **Canvas node identity is `sha1(task_id:stage)`**, never a list position. Positional indexing
   breaks the moment an operator directive card is appended. Canvas writes are atomic
   (`os.replace`) because a watcher may be reading, and every canvas call is exception-swallowing —
   telemetry must never fail a pipeline run.

9. **The canvas renders natively in the dashboard** (`renderTaskCanvas` in `dashboard.js`),
   so Obsidian is optional. The `obsidian://` link only resolves when Obsidian is installed on
   the machine viewing the page, which is usually not the Jetson serving it. Node text is
   authored larger than it renders and the auto scale has a legibility floor of 0.8 — the DAG is
   ~2460px wide, and true fit-to-width would give unreadable ~4px text.

10. **Canvas steering is queued, not injected.** `AgySessionRunner` runs `agy` one-shot via
   `--print=<prompt>` over plain pipes with no stdin, so nothing can reach a running agent.
   Directives append to `task.custom_instructions` and apply at the next agent turn. Do not
   describe this as live steering.

11. **`sync_dossier` is sync and filesystem-only** (the engine calls it ~16× per run).
    `sync_dossier_async` adds wikilink graph indexing and is awaited only at real state changes.

12. **Blast radius is advisory, never a test filter.** `issueforge/graph/` computes downstream impact
    and surfaces it to the operator and the Reviewer, but the Tester still runs the full suite.
    A missed import edge (dynamic imports, plugin registries, non-Python callers) would otherwise
    silently skip a real regression test. `BlastRadius.indeterminate` means "could not trace" —
    never read it as "no impact".

13. **The merge guard runs after commit, before push** (`push_and_create_pr`), and nowhere else:
    the Coder's changes are uncommitted until that point and git refuses to merge over a dirty
    tree. Every failure path calls `abort_merge()`, so the branch is always left exactly as it
    was — tests assert HEAD is unchanged and no `MERGE_HEAD` remains.

14. **`compileall` must run with `bytecode_free_env(sandbox)`** (`issueforge/core/sandbox.py`).
    Without it, `__pycache__` lands in the git workspace, `git add -A` sweeps it into the commit,
    and it can satisfy the "diff is non-empty" guard on its own.

15. **Knowledge notes are a projection, not a second system.** `task_learnings` in SQLite
    remains the extraction and retrieval path the Planner reads. `issueforge/vault/knowledge_vault.py`
    mirrors each row into `knowledge_vault/Lesson-*.md` linked to the same `Repo-*` / `Issue-*`
    nodes the task dossiers emit, so lessons and the work that produced them connect in the
    Obsidian graph. Do not add a parallel extraction path.

16. **Plan caching excludes CODER failures.** `issueforge/core/caching.py` reuses `PLAN.md` only when
    the previous run failed *after* planning. A coder that produced nothing may have had an
    unimplementable plan, and reusing it would loop on the same dead end. It must be applied
    *after* `clone_repository`, which wipes the workspace.

17. **Telegram Conversational Takeover**: Operators can send free-form natural language prompts directly in Telegram chat; `SupervisoryOrchestrator.interpret_operator_instruction` classifies intent, auto-approves, and deploys the pipeline autonomously.

18. **Agent artifacts are excluded at clone, not filtered at commit.** The Planner, Tester and
    Reviewer write `PLAN.md`, `TEST_RESULTS.md` and `REVIEW.md` into the workspace root, and
    `commit_changes` runs `git add -A`. `GitRepoManager._exclude_agent_artifacts()` writes them
    into `.git/info/exclude` right after the clone. Git honours ignore rules only for *untracked*
    paths, so a repository that versions its own `PLAN.md` — issueforge itself does — still diffs and
    commits real edits to it. A name-based filter at commit time would break that case.

19. **The dashboard is always token-gated.** `issueforge start` binds `127.0.0.1` by default,
    and `assert_safe_binding` refuses a non-loopback host unless `FORGE_AUTH_TOKEN` was set by the
    operator. It runs *before* the auto-generated login token is applied, so that token can never
    license a public bind. `create_app()` itself is ungated when no token is set, which is what the
    tests rely on. The token is accepted as `Authorization: Bearer`, `?token=`, or the
    `forge_token` cookie that a `?token=` visit sets (SameSite=Strict). WebSockets accept only
    `?token=` or the cookie, because browsers cannot set headers on them.

20. **WebSockets need their own guard.** `BaseHTTPMiddleware` never sees WebSocket connections, so
    `terminal_websocket` calls `websocket_is_allowed` before `accept()`. It checks the token and
    rejects a present `Origin` whose host:port differs from `Host`: browsers apply no CORS to
    WebSockets. Any new WebSocket route must call it too. (A bash sandbox-shell route existed
    with no UI reaching it; it was removed rather than guarded — don't bring it back.)

21. **Quote everything interpolated into a shell command.** `NativeSandbox.run_command` uses
    `create_subprocess_shell`, so file paths, branch names, commit messages and URLs go through
    `shlex.quote`. A commit message once expanded `$(...)`.

22. **Agents never run in the server's working directory.** Each agent gets a sandbox, and the
    operator-command agent runs in `<workspace_root>/operator`. `Path.cwd()` is the directory the
    operator happened to start from, and agents run with `--dangerously-skip-permissions`.

23. **The wheel must contain the application.** Templates, static assets and `agents/skills/` ship
    through `[tool.setuptools.package-data]`. A new non-Python file outside those globs installs
    cleanly and then 500s. `tests/test_packaging.py` checks the source tree, not the built wheel.

---

## Future Direction (from `thot.md`)

The sandbox model is evolving toward a **self-evolving harness** — an agent that can solve, memorize, and document problems, then autonomously improve its own tools and skills based on the Git tasks, PRs, and work items it processes.
