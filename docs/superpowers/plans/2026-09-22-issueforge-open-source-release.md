# issueforge Open Source Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the private, single-operator `agit` repository into `issueforge` — an MIT-licensed open source project that any developer can `pip install` and run safely on their own machine.

**Architecture:** No architectural change. The existing single-user design *is* the design being shipped; users each run their own instance with their own keys. The work is subtraction (dead code, stale specs), renaming, closing the unauthenticated-network hole, and making the wheel actually contain the application.

**Tech Stack:** Python ≥3.10, FastAPI, SQLAlchemy async + aiosqlite, Typer, setuptools, pytest. No new runtime dependencies are introduced by this plan.

**Spec:** This document is self-contained. The decisions it implements were settled in session on 2026-09-22:

- Distribution: PyPI package plus a public GitHub repository. Not a hosted service.
- Name: `issueforge` (PyPI `agit` is taken by an unrelated project). Full rename, one commit.
- License: MIT. Personal project, author holds the copyright.
- Users install and run their own instance. No multi-tenancy, no accounts, no quotas.

## Global Constraints

- Python ≥ 3.10. Linux only — the sandbox uses POSIX process groups and PTYs.
- The full test suite must pass at the end of every task. Baseline at plan time: **198 passed**.
- Run tests with `.venv/bin/python -m pytest -q`.
- No new runtime dependency may be added without being named and justified in the task that adds it. This plan adds none.
- Every task ends in a commit. Commit messages follow Conventional Commits.
- Prose written to disk — docs, comments, commit messages, README — is normal English regardless of the session's reply style.
- The vault root becomes `~/.issueforge` and the environment variable prefix becomes `FORGE_`. These are assumptions of Task 5; if the author prefers `ISSUEFORGE_`, change them there and nowhere else.

---

## File Structure

Files this plan creates:

| Path | Responsibility |
|---|---|
| `LICENSE` | MIT license text, copyright the author |
| `issueforge/web/auth.py` | Single middleware enforcing the access token, plus the loopback-binding rule |
| `issueforge/agents/skills/` | The five agent skill folders, moved inside the package so the wheel carries them |
| `tests/test_auth.py` | Token enforcement, loopback default, webhook exemption |
| `tests/test_packaging.py` | Asserts templates, static assets and skills resolve from the installed package |
| `.github/workflows/ci.yml` | Runs the suite on push and pull request |

Files this plan deletes:

| Path | Why |
|---|---|
| `context.md`, `new-context.md` | Specify a Google ADK rewrite that was deliberately never built. Actively misleads readers. |
| `PLAN.md`, `REVIEW.md`, `TEST_RESULTS.md` | Agent artifacts committed by an earlier pipeline run, containing another machine's paths and stale test counts. |

Files this plan renames: the whole `agit/` package tree becomes `issueforge/`, in Task 5.

---

## Sprint 0 — Subtraction

The repository carries roughly 2100 lines that no caller reaches and no reader benefits from. Removing them first means every later task touches less code.

### Task 1: Delete unreachable functions

Every symbol below was verified to have zero references across `agit/`, `tests/`, the templates and the JavaScript. They are deleted, not deprecated.

**Files:**
- Modify: `agit/git/repo_manager.py` — remove `probe_and_merge`, `finalize_merge`, `checkout_target_branch`
- Modify: `agit/core/database.py` — remove `add_pipeline_run`, `update_pipeline_run`, `update_task_subtasks`
- Modify: `agit/git/github_client.py` — remove `get_issue`, `list_pull_request_comments`, `create_pull_request_comment`
- Modify: `agit/git/gitlab_client.py` — remove `create_merge_request_note`, `list_merge_request_notes`
- Modify: `agit/core/terminal_manager.py` — remove `close_shell`
- Modify: `agit/graph/ast_parser.py` — remove `dependencies_of`
- Modify: `agit/vault/canvas_builder.py` — remove `list_steering_directives`
- Modify: `agit/vault/knowledge_vault.py` — remove `list_notes`
- Modify: `tests/test_issue_closing.py:77,128` — two `patch("agit.git.repo_manager.GitRepoManager.probe_and_merge", ...)` lines that mock a function which no longer exists

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. This task only removes.

- [ ] **Step 1: Prove they are unreferenced**

Run this before deleting anything. It must print `0` for every name.

```bash
for s in probe_and_merge finalize_merge checkout_target_branch \
         add_pipeline_run update_pipeline_run update_task_subtasks \
         get_issue list_pull_request_comments create_pull_request_comment \
         create_merge_request_note list_merge_request_notes close_shell \
         dependencies_of list_steering_directives list_notes; do
  n=$(grep -rn "\b$s\b" agit/ --include=*.py --include=*.js --include=*.html \
        | grep -v "def $s" | wc -l)
  echo "$n  $s"
done
```

Expected: `0` beside every name. `probe_and_merge` will show `0` here because the grep excludes `tests/`; its only references are the two mock lines in `tests/test_issue_closing.py`, removed in step 2.

- [ ] **Step 2: Delete each function and the two stale mocks**

Delete the whole `def` block for each symbol, including its docstring and decorators. In `tests/test_issue_closing.py`, remove these two lines and leave the surrounding `with` blocks otherwise intact:

```python
         patch("agit.git.repo_manager.GitRepoManager.probe_and_merge", new=AsyncMock(return_value=(False, []))), \
```

- [ ] **Step 3: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `198 passed`. Any failure means a symbol was reachable after all — restore that one function and re-check with a wider grep that includes `tests/`.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: delete unreachable functions across git, core and vault layers"
```

### Task 2: Collapse the duplicated LLM helpers

`_setup_api_keys` is copy-pasted into four agents and is redundant besides — `call_llm_with_fallback` already calls `setup_llm_api_keys()` at `agit/agents/llm.py:38`. `_extract_json` is copy-pasted into three agents with three slightly different regexes.

**Files:**
- Modify: `agit/agents/llm.py` — add `extract_json`
- Modify: `agit/agents/planner.py` — remove `_setup_api_keys` and its call
- Modify: `agit/agents/coder.py` — remove both helpers, switch call sites
- Modify: `agit/agents/reviewer.py` — remove both helpers, switch call sites
- Modify: `agit/agents/orchestrator.py` — remove both helpers, switch call sites
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: `setup_llm_api_keys()` — already exists in `agit/agents/llm.py`.
- Produces: `extract_json(text: str) -> Optional[Dict[str, Any]]` in `agit/agents/llm.py`. Later tasks and all four agents import it as `from agit.agents.llm import extract_json`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agents.py`:

```python
def test_extract_json_handles_every_shape_the_agents_see():
    from agit.agents.llm import extract_json

    # Bare JSON
    assert extract_json('{"a": 1}') == {"a": 1}
    # Fenced with a language tag, as Gemini returns it
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    # Fenced without a tag
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}
    # Prose on both sides, which is the common failure case
    assert extract_json('Sure, here it is:\n{"a": 1}\nHope that helps.') == {"a": 1}
    # Nested braces must not truncate at the first closing brace
    assert extract_json('{"a": {"b": 2}}') == {"a": {"b": 2}}
    # Unparseable input returns None rather than raising
    assert extract_json("not json at all") is None
    assert extract_json("") is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agents.py::test_extract_json_handles_every_shape_the_agents_see -q`
Expected: FAIL with `ImportError: cannot import name 'extract_json'`.

- [ ] **Step 3: Add the single implementation**

Append to `agit/agents/llm.py`:

```python
def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse the first JSON object in an LLM response, fenced or bare.

    Returns None rather than raising: every caller treats "no JSON" as a
    recoverable outcome and falls back to its own heuristics.
    """
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except Exception:
        return None
```

Add `import json` and `import re` to the imports at the top of `agit/agents/llm.py` if they are not already present.

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_agents.py::test_extract_json_handles_every_shape_the_agents_see -q`
Expected: PASS.

- [ ] **Step 5: Delete the four copies of `_setup_api_keys`**

In `agit/agents/planner.py`, `coder.py`, `reviewer.py` and `orchestrator.py`: delete the `_setup_api_keys` method and delete every `self._setup_api_keys()` call. Do not replace the calls with anything — `call_llm_with_fallback` already sets the keys, and the `agy` path takes its key from the environment the runner builds.

- [ ] **Step 6: Delete the three copies of `_extract_json`**

In `agit/agents/coder.py`, `reviewer.py` and `orchestrator.py`: delete the `_extract_json` method, add `extract_json` to the existing `from agit.agents.llm import ...` line, and rewrite each call site from `self._extract_json(x)` to `extract_json(x)`.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `199 passed` (198 plus the new test).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(agents): single extract_json, drop redundant per-agent key setup"
```

### Task 3: Delete the stale specification and artifact documents

**Files:**
- Delete: `context.md`, `new-context.md`, `PLAN.md`, `REVIEW.md`, `TEST_RESULTS.md`
- Modify: `README.md` — remove references to the deleted files if any exist

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Confirm nothing references them**

```bash
grep -rn "new-context\|context\.md\|TEST_RESULTS\|REVIEW\.md" \
  --include=*.py --include=*.md --include=*.html . \
  | grep -v "^./docs/superpowers/plans/"
```

Expected: hits only inside `CLAUDE.md` and `IMPLEMENTATION_PLAN.md` prose, which are edited in the next step. Note that `agit/agents/reviewer.py` and `agit/agents/tester.py` write `REVIEW.md` and `TEST_RESULTS.md` *into sandboxes* — that is a different file in a different directory. Do not touch that code.

- [ ] **Step 2: Delete the files and their mentions**

```bash
git rm context.md new-context.md PLAN.md REVIEW.md TEST_RESULTS.md
```

Then remove the sentence mentioning `thot.md`'s sibling specs from `CLAUDE.md`'s "Future Direction" section only if it names a deleted file, and update the `**Specification Reference:**` line at the top of `IMPLEMENTATION_PLAN.md` to point at this plan instead of `new-context.md`.

- [ ] **Step 3: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `199 passed`. No test reads these files; a failure means one did.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs: remove superseded ADK specifications and committed agent artifacts"
```

---

## Sprint 1 — Rename

### Task 4: Rename the project to issueforge

One commit, mechanically applied, verified by the test suite. Doing this before the packaging work means no later task has to be redone.

**Files:**
- Rename: `agit/` → `issueforge/` (the whole tree)
- Modify: every `.py` under `issueforge/` and `tests/` — import paths
- Modify: `issueforge/config.py` — environment aliases and vault default
- Modify: `pyproject.toml` — project name, script entry point, package discovery
- Modify: `systemd/agit.service` → `systemd/issueforge.service`
- Modify: `README.md`, `CLAUDE.md`, `IMPLEMENTATION_PLAN.md`, `.env.example`, `docs/*.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the import root `issueforge`, the console command `issueforge`, the environment prefix `FORGE_`, and the state directory `~/.issueforge`. Every later task uses these names.

- [ ] **Step 1: Move the package**

```bash
git mv agit issueforge
git mv systemd/agit.service systemd/issueforge.service
```

- [ ] **Step 2: Rewrite import paths**

```bash
grep -rl "\bagit\." --include=*.py . | xargs sed -i 's/\bagit\./issueforge./g'
grep -rl "from agit import\|import agit\b" --include=*.py . \
  | xargs sed -i 's/from agit import/from issueforge import/g; s/^import agit\b/import issueforge/g'
```

Then check for stragglers — string literals used in `patch()` calls are the usual miss:

```bash
grep -rn "['\"]agit\." --include=*.py . | head -20
```

Expected after the sed: no output. Anything printed is a mock target string that must be rewritten by hand.

- [ ] **Step 3: Rename environment variables and the state directory**

In `issueforge/config.py`, change every `Field(..., alias="AGIT_X")` to `alias="FORGE_X"`, and change the vault default from `Path.home() / ".agit"` to `Path.home() / ".issueforge"`. Apply the same rename to `.env.example` and to every `AGIT_` mention in the documentation.

The settings field *names* (`agit_vault_root` and friends) also rename to `forge_vault_root` etc. — `tests/conftest.py` patches them by name and will fail loudly if a rename is missed, which is the point.

- [ ] **Step 4: Update packaging metadata**

In `pyproject.toml`:

```toml
[project]
name = "issueforge"
version = "0.1.0"
description = "Autonomous multi-LLM Git engineering agent with a human approval gate"

[project.scripts]
issueforge = "issueforge.main:cli"

[tool.setuptools.packages.find]
include = ["issueforge*"]
```

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `199 passed`. Import errors here mean a missed path; `AttributeError` on settings means a missed field rename.

- [ ] **Step 6: Verify the command still starts**

Run: `.venv/bin/pip install -e . && .venv/bin/issueforge --help`
Expected: the Typer help listing `start`, `trigger`, `migrate-vault` and `sync-knowledge`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: rename agit to issueforge across package, config and docs"
```

- [ ] **Step 8: Move your own state directory**

Not part of the commit. On any machine that ran the old version:

```bash
mv ~/.agit ~/.issueforge
```

---

## Sprint 2 — Close the network hole

Today `FORGE_HOST` defaults to `0.0.0.0`, no route requires authentication, and `/api/sandboxes/{task_id}/{run_id}/shell` is an interactive shell over WebSocket. Anyone who can reach the port gets command execution as the user who started the server. That is survivable on a private Jetson and unacceptable in a package strangers install.

### Task 5: Default to loopback and refuse unauthenticated exposure

**Files:**
- Modify: `issueforge/config.py` — host default, new token field
- Modify: `issueforge/main.py` — startup guard in the `start` command
- Test: `tests/test_auth.py` (create)

**Interfaces:**
- Consumes: `settings.forge_host`, `settings.forge_port`.
- Produces: `settings.forge_auth_token: Optional[str]` and `issueforge.main.assert_safe_binding(host: str, token: Optional[str]) -> None`, which raises `SystemExit` with an explanatory message when a non-loopback host has no token. Task 6 reuses the same setting.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth.py`:

```python
import pytest


def test_loopback_binding_needs_no_token():
    from issueforge.main import assert_safe_binding

    assert_safe_binding("127.0.0.1", None)
    assert_safe_binding("localhost", None)
    assert_safe_binding("::1", None)


def test_public_binding_without_a_token_refuses_to_start():
    from issueforge.main import assert_safe_binding

    with pytest.raises(SystemExit) as exc:
        assert_safe_binding("0.0.0.0", None)
    assert "FORGE_AUTH_TOKEN" in str(exc.value)


def test_public_binding_with_a_token_is_allowed():
    from issueforge.main import assert_safe_binding

    assert_safe_binding("0.0.0.0", "a-long-random-token")


def test_default_host_is_loopback():
    from issueforge.config import settings

    assert settings.forge_host in ("127.0.0.1", "localhost")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_auth.py -q`
Expected: FAIL — `ImportError` on `assert_safe_binding`, and the default-host test fails because the default is still `0.0.0.0`.

- [ ] **Step 3: Change the default and add the token setting**

In `issueforge/config.py`:

```python
    forge_host: str = Field(default="127.0.0.1", alias="FORGE_HOST")
    forge_port: int = Field(default=8000, alias="FORGE_PORT")
    forge_auth_token: Optional[str] = Field(default=None, alias="FORGE_AUTH_TOKEN")
```

- [ ] **Step 4: Add the startup guard**

In `issueforge/main.py`, above the `start` command:

```python
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
```

Call it as the first statement inside `start`, before the console banner:

```python
    assert_safe_binding(host, settings.forge_auth_token)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_auth.py -q`
Expected: 4 passed.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `203 passed`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "fix(security): bind loopback by default, refuse unauthenticated exposure"
```

### Task 6: Enforce the token on every request when one is set

**Files:**
- Create: `issueforge/web/auth.py`
- Modify: `issueforge/main.py` — register the middleware
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `settings.forge_auth_token` from Task 5.
- Produces: `TokenAuthMiddleware` in `issueforge/web/auth.py`, registered in `create_app()`.

Design, deliberately minimal: when no token is configured the middleware is a pass-through, so the loopback experience is unchanged and no existing test breaks. When a token is configured, every request needs it — as an `Authorization: Bearer` header, a `token` query parameter (WebSockets and `EventSource` cannot set headers), or the `forge_token` cookie that a correct query parameter sets. Webhook routes are exempt because they already verify platform signatures (`GitHubClient.verify_webhook_signature`, `GitLabClient.verify_webhook_token`) and third parties cannot know the local token.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth.py`:

```python
from fastapi.testclient import TestClient


def _client_with_token(monkeypatch, token):
    from issueforge.config import settings
    from issueforge.main import create_app

    monkeypatch.setattr(settings, "forge_auth_token", token)
    return TestClient(create_app())


def test_no_token_configured_means_no_gate(monkeypatch):
    client = _client_with_token(monkeypatch, None)
    assert client.get("/api/tasks").status_code == 200


def test_request_without_token_is_rejected(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    assert client.get("/api/tasks").status_code == 401


def test_bearer_header_is_accepted(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    res = client.get("/api/tasks", headers={"Authorization": "Bearer secret-token"})
    assert res.status_code == 200


def test_query_parameter_is_accepted_and_sets_a_cookie(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    res = client.get("/?token=secret-token")
    assert res.status_code == 200
    assert client.cookies.get("forge_token") == "secret-token"


def test_wrong_token_is_rejected(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    assert client.get("/api/tasks?token=wrong").status_code == 401


def test_webhooks_stay_reachable_because_they_verify_signatures(monkeypatch):
    client = _client_with_token(monkeypatch, "secret-token")
    res = client.post("/webhooks/github", json={}, headers={"X-GitHub-Event": "issues"})
    # 401 from the signature check is fine; 401 from the token gate is not.
    # The distinguishing marker is that the middleware never ran.
    assert res.status_code != 401 or "webhook" in res.text.lower()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_auth.py -q`
Expected: the four gated tests fail — every request currently returns 200.

- [ ] **Step 3: Write the middleware**

Create `issueforge/web/auth.py`:

```python
import hmac
import logging
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from issueforge.config import settings

logger = logging.getLogger("issueforge.web.auth")

# Webhooks authenticate themselves with a platform signature, and the sending
# party cannot know this instance's token. Health checks must stay reachable
# for process supervisors.
EXEMPT_PREFIXES = ("/webhooks/", "/healthz")


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Require a shared token when one is configured. Pass through when not.

    Loopback installs configure no token and behave exactly as before. Only an
    operator who deliberately exposed the port sets one, and for them every
    route is gated, including the WebSocket shell.
    """

    async def dispatch(self, request: Request, call_next):
        expected: Optional[str] = settings.forge_auth_token
        if not expected or request.url.path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        supplied = self._supplied_token(request)
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized."}, status_code=401)

        response = await call_next(request)
        if request.query_params.get("token"):
            # Let the browser stop carrying the token in every URL.
            response.set_cookie(
                "forge_token", expected, httponly=True, samesite="strict"
            )
        return response

    @staticmethod
    def _supplied_token(request: Request) -> Optional[str]:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.query_params.get("token") or request.cookies.get("forge_token")
```

- [ ] **Step 4: Register it**

In `issueforge/main.py`, inside `create_app()`, before `app.include_router(api_router)`:

```python
    from issueforge.web.auth import TokenAuthMiddleware

    app.add_middleware(TokenAuthMiddleware)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_auth.py -q`
Expected: all pass.

- [ ] **Step 6: Guard the WebSocket routes**

`BaseHTTPMiddleware` does not see WebSocket connections. Add an explicit check at the top of both WebSocket handlers in `issueforge/web/api.py` — `terminal_websocket` and `sandbox_shell_websocket` — immediately before `await websocket.accept()`:

```python
    expected = settings.forge_auth_token
    if expected:
        supplied = websocket.query_params.get("token") or websocket.cookies.get("forge_token")
        if not supplied or not hmac.compare_digest(supplied, expected):
            await websocket.close(code=1008)
            return
```

Add `import hmac` to that module's imports. The dashboard JavaScript that opens these sockets must append `?token=` when the page was loaded with one; the cookie set in step 3 covers the normal browser case.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `209 passed`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(security): token gate on every route including the WebSocket shell"
```

---

## Sprint 3 — Make the wheel work

A `pip install` today would produce a package with no templates, no static assets and no agent skills — it would crash on first request.

### Task 7: Move the agent skills inside the package

`AgySessionRunner._ensure_skills_present` looks for `.agents/skills` three directories above the module, which is the repository root during development and `site-packages/.agents` after installation, where it will never exist.

**Files:**
- Move: `.agents/skills/` → `issueforge/agents/skills/`
- Modify: `issueforge/agents/agy_runner.py` — resolution order
- Test: `tests/test_packaging.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: skills resolvable from the installed package. Task 8 ships them via `package-data`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_packaging.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_packaging.py -q`
Expected: the skills test fails; the templates test already passes because those files are inside the package.

- [ ] **Step 3: Move the skills**

```bash
git mv .agents/skills issueforge/agents/skills
rmdir .agents 2>/dev/null || true
```

- [ ] **Step 4: Fix the resolution order**

In `issueforge/agents/agy_runner.py`, in `_ensure_skills_present`, replace the `source_candidates` list with:

```python
            source_candidates = [
                Path(__file__).resolve().parent / "skills",   # installed package
                Path.cwd() / ".agents" / "skills",            # repository checkout override
                Path.home() / ".gemini" / "skills",           # operator's own skills
            ]
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_packaging.py tests/test_multi_run_and_agy.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "fix(packaging): ship agent skills inside the package"
```

### Task 8: Ship the assets and split the development dependencies

**Files:**
- Modify: `pyproject.toml`
- Test: a clean-virtualenv install, run by hand

**Interfaces:**
- Consumes: the layout established in Task 7.
- Produces: a wheel that contains templates, static assets and skills, and that does not install pytest.

- [ ] **Step 1: Declare the package data and move the test dependencies**

In `pyproject.toml`, raise the setuptools floor (the `**` glob syntax needs ≥64), remove `pytest` and `pytest-asyncio` from `[project].dependencies`, and add:

```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project.optional-dependencies]
dev = ["pytest>=8.2.0", "pytest-asyncio>=0.23.7"]

[tool.setuptools.package-data]
issueforge = [
    "web/templates/*.html",
    "web/static/**/*",
    "agents/skills/**/*",
]
```

- [ ] **Step 2: Build the wheel**

```bash
.venv/bin/pip install build
.venv/bin/python -m build --wheel
```

Expected: `dist/issueforge-0.1.0-py3-none-any.whl` is produced.

- [ ] **Step 3: Verify the wheel actually contains the application**

```bash
.venv/bin/python -m zipfile -l dist/issueforge-0.1.0-py3-none-any.whl \
  | grep -cE "web/templates/.*\.html|web/static/js/dashboard\.js|agents/skills/ponytail"
```

Expected: a count of at least 3. Zero means `package-data` did not match — check the patterns before going further.

- [ ] **Step 4: Install into a clean virtualenv and start it**

```bash
python3 -m venv /tmp/forge-smoke
/tmp/forge-smoke/bin/pip install dist/issueforge-0.1.0-py3-none-any.whl
/tmp/forge-smoke/bin/issueforge --help
/tmp/forge-smoke/bin/python -c "import pytest" 2>&1 | tail -1
```

Expected: help text prints; the last command prints `ModuleNotFoundError: No module named 'pytest'`, proving the test framework is no longer a runtime dependency.

- [ ] **Step 5: Confirm the dashboard renders from the installed copy**

```bash
/tmp/forge-smoke/bin/issueforge start --port 8899 &
sleep 5
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8899/
kill %1
```

Expected: `200`. A `500` means templates or static assets are still missing from the wheel.

- [ ] **Step 6: Commit**

```bash
rm -rf /tmp/forge-smoke dist
git add -A
git commit -m "build: ship templates, assets and skills in the wheel; split dev extras"
```

### Task 9: Add a first-run configuration wizard

A new user currently has to find `.env.example`, copy it and guess which of thirty variables matter. Four of them do.

**Files:**
- Modify: `issueforge/main.py` — new `init` command
- Test: `tests/test_packaging.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the `issueforge init` command, which writes a `.env` file in the current directory.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_packaging.py`:

```python
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


def test_init_refuses_to_clobber_an_existing_env(tmp_path):
    import pytest

    from issueforge.main import write_env_file

    target = tmp_path / ".env"
    target.write_text("GEMINI_API_KEY=already-here\n")

    with pytest.raises(FileExistsError):
        write_env_file(target, gemini_api_key="new", github_token=None,
                       gitlab_token=None, telegram_bot_token=None)
    assert "already-here" in target.read_text()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_packaging.py -q`
Expected: FAIL with `ImportError: cannot import name 'write_env_file'`.

- [ ] **Step 3: Implement the writer and the command**

In `issueforge/main.py`:

```python
def write_env_file(
    path: Path,
    gemini_api_key: Optional[str],
    github_token: Optional[str],
    gitlab_token: Optional[str],
    telegram_bot_token: Optional[str],
) -> None:
    """Write a minimal .env. Never overwrites an existing one."""
    if path.exists():
        raise FileExistsError(f"{path} already exists; edit it by hand instead.")

    def line(key: str, value: Optional[str]) -> str:
        return f"{key}={value}" if value else f"# {key}="

    path.write_text(
        "\n".join(
            [
                "# Written by `issueforge init`. See .env.example for every option.",
                line("GEMINI_API_KEY", gemini_api_key),
                line("GITHUB_TOKEN", github_token),
                line("GITLAB_TOKEN", gitlab_token),
                line("TELEGRAM_BOT_TOKEN", telegram_bot_token),
                "",
                "# The dashboard can run shell commands. Keep it on loopback unless",
                "# you set FORGE_AUTH_TOKEN as well.",
                "FORGE_HOST=127.0.0.1",
                "FORGE_PORT=8000",
                "# FORGE_AUTH_TOKEN=",
                "",
            ]
        ),
        encoding="utf-8",
    )


@cli.command()
def init() -> None:
    """Create a .env file in the current directory, interactively."""
    console.print("[bold]issueforge setup[/bold] — press Enter to skip any value.\n")
    gemini = typer.prompt("Gemini API key", default="", show_default=False) or None
    github = typer.prompt("GitHub token (optional)", default="", show_default=False) or None
    gitlab = typer.prompt("GitLab token (optional)", default="", show_default=False) or None
    telegram = typer.prompt("Telegram bot token (optional)", default="", show_default=False) or None

    target = Path.cwd() / ".env"
    try:
        write_env_file(target, gemini, github, gitlab, telegram)
    except FileExistsError as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(code=1)

    console.print(f"[green]Wrote {target}[/green]")
    console.print("Start it with: [bold]issueforge start[/bold]")
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_packaging.py -q`
Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: `213 passed`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(cli): issueforge init writes a minimal .env interactively"
```

### Task 10: License and README

**Files:**
- Create: `LICENSE`
- Modify: `README.md`

**Interfaces:**
- Consumes: the command names and settings established in Tasks 4, 5 and 9.
- Produces: nothing code-facing.

- [ ] **Step 1: Write the MIT license**

Create `LICENSE` containing the standard MIT text, `Copyright (c) 2026 <author name>`. Take the exact wording from https://opensource.org/license/mit — do not paraphrase it.

- [ ] **Step 2: Add the license to the metadata**

In `pyproject.toml` under `[project]`:

```toml
license = "MIT"
license-files = ["LICENSE"]
```

- [ ] **Step 3: Rewrite the README opening**

The current README opens with "enterprise-grade" and a marketing table. Replace the top section with what a stranger needs in the first thirty seconds: what it does, the install command, the four required settings, and — prominently, not in a footnote — the two safety facts:

- The dashboard executes shell commands, so it binds `127.0.0.1` by default and exposing it requires `FORGE_AUTH_TOKEN`.
- Agents run `agy` with `--dangerously-skip-permissions` inside the sandbox directory, so an agent can modify anything the user running it can modify.

Keep the architecture sections. Delete the "Quick Start on Jetson Nano Orin" framing — the Jetson is where it was built, not a requirement.

- [ ] **Step 4: Verify every command in the README runs**

Copy each shell command out of the README and run it. Fix the README, not the code, when one fails.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: MIT license and a README written for a first-time reader"
```

---

## Sprint 4 — Publish

### Task 11: Continuous integration

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the `dev` extra from Task 8.
- Produces: a green check on every push.

- [ ] **Step 1: Write the workflow**

```yaml
name: tests

on:
  push:
  pull_request:

jobs:
  pytest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: pytest -q
```

- [ ] **Step 2: Verify the suite passes without agy installed**

CI runners have no `agy` binary. Confirm locally that this does not matter:

```bash
env PATH=/usr/bin:/bin .venv/bin/python -m pytest -q
```

Expected: `213 passed`. Any failure here is a test that shells out to a real `agy` and must be mocked before CI can be green.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "ci: run the suite on 3.10 and 3.12"
```

### Task 12: Publish the repository and the package

**Files:** none — this task is release mechanics.

- [ ] **Step 1: Final secret sweep**

```bash
git rev-list --all | while read c; do
  git grep -I -nE "(AIza[0-9A-Za-z_-]{20,}|ghp_[0-9A-Za-z]{20,}|glpat-[0-9A-Za-z_-]{15,}|sk-[0-9A-Za-z]{20,}|[0-9]{8,10}:AA[0-9A-Za-z_-]{30,})" $c -- 2>/dev/null
done | grep -v "your_\|test\|dummy\|example" | sort -u
```

Expected: no output. This was verified clean on 2026-09-22; re-run because the rename touched every file.

- [ ] **Step 2: Create the GitHub repository and push**

```bash
gh repo create issueforge --public --source=. --remote=github --push
```

The existing GitLab remote stays as `origin`; GitHub becomes the public home.

- [ ] **Step 3: Confirm CI is green before announcing anything**

```bash
gh run watch
```

- [ ] **Step 4: Publish to PyPI**

```bash
.venv/bin/python -m build
.venv/bin/pip install twine
.venv/bin/twine upload dist/*
```

- [ ] **Step 5: Verify the published package installs from PyPI**

```bash
python3 -m venv /tmp/forge-pypi
/tmp/forge-pypi/bin/pip install issueforge
/tmp/forge-pypi/bin/issueforge --help
rm -rf /tmp/forge-pypi
```

Expected: help text. This is the exact path a stranger takes.

- [ ] **Step 6: Tag the release**

```bash
git tag -a v0.1.0 -m "First public release"
git push github v0.1.0
```

---

## Not in this plan

**The frontend redesign.** It needs a design pass with the `frontend-design` skill before it can be broken into tasks, and writing implementation steps before the design exists would be inventing them. It gets its own plan once the visual direction is settled. It does not block publishing — the current dashboard works.

**Hosted multi-tenant service.** Explicitly deferred. Nothing in this plan blocks it later, and shipping the package first is the cheapest way to learn whether anyone wants the hosted version.

**Collapsing the agy-then-LiteLLM scaffolding.** The ponytail review counted roughly 120 removable lines in the "try `agy`, fall back to LiteLLM" block repeated across seven call sites, and the cleanup was approved in full. It is held back from this plan on purpose: it is a behaviour-preserving refactor of the single most exercised path in the system, the seven copies differ in ways that matter (the Planner checks `PLAN.md` on disk, the others parse JSON), and it would land in the middle of a rename. It belongs in its own pass with the `safe-refactor` discipline, after the release is out. Tasks 1 through 3 still remove the other ~2000 lines.

**Sandbox hardening with bubblewrap.** Worth doing — it would stop an agent from wandering outside its workspace into the user's home directory — but it is a safety improvement for the user's own machine, not a release blocker, and it changes how every command is executed. Separate plan.
