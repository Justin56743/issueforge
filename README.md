# Issueforge

**Issueforge** is a human-in-the-loop Git engineering orchestrator. It monitors GitHub/GitLab
repositories for issues, PRs and work items, notifies you via Telegram and a web dashboard, then
runs a multi-LLM agent pipeline (Planner → Coder → Tester → Reviewer) in an isolated native
workspace to implement the change — pausing to ask for approval, a target branch, and a final
push confirmation along the way.

---

## Install

```bash
pip install issueforge
```

Requires Python 3.10+, Linux (no Docker — sandboxes are plain OS directories and process groups).
`git` must be on `PATH`: every task clones, branches and pushes with it.

`agy` is an external coding-agent CLI. When it is installed (`~/.local/bin`, `/usr/local/bin`,
`/usr/bin` or anywhere on `PATH`), Issueforge runs each agent through it as a subprocess.
Issueforge does not install it, and it is optional: without it, the Planner, Coder, Tester and
Reviewer call models through LiteLLM with the API keys in your `.env` (`GEMINI_API_KEY`, plus any
of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`).

> **Upgrading from Agit?** The project (and its default state directory, database filename, and
> environment variable prefix) were renamed. Run this **before the first `issueforge start`**:
> ```bash
> mv ~/.agit ~/.issueforge
> mv ~/.issueforge/agit.db ~/.issueforge/issueforge.db
> ```
> The order matters: `issueforge start` creates `~/.issueforge`, and once it exists
> `mv ~/.agit ~/.issueforge` nests the old directory inside it as `~/.issueforge/.agit`. If you
> already started it, move the new directory aside first (`mv ~/.issueforge ~/.issueforge.new`),
> then run the two lines above.
>
> The second line matters on its own: the default database filename also changed, from `agit.db`
> to `issueforge.db`, so moving only the directory leaves the application looking for a database
> that isn't there — it will silently create a fresh empty one and your task history, events and
> learnings will appear to have vanished. Also update any existing `.env`: environment variables
> changed prefix from `AGIT_` to `FORGE_` (e.g. `AGIT_VAULT_ROOT` → `FORGE_VAULT_ROOT`) — old
> `AGIT_*` variables are no longer recognized and will be silently ignored, not an error. Rename
> each one, e.g. `AGIT_HOST` → `FORGE_HOST`, `AGIT_DB_PATH` → `FORGE_DB_PATH`,
> `AGIT_WORKSPACE_ROOT` → `FORGE_WORKSPACE_ROOT`. `issueforge start` warns when it still sees
> `~/.agit` or `AGIT_*` keys in `./.env`.
>
> Two more changes to carry over:
> - An installed `agit` systemd unit must be replaced: it points at the old package and paths.
>   Start from [`systemd/issueforge.service`](https://github.com/Justin56743/issueforge/blob/main/systemd/issueforge.service) and fill in its placeholders.
> - The dashboard now binds `127.0.0.1` by default. To keep serving it on your LAN, pass
>   `--host 0.0.0.0` **and** set `FORGE_AUTH_TOKEN`; without the token it refuses to start.

### Development

From a checkout of this repository:

```bash
git clone https://github.com/Justin56743/issueforge.git && cd issueforge
pip install -e ".[dev]"
pytest -q
```

`pytest` is only in the `dev` extra, not the base install.

---

## Quick start

```bash
mkdir myproject && cd myproject
issueforge init      # writes ./.env, interactively, mode 0600 — never overwrites an existing one
issueforge start     # binds 127.0.0.1:8000 by default and prints a login URL
```

`issueforge init` prompts for the settings most setups need (secrets are not echoed):

- `GEMINI_API_KEY`
- `GITHUB_TOKEN`
- `GITLAB_TOKEN`
- `TELEGRAM_BOT_TOKEN`, and then your Telegram user ID for `TELEGRAM_ALLOWED_USER_IDS`. The bot
  ignores everyone until that allowlist names someone.

`pydantic-settings` reads `.env` from the current working directory, so `issueforge start` must be
run from the same directory `init` wrote it into.

`issueforge start` prints `http://127.0.0.1:8000/?token=<token>`. Open that link once; it sets a
cookie, so afterwards `http://localhost:8000` (dashboard) and `http://localhost:8000/folders`
(task dossiers) work directly.

Other commands: `issueforge status`, `issueforge trigger`, `issueforge migrate-vault`,
`issueforge sync-knowledge`. Run `issueforge --help` for the full list.

---

## Safety

- **The dashboard executes shell commands.** It drives git and the `agy` CLI inside task
  sandboxes, so it is protected in two layers:
  - **A token, always.** `issueforge start` uses `FORGE_AUTH_TOKEN` when it is set. Otherwise it
    generates one on first start, stores it in `~/.issueforge/auth_token` (mode 0600, under
    `FORGE_VAULT_ROOT`), and reuses it on every later start. Binding loopback alone is not enough:
    any web page open in your browser can reach `127.0.0.1`, so without the token it could drive
    the dashboard. Delete the file to rotate the token.
  - **Loopback by default.** `issueforge start` binds `127.0.0.1`. A non-loopback `--host` refuses
    to start unless `FORGE_AUTH_TOKEN` is set explicitly, so exposing the dashboard is always a
    deliberate choice.
- **Agents run `agy --dangerously-skip-permissions`** inside each task's sandbox directory, so a
  running agent can modify anything the user running Issueforge can modify. Only point it at
  repositories you're comfortable handing an agent that kind of access to.
- Every HTTP route requires the token, supplied as an `Authorization: Bearer <token>` header, a
  `?token=<token>` query parameter, or a `forge_token` cookie. Visiting any page once with
  `?token=` sets that cookie for later requests. The two WebSockets (the terminal and the
  interactive sandbox shell) accept only the `?token=` parameter or the cookie, not the header. They
  also refuse any browser connection whose `Origin` does not match the host it connected to.
  - `/api/webhooks/github` and `/api/webhooks/gitlab` skip the token only when their platform
    secret (`GITHUB_WEBHOOK_SECRET` / `GITLAB_WEBHOOK_SECRET`) is configured. Without a secret set,
    the webhook URL itself must carry `?token=`.
  - A `?token=` URL ends up in server access logs and browser history — use it once to set the
    cookie, then drop it from bookmarks, scripts and webhook configs.

---

## Architecture & Engineering Documentation

The [`docs/`](https://github.com/Justin56743/issueforge/tree/main/docs) directory holds the original design documents. They are **design history,
not a description of the running system**: they plan JWT/RBAC, Redis, `/api/v1` and container
sandboxes, none of which was built. The security and isolation model is the one in
[Safety](#safety) above.

| Document | Focus & Description |
| :--- | :--- |
| **[High-Level Design (HLD)](https://github.com/Justin56743/issueforge/blob/main/docs/HLD.md)** | System topology, domain boundaries, component interactions, tech stack, data persistence, and security/isolation models. |
| **[Low-Level Design (LLD)](https://github.com/Justin56743/issueforge/blob/main/docs/LLD.md)** | Class blueprints, database entity schemas, Data Transfer Objects (DTOs), REST/WebSocket API endpoints, and error handling policies. |
| **[System Workflows (FLOW)](https://github.com/Justin56743/issueforge/blob/main/docs/FLOW.md)** | End-to-end task execution lifecycles, state machine transitions, and decision-tree logic. |
| **[Sequence Diagrams](https://github.com/Justin56743/issueforge/blob/main/docs/SEQUENCE_DIAGRAM.md)** | Step-by-step Mermaid sequence diagrams for authentication, execution, real-time log streaming, data sync, and retries. |

### Multi-LLM collaborative roles

1. **Supervisory Orchestrator** (`ORCHESTRATOR`): Tech Lead managing goal decomposition, conversational Telegram take-over, operator triage Q&A, and branch targeting.
2. **Planner Agent** (`PLANNER_MODEL`): Scans repository tree and configurations, drafting a step-by-step implementation plan.
3. **Coder Agent** (`CODER_MODEL`): Generates file modifications and writes them into the sandbox.
4. **Tester Agent** (`TESTER_MODEL`): Discovers and executes project test suites (`pytest`, `npm test`, `cargo test`, etc.), analyzes failures, and guides the Coder iteratively until tests pass.
5. **Reviewer Agent** (`REVIEWER_MODEL`): Computes unified git diffs, performs risk assessment, and drafts a PR description.
6. **Merge Conflict Resolver** (`MERGE_RESOLVER`): Performs automated 3-way reconciliation when the target branch has advanced, verifying syntax and tests before committing the merge.

### Issueforge Agent Studio

- **Live file tree explorer**: repository files with git status indicators (`+new`, `●mod`).
- **In-browser web editor**: inspect and edit code directly in the sandbox, with path-traversal guards keeping edits inside the workspace.
- **Live `agy` session terminal**: real-time stream of the Planner/Coder/Tester/Reviewer session in an xterm.js terminal feed.
- **Tool-call normalizer & diagnostics**: corrects malformed tool parameters and bounds file paths inside the sandbox before they reach the agent.
- **Mid-run steering**: queue an operator directive from the Studio (`POST /api/tasks/{task_id}/steer`), the dashboard, or Telegram (`/steer` or conversational text) — applied at the agent's next turn (see [Canvas steering](#canvas-steering-queued-not-live) below).
- **Plan caching**: reuses a verified `PLAN.md` across retry attempts when the failure happened after planning, and applies focused differential repair.

---

## Key capabilities

- **Conversational Telegram take-over**: send a free-form instruction and the Supervisory Orchestrator classifies intent, auto-approves, and runs the pipeline end-to-end.
- **Permanent task dossiers** (`.issueforge/tasks/{task_id}/`): `README.md`, `task_summary.json`, run metadata, and every historical sandbox (`sandboxes/run-X`), browsable via the `/folders` explorer.
- **Live xterm.js PTY terminal**: window resize (`TIOCSWINSZ`), per-run tabs, and one-click retry.
- **HITL target branch gate**: after discovering remote branches the pipeline stops and asks which branch to target, on Telegram and the dashboard, before a line is written. A branch defaulted at creation does not count as your choice — the run only proceeds once you confirm one.
- **Cross-task knowledge vault**: lessons the Reviewer distills from completed work become Obsidian notes in `knowledge_vault/`, linked to the repository and issue that produced them, and are fed back to the Planner on future tasks.
- **Plan reuse across retries**: when a retry follows a failure that happened after planning, the verified `PLAN.md` is carried into the fresh sandbox instead of being regenerated.
- **Obsidian vault state layer** (`~/.issueforge`): the entire state directory is a valid Obsidian vault. Task dossiers carry YAML frontmatter and `[[wikilinks]]`, so opening the vault shows a graph linking tasks to their repositories, issues, branches and people.
- **Live execution DAG in the dashboard**: every task gets a `task_dag.canvas` whose seven stage nodes recolour in real time (yellow running, green passed, red failed, cyan awaiting operator) — rendered natively in the browser, no Obsidian install needed.
- **AST blast-radius analysis**: parses the workspace into a NetworkX import graph and reports which downstream modules and tests a change touches, with a LOW/MEDIUM/HIGH risk score fed to the Reviewer. Advisory only — the full test suite still runs regardless.
- **Automated 3-way merge guard**: before pushing, dry-run merges the target branch; if it has advanced and conflicts, an LLM reconciles the conflict markers, the result is syntax-verified, and the merge is committed. Any failure aborts cleanly and hands the conflict back to you.
- **Native process isolation, no Docker**: each sandbox is a plain directory and process group on the host filesystem. The primary target deployment is the NVIDIA Jetson Orin, but nothing about the isolation model is Jetson-specific.
- **Multi-LLM pipeline with fallback**: Planner, Coder, Tester and Reviewer calls go through LiteLLM with sequential model fallback routing.

---

## The Telegram bot

- **New task alert**: an issue/PR card with inline buttons — `Approve & Start`, `Pick Branch`, `Custom Instructions`, `Reject`.
- **Confirmation card**: the final diff, test status, target branch and risk level, with `Confirm & Push PR`, `Change Branch`, `Request Revision`, `Discard Changes`.
- **Commands**: `/status` (pending/running/completed counts), `/tasks` (recent tasks), `/sync` (trigger an immediate PAT sync), `/steer` (queue a directive), `/help`.

---

## The Obsidian vault

Set `FORGE_VAULT_ROOT` (default `~/.issueforge`) and open that directory as a vault in Obsidian:

```text
~/.issueforge/
├── .obsidian/          # Editor + graph colour-group configuration (written once, never clobbered)
├── issueforge.db       # SQLite database
├── tasks/<task_id>/
│   ├── README.md       # YAML frontmatter + [[wikilinks]] — the task's graph node
│   ├── PLAN.md         # Living implementation plan
│   ├── task_dag.canvas # Live execution DAG
│   ├── metadata/run-N/ # sandbox.log, execution_history.json, terminal.raw
│   └── sandboxes/run-N/# Isolated git workspace per attempt
├── knowledge_vault/    # Lesson notes distilled from completed tasks
└── codebase_graph/     # Import graph JSON written by the blast-radius analysis
```

`FORGE_TASKS_ROOT`, `FORGE_WORKSPACE_ROOT` and `FORGE_DB_PATH` all derive from the vault root
unless set explicitly, so setting only `FORGE_VAULT_ROOT` keeps everything in one tree.

### Viewing the execution DAG

The task detail page renders `task_dag.canvas` natively — Obsidian is not required. The card shows
the seven pipeline stages with live colour transitions, zoom controls, and a box to queue a
steering directive.

Obsidian remains useful for the *graph* view across tasks (`[[wikilinks]]` between repos, issues,
branches and people) and for editing dossiers by hand. The setup below is optional.

### Opening the vault in Obsidian

1. Install Obsidian on the machine you browse the dashboard from.
2. **Open folder as vault** → select the vault root. `~/.issueforge` is a dotfolder and most file
   pickers hide those, so either reveal hidden folders, or point Obsidian at a visible symlink:
   ```bash
   ln -s ~/.issueforge ~/issueforge-vault
   ```
3. The task detail page's **Canvas in Obsidian** button then opens that task's `task_dag.canvas`
   directly.

The button emits `obsidian://open?path=<absolute path>`, so it only works when Obsidian is
installed **on the machine viewing the page** and the vault has been added there — usually not the
machine serving the dashboard. Use **Copy Canvas Path**, or **Canvas JSON**
(`/api/tasks/{task_id}/canvas`), to read the DAG without Obsidian.

### Migrating dossiers from an older layout

Earlier versions wrote dossiers to a CWD-relative `./.issueforge/tasks`. To relocate them into the vault:

```bash
issueforge migrate-vault            # dry run: prints a plan
issueforge migrate-vault --apply    # perform the move
```

Nothing is deleted — migrated sources are renamed to `<name>.migrated`, so re-running is a no-op.
`test-*` fixture leftovers are skipped unless you pass `--include-orphans`.

### Merge guard behaviour

The guard runs inside `push_and_create_pr`, after the commit and before the push — that is the
only point in the pipeline where the working tree is clean enough for git to merge. Consequences
worth knowing:

- Conflicts surface at **push time**, not on the confirmation card.
- If reconciliation fails for any reason, the merge is aborted (branch and `HEAD` untouched) and
  the task fails with the conflicting filename so you can resolve it by hand.
- The resolved merge is syntax-checked before it is committed; a resolution that breaks the build
  is rejected rather than pushed.

### Canvas steering: queued, not live

Operator notes on a canvas (a card containing `STEER:` or an **Operator Steering Directive**
heading) are **queued onto the task**, not injected into a running agent. `agy` is launched as a
one-shot subprocess with its whole prompt already in argv and no stdin, so nothing can reach it
mid-flight. A directive therefore takes effect at the **next** Planner/Coder/Tester invocation —
the next test-repair iteration, or the next run. The card turns green once queued to confirm
receipt.

---

## License

MIT — see [`LICENSE`](https://github.com/Justin56743/issueforge/blob/main/LICENSE).
