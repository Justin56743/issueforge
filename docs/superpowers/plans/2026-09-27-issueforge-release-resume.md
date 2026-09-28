# Resume the issueforge release — Tasks 4 (re-review) through 11

## Context

`agit` is being published as **issueforge**: an MIT-licensed pip package each user installs and runs on their own machine. The hosted multi-tenant version was designed and then deliberately dropped — with under ten invite-only users and no hosting budget, self-install removes auth, tenancy, quotas and deployment from scope entirely.

The work is already half done. A 12-task plan exists at `docs/superpowers/plans/2026-09-22-issueforge-open-source-release.md` and Tasks 1-4 are committed and pushed. **This is not a new plan — it is the resume document for that one.** The existing plan stays as written except for three corrections listed below; rewriting 1082 lines of already-executed steps would be pure churn.

What remains: close out Task 4, then Tasks 5-11, stopping before publication.

## Current state (verified 2026-09-27)

- Branch `issueforge-release`, HEAD `b74c9c0`, working tree clean, pushed to `origin`.
- 199 tests collected and passing.
- Tasks 1, 2, 3 complete and reviewed clean. Task 4 implemented, reviewed, fixed — **but its scoped re-review never ran**, so Task 4 is not complete.
- Execution runs through `superpowers:subagent-driven-development`. The ledger is `.superpowers/sdd/2026-09-22-issueforge-open-source-release/progress.md` (git-ignored, on disk) and holds every ruling made so far. Briefs for Tasks 5 and 6 are already extracted there.

## Corrections to the existing plan

Found by re-verifying the plan's assumptions against the renamed codebase. Apply these when executing; do not edit the plan document.

**1. Task 6's webhook exemption is wrong and would break webhook delivery.** The plan's `EXEMPT_PREFIXES` uses `/webhooks/`, but the real routes are `/api/webhooks/github` (`issueforge/web/api.py:42`) and `/api/webhooks/gitlab` (`:115`). With the plan's value, a configured token would reject every inbound webhook. Use:

```python
EXEMPT_PREFIXES = ("/api/webhooks/",)
```

**2. There is no health-check route.** Drop `/healthz` from the exemption tuple — exempting a path that does not exist is dead config.

**3. Task 6's WebSocket paths.** The two endpoints are `/ws/tasks/{task_id}/terminal` (`api.py:499`) and `/ws/tasks/{task_id}/sandbox-shell` (`:535`). The function names in the plan (`terminal_websocket`, `sandbox_shell_websocket`) are correct; only the paths in the surrounding prose were wrong.

Everything else the plan assumes still holds: `forge_host` still defaults to `"0.0.0.0"` (`config.py:21`), `pytest` is still a runtime dependency, there is no `[tool.setuptools.package-data]`, no `LICENSE`, no `.github/workflows/`, `.agents/skills/` is still at the repo root with `issueforge/agents/skills/` absent, and no middleware is registered anywhere.

## Execution sequence

**Step 0 — close Task 4.** Build the scoped package over `45db434..b74c9c0` and re-review only that range. Verify the two findings: the three banners now read `issueforge@sandbox`, and the README upgrade note gives **both** move commands. The note is at `README.md:57-61`, opening the Quick Start section — confirm by eye that it is prominent, because a reader who misses the second `mv` silently loses their entire task history. Then ledger Task 4 complete.

**Steps 1-7 — Tasks 5 through 11**, in order, one subagent per task with a task review after each:

| Task | Deliverable |
|---|---|
| 5 | `forge_host` defaults to `127.0.0.1`; `assert_safe_binding` refuses a non-loopback bind with no token |
| 6 | `TokenAuthMiddleware` gates every route when a token is set, including both WebSockets; webhooks exempt |
| 7 | Agent skills move to `issueforge/agents/skills/`; resolution order fixed in `agy_runner.py:230-234` |
| 8 | `package-data` so the wheel carries templates, static and skills; pytest moved to a `dev` extra |
| 9 | `issueforge init` writes a minimal `.env` |
| 10 | MIT `LICENSE`; README rewritten for a first-time reader |
| 11 | GitHub Actions running the suite on 3.10 and 3.12 |

**Fold into the tasks they touch**, rather than as separate work:

- In Task 5: `forge_secret_key` (`config.py:24`) ships a hardcoded default and appears unused. Grep it; if nothing reads it, delete it in the same commit rather than leaving two credential-shaped settings in the file. If something does read it, leave it alone and say so.
- In Task 8: this `.venv` is uv-managed and has **no pip**. Use `uv pip install ... --python .venv/bin/python` locally. The GitHub Actions runner in Task 11 has ordinary pip and is unaffected.
- In Task 10: untrack `.codegraphx/` (844K, two files, tracked, still full of old `agit/` paths, nothing reads it) and add it to `.gitignore`. It should not ship in a published repository.
- In Task 10: the two cosmetic blank-line findings deferred from Task 1 (`knowledge_vault.py`, `terminal_manager.py`).

**Stop after Task 11.** Task 12 publishes to GitHub and PyPI; a PyPI version number can never be reused, so it stays manual. Hand over the commands, the pre-flight secret sweep included, and let the author run them.

## Verification

Every task ends with `.venv/bin/python -m pytest -q` green, and the count only ever goes up: 199 now → 203 after Task 5 → 209 after Task 6 → 211 after Task 7 → 213 after Task 9. No test may be deleted or skipped to reach a number.

Two checks the suite cannot make, both required before the plan is done:

1. **The wheel must actually contain the application.** Build it, then confirm the templates, `dashboard.js` and the skills are inside the archive, install it into a throwaway virtualenv, start it, and fetch `/` expecting `200`. A wheel that installs cleanly and then 500s on first request is the failure this catches.
2. **The token gate must hold on the WebSocket shell.** `BaseHTTPMiddleware` never sees WebSocket connections, so the guard is hand-written in both handlers. With a token configured, a socket opened without one must be refused.

One known flake: `tests/test_steering_channel.py::test_orchestrator_interpret_steering_intent`. Re-run it in isolation before treating a failure as new breakage.

## Out of scope

- **Frontend redesign** — confirmed to come after the release, as its own plan, following a `frontend-design` pass. The current dashboard works; it just looks generic.
- **Publishing** — Task 12, manual, author-run.
- **Collapsing the agy-then-LiteLLM scaffolding** (~120 lines across seven call sites) — a behaviour-preserving refactor of the most exercised path in the system, deferred to its own `safe-refactor` pass.
- **Sandbox hardening with bubblewrap** — real value, changes how every command executes, not a release blocker.
