# Issueforge 3.0 Architecture Implementation Plan

> **Design history, not current behaviour.** This plan predates the move away from Google ADK: no ADK supervisor and no Google GenAI SDK were built. LLM calls go through the `agy` CLI first, with LiteLLM as the fallback. The implemented system is described in [README.md](README.md); there is no container sandbox — sandboxes are plain directories and agents run with the user's full permissions.

## Complete Multi-Agent Supervisory Orchestration & Obsidian Graph Systems

**Target Platform:** NVIDIA Jetson Orin (Native Linux, Zero-Docker Architecture)  
**Specification Reference:** none current (the original spec was never published)  
**Status:** Milestones 1 and 2 delivered; Milestone 3 delivered scoped down (see §7)  
**Author:** Pair Programming Session (Antigravity & Lead Engineer)

---

## 1. Executive Summary & Design Governance

This document establishes the end-to-end technical implementation plan for upgrading Issueforge from a linear multi-agent pipeline into an adaptive, graph-structured engineering organism powered by **Google ADK** and an **Obsidian Graph & Canvas Topology Engine**.

### 1.1 Architectural Consensus (Resolved via `/grill-me`)

| Decision Domain | Selected Approach | Operational Rationale |
|---|---|---|
| **Delivery Strategy** | **Phased Milestones** | De-risks execution: Milestone 1 (Vault & Canvas) ➔ Milestone 2 (AST & Merge) ➔ Milestone 3 (ADK Supervisor). |
| **Integration Pattern** | **Hybrid Backwards-Compatible** | Keep public APIs of `engine.py` and `orchestrator.py` intact; existing 44 test suites and Telegram conversational takeover remain 100% green. |
| **Vault Location** | **Configurable (`~/.issueforge` default)** | Uses `FORGE_VAULT_ROOT` environment setting, defaulting to `~/.issueforge` on Jetson, overrideable by temp test fixtures. |
| **LLM Client Strategy** | **Native Google GenAI SDK** | `google.genai.Client(api_key=settings.gemini_api_key)` for ADK supervisor, Knowledge Vault, and Merge Resolver, with fallback to LiteLLM for multi-provider failover. |
| **Canvas Watcher** | **Per-Task Background Loop** | Spawned as an `asyncio.Task` during run execution; watches `task_dag.canvas` for operator directives and cancels cleanly upon run termination. |
| **AST Scope** | **Python stdlib + NetworkX** | Deep Python AST first via stdlib `ast` and NetworkX (0 external C/node dependencies on Jetson), with extensible plugin hooks for JS/Go. |
| **HITL Pause Gates** | **Dual Verification Gates** | Stage 2 (Branch Selection Gate) + Stage 6 (Pre-Push Confirmation Gate) preserving full human safety. |
| **Merge Conflict Gate** | **Test Verification Gate** | After Gemini reconciles conflict markers and stages files, execute syntax checks and blast-radius unit tests before committing. |
| **Knowledge Vault Search** | **Fast Lexical & Tag Scoring** | 0ms latency, zero GPU/memory overhead on Jetson Orin, zero vector database dependencies. |

---

## 2. Directory Layout & File Topology

```text
~/.issueforge/                                    # Root Obsidian Vault & State Directory
├── .obsidian/                              # Native Obsidian workspace & graph settings
│   ├── app.json                            # Editor and file association configs
│   ├── graph.json                          # Colored visual graph clustering rules & physics
│   └── types.json                          # Canvas node type registrations
├── tasks/                                  # Task Dossiers (One folder per engineering task)
│   └── task-XXXX/
│       ├── README.md                       # Task overview with [[wikilinks]] & YAML metadata
│       ├── PLAN.md                         # Living architectural specification
│       ├── task_dag.canvas                 # Interactive real-time visual DAG
│       ├── task_summary.json               # Fast machine-readable metadata cache
│       ├── metadata/run-X/                 # Segregated execution logs (sandbox.log, terminal.raw)
│       └── sandboxes/run-X/                # Hermetically isolated Git workspaces
├── knowledge_vault/                        # Persistent Cross-Task Architectural Memory
│   ├── Gotcha-Remote-Tracking-Branch-Clone.md
│   └── Pattern-FastAPI-SSE-Heartbeat.md
└── codebase_graph/                         # AST Dependency Graph Artifacts
    └── <repo>-ast.json

/home/justin/Projects/issueforge/                 # Python Application Codebase Root
├── issueforge/
│   ├── config.py                           # Settings singleton + FORGE_VAULT_ROOT
│   ├── main.py                             # CLI startup & vault initialization hook
│   ├── agents/
│   │   ├── adk/                            # [MILESTONE 3] Google ADK Workflow Subsystem
│   │   │   ├── __init__.py
│   │   │   ├── supervisor.py               # IssueforgeSupervisoryOrchestrator state machine
│   │   │   └── hitl_gate.py                # Asynchronous pause/resume gates
│   │   ├── merge_resolver.py               # [MILESTONE 2] 3-Way AST Conflict Resolver
│   │   ├── orchestrator.py                 # Existing Supervisory Orchestrator (preserved)
│   │   ├── engine.py                       # High-level engine delegating to ADK
│   │   └── llm.py                          # Unified LiteLLM + Google GenAI fallback router
│   ├── vault/                              # [MILESTONE 1] Obsidian Vault Subsystem
│   │   ├── __init__.py
│   │   ├── manager.py                      # Vault bootstrap & .obsidian configuration
│   │   ├── linker.py                       # [[wikilink]] extraction & SQLite edge indexing
│   │   ├── canvas_builder.py               # Real-time .canvas JSON generation engine
│   │   ├── canvas_watcher.py               # File watcher for reverse steering via Canvas
│   │   └── knowledge_vault.py              # [MILESTONE 3] Cross-task gotcha extraction & RAG
│   ├── graph/                              # [MILESTONE 2] Codebase AST Subsystem
│   │   ├── __init__.py
│   │   ├── ast_parser.py                   # Python AST dependency parser using NetworkX
│   │   └── blast_radius.py                 # Downstream dependency depth & test selector
│   └── core/
│       ├── database.py                     # SQLite connection & schema migrations
│       ├── task_dossier.py                 # Task dossier generator wired to canvas
│       └── caching.py                      # [MILESTONE 3] Multi-run plan artifact caching
└── tests/
    ├── test_obsidian_vault.py              # [MILESTONE 1] Vault configs & wikilink sync
    ├── test_canvas_builder.py              # [MILESTONE 1] Canvas mutations & reverse steering
    ├── test_ast_blast_radius.py            # [MILESTONE 2] AST parser & blast radius
    ├── test_merge_resolver.py              # [MILESTONE 2] Dry-run merge & conflict resolver
    └── test_adk_supervisor.py              # [MILESTONE 3] ADK state machine & pause gates
```

---

## 3. Milestone 1: Obsidian Vault & Dynamic Canvas Engine

### 3.1 Objectives
Transform the `.issueforge/` filesystem into a fully compliant Obsidian Vault, enable real-time 2D visual DAG tracking via `.canvas` files, support human operator steering by writing notes directly onto the canvas, and sync `[[wikilinks]]` into SQLite.

### 3.2 Concrete Code Implementation

#### 1. Dependencies & Configuration
- **Files:** `pyproject.toml`, `issueforge/config.py`
- Add `networkx>=3.0`, `google-genai>=2.0.0`, `google-adk>=2.9.0` to `pyproject.toml`.
- Add `vault_root: Path = Field(default=Path.home() / ".issueforge", validation_alias="FORGE_VAULT_ROOT")` in `issueforge/config.py`.
- Add helper properties:
  - `obsidian_dir -> vault_root / ".obsidian"`
  - `tasks_dir -> vault_root / "tasks"`
  - `knowledge_dir -> vault_root / "knowledge_vault"`
  - `graph_dir -> vault_root / "codebase_graph"`

#### 2. SQLite Schema Migrations
- **File:** `issueforge/core/database.py`
- In `init_db()`, execute migration DDL:
  ```sql
  -- Dynamic branch & active run tracking
  ALTER TABLE tasks ADD COLUMN target_branch_candidates TEXT DEFAULT '[]';
  ALTER TABLE tasks ADD COLUMN selected_target_branch TEXT DEFAULT NULL;
  ALTER TABLE tasks ADD COLUMN active_run_id TEXT DEFAULT 'run-1';

  -- Obsidian Graph Inter-Node Topology Table
  CREATE TABLE IF NOT EXISTS task_graph_edges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_task_id TEXT NOT NULL,
      target_node TEXT NOT NULL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(source_task_id) REFERENCES tasks(id) ON DELETE CASCADE
  );
  CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON task_graph_edges(source_task_id);
  CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON task_graph_edges(target_node);

  -- Multi-Run Segregated Execution History Table
  CREATE TABLE IF NOT EXISTS task_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      status TEXT NOT NULL,
      failure_stage TEXT,
      plan_reused BOOLEAN DEFAULT 0,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
  );
  CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_unique ON task_runs(task_id, run_id);
  ```

#### 3. Vault Bootstrap (`issueforge/vault/manager.py`)
- Implements `VaultManager`:
  - `initialize_vault()`: ensures directories `.obsidian`, `tasks`, `knowledge_vault`, and `codebase_graph` exist.
  - Generates `.obsidian/app.json` with editor configurations (`legacyEditor: false`, `livePreview: true`, `showLineNumber: true`).
  - Generates `.obsidian/graph.json` with color-coded topology groups:
    - `tag:#issueforge/task`: Emerald Green (`4317676`)
    - `tag:#issueforge/knowledge`: Warm Orange (`16744448`)
    - `tag:#issueforge/ast`: Cyan/Blue (`2201331`)
    - `tag:#priority/critical`: Bright Red (`16711680`)
    - Force physics tuned for edge hardware.

#### 4. Wikilink Indexing & Edge Sync (`issueforge/vault/linker.py`)
- Implements `VaultLinkIndexer`:
  - `extract_links(file_path)`: Regex extraction supporting standard `[[Node]]` and aliased `[[Node|Title]]`.
  - `sync_task_edges(task_id, readme_path)`: Atomic sync of directional edges into SQLite `task_graph_edges`.

#### 5. Obsidian Canvas DAG Builder (`issueforge/vault/canvas_builder.py`)
- Implements `CanvasDAGBuilder`:
  - `initialize_task_dag(task_id, issue_title)`: Constructs horizontal DAG with 7 stages:
    1. Ingested Task (Purple "6")
    2. Planner Agent (Yellow "3" or Green "4")
    3. Branch Gate (Cyan "5")
    4. Coder Agent (`agy`) (Yellow "3" or Green "4")
    5. Tester Agent (Yellow "3", Green "4", or Red "1")
    6. AST Merge Guard (Yellow "3" or Green "4")
    7. Reviewer Agent (Purple "6" or Green "4")
  - `update_stage_node(stage_index, markdown_text, color_code)`: Updates telemetry and color in-flight.
  - `inject_steering_directive(directive_text, target_stage_index)`: Attaches an orange human directive card ("2") above the target agent node.
  - `save()`: Writes valid JSON matching Obsidian Canvas specifications.

#### 6. Real-Time Reverse Steering Watcher (`issueforge/vault/canvas_watcher.py`)
- Implements `CanvasSteeringWatcher`:
  - Uses `watchfiles.awatch` to monitor `task_dag.canvas`.
  - Detects newly dropped nodes containing `"### 🧑‍💻 Operator Steering Directive"` or `"STEER:"`.
  - Triggers the registered async steering callback.

#### 7. Integration Hooks
- Update `issueforge/core/task_dossier.py` to invoke `CanvasDAGBuilder.initialize_task_dag()` and `VaultLinkIndexer.sync_task_edges()`.
- Update `issueforge/main.py` startup lifespan to call `VaultManager(settings.vault_root).initialize_vault()`.

### 3.3 Milestone 1 Verification
- Create `tests/test_obsidian_vault.py`:
  - Verify directory creation, `.obsidian/app.json`, `.obsidian/graph.json` content and color schemas.
  - Verify wikilink extraction (raw and aliased) and SQLite edge synchronization.
- Create `tests/test_canvas_builder.py`:
  - Verify 7 nodes and 6 edges generated in DAG layout.
  - Verify node updates through color codes "1" -> "5" -> "4".
  - Verify steering directive injection.
  - Verify `CanvasSteeringWatcher` triggers callback on file modification.
- Execute full test suite:
  ```bash
  .venv/bin/pytest tests/test_obsidian_vault.py tests/test_canvas_builder.py -v
  ```

---

## 4. Milestone 2: AST Codebase Graph & 3-Way AST Conflict Resolver

### 4.1 Objectives
Eliminate slow full-suite test runs and broken Git PR merges on the Jetson Orin by constructing a native Python AST dependency graph using NetworkX and building an automated 3-way AST merge conflict resolver powered by Gemini 3.7 Flash.

### 4.2 Concrete Code Implementation

#### 1. Codebase AST Graph Engine (`issueforge/graph/ast_parser.py`)
- Implements `CodebaseASTGraph`:
  - Traverses workspace `*.py` files (excluding `.venv`, `.git`, `.issueforge`).
  - Uses `ast.parse()` to extract `Import` and `ImportFrom` nodes.
  - Resolves module names to relative file paths.
  - Constructs `nx.DiGraph` representing workspace import relationships.
  - `calculate_blast_radius(modified_files, max_depth=2)`:
    - Reverses graph to follow downstream dependants (`reverse_graph = self.graph.reverse()`).
    - Uses `nx.bfs_tree` to traverse downstream impact.
    - Filters impacted files to find associated test files (`test_*.py` or `*_test.py`).
    - Computes risk assessment: `LOW` (<=3 files), `MEDIUM` (4-8 files), `HIGH` (>8 files or >4 tests).

#### 2. Automated 3-Way AST Merge Conflict Resolver (`issueforge/agents/merge_resolver.py`)
- Implements `MergeConflictResolver`:
  - `probe_for_conflicts(target_branch)`:
    - Fetches remote updates: `git fetch origin {target_branch}`.
    - Runs pre-flight dry-run merge: `git merge --no-commit --no-ff origin/{target_branch}`.
    - Returns `True` if `CONFLICT` detected; otherwise aborts cleanly.
  - `list_conflicted_files()`:
    - Queries unmerged files: `git diff --name-only --diff-filter=U`.
  - `resolve_single_file(file_path)`:
    - Detects conflict blocks (`<<<<<<< HEAD`, `=======`, `>>>>>>>`).
    - Prompts Gemini 3.7 Flash with the 3-way AST reconciliation instructions.
    - Strips markdown formatting, writes clean reconciled code, and stages via `git add {rel_path}`.
  - `verify_resolution_integrity(sandbox_path)`:
    - **Syntax Verification**: `python3 -m compileall .`.
    - **Blast Radius Unit Tests**: Runs tests identified by `CodebaseASTGraph` on the merged workspace.
  - `execute_resolution_pipeline(target_branch)`:
    - Full orchestration: probe -> extract -> resolve -> verify syntax & tests -> finalize `git commit -m 'chore(merge): resolve conflicts with {target_branch} via Issueforge AST Resolver'`.

### 4.3 Milestone 2 Verification
- Create `tests/test_ast_blast_radius.py`:
  - Test building AST graph on mock multi-file Python repository with imports.
  - Test calculating blast radius and test discovery.
  - Test risk level calculation (`LOW`, `MEDIUM`, `HIGH`).
- Create `tests/test_merge_resolver.py`:
  - Test dry-run conflict detection on a Git sandbox fixture with conflicting branches.
  - Test `resolve_single_file` removes `<<<<<<<`, `=======`, `>>>>>>>` markers.
  - Test syntax verification gate and automated merge commit.
- Execute test suite:
  ```bash
  .venv/bin/pytest tests/test_ast_blast_radius.py tests/test_merge_resolver.py -v
  ```

---

## 5. Milestone 3: Google ADK Supervisory Orchestrator & Knowledge Vault Integration

### 5.1 Objectives
Upgrade the central orchestration engine to Google ADK (`google-adk`), bind the multi-agent pipeline into a robust state machine with dual HITL pause gates, integrate atomic cross-task gotcha extraction in the Knowledge Vault, and enable differential artifact caching.

### 5.2 Concrete Code Implementation

#### 1. Cross-Task Knowledge Vault (`issueforge/vault/knowledge_vault.py`)
- Implements `KnowledgeVaultManager`:
  - Operating under `.issueforge/knowledge_vault/`.
  - `record_successful_run_lesson(task_id, repo_name, diff_text, failure_log)`:
    - Invoked when complex tasks succeed or recover from prior failures.
    - Uses Gemini 3.6 Flash to extract atomic lessons (Gotchas, Patterns, Conventions).
    - Formats as Markdown files with YAML frontmatter (`title`, `type`, `repo`, `tags`, `source_task`).
  - `retrieve_context_for_task(issue_description, repo_name)`:
    - High-speed lexical keyword and tag scoring (0ms latency, zero GPU/memory overhead on Jetson).
    - Returns top 3 matching precedents formatted for the Planner agent prompt.

#### 2. Incremental Artifact Caching (`issueforge/core/caching.py`)
- Implements `ArtifactCacheManager`:
  - `apply_plan_cache(task_dir, from_run_id, to_run_id, failure_stage)`:
    - If prior run failed in `TESTER`, `MERGE_CONFLICT`, or `REVIEWER` (meaning `PLAN.md` was already valid), automatically copies `PLAN.md` into the new sandbox.
    - Saves LLM token costs and guarantees architectural consistency.

#### 3. Google ADK Supervisory Orchestrator (`issueforge/agents/adk/supervisor.py`)
- Implements `IssueforgeSupervisoryOrchestrator`:
  - Built on Google ADK state machine architecture.
  - Encapsulates state in `TaskExecutionState` (Pydantic model):
    - `task_id`, `repo_url`, `issue_description`, `sandbox_dir`, `canvas_path`, `active_run_id`, `selected_target_branch`, `target_branch_candidates`, `failure_stage`.
  - Coordinates all stages with live Canvas telemetry and dual HITL gates:
    - **Stage 1 (Planner & Knowledge)**: Ingests `knowledge_vault` context, generates `PLAN.md`, updates Canvas to Green "4".
    - **Stage 2 (HITL Branch Gate)**: Discovers remote branches (`--no-single-branch`), sets Canvas to Cyan "5", enters asynchronous pause until branch is selected via Telegram, Web UI, or Canvas steering.
    - **Stage 3 (Coder `agy`)**: Dispatches `agy` CLI in sandbox with `--model gemini-3.6-flash-high`.
    - **Stage 4 (Blast Radius & Tester)**: Computes AST blast radius, runs syntax check (`python3 -m compileall .`), runs targeted tests with iterative repair loop.
    - **Stage 5 (AST Merge Guard)**: Invokes `MergeConflictResolver` against `selected_target_branch`.
    - **Stage 6 (Reviewer & Pre-Push HITL Gate)**: Extracts lessons into Knowledge Vault, posts confirmation card, awaits operator push approval, and finalizes push.

#### 4. Engine & Queue Hybrid Delegation (`issueforge/agents/engine.py`)
- Refactor `ExecutionEngine.execute_task()` to delegate execution to `IssueforgeSupervisoryOrchestrator`, while maintaining existing event bus publishing, SQLite task status transitions, and Telegram notifications.

### 5.3 Milestone 3 Verification
- Create `tests/test_adk_supervisor.py`:
  - Test state machine initialization and stage transitions.
  - Test HITL pause gate blocks execution and unblocks on async callback.
  - Test canvas node updates throughout lifecycle.
  - Test Knowledge Vault extraction and lexical retrieval.
  - Test artifact plan caching across run attempts.
- Execute full regression test suite:
  ```bash
  .venv/bin/pytest tests/ -v
  ```
  Ensure all 44 baseline tests + all new Milestone 1, 2, and 3 tests pass cleanly (estimated ~60+ total tests).

---

## 6. Complete Verification Suite Matrix

| Milestone | Test File | Key Test Cases | Expected Pass Criteria |
|---|---|---|---|
| **M1** | `tests/test_obsidian_vault.py` | `test_vault_initialization`, `test_wikilink_extraction`, `test_task_graph_edge_sync` | `.obsidian` configs created, wikilinks indexed into SQLite. |
| **M1** | `tests/test_canvas_builder.py` | `test_dag_initialization`, `test_stage_color_mutation`, `test_steering_directive_injection`, `test_canvas_watcher_directive` | `.canvas` JSON compliant, watcher detects steering annotations within 1s. |
| **M2** | `tests/test_ast_blast_radius.py` | `test_ast_graph_construction`, `test_blast_radius_calculation`, `test_selective_test_discovery` | Dependent modules and test files identified correctly. |
| **M2** | `tests/test_merge_resolver.py` | `test_dry_run_conflict_probe`, `test_conflict_marker_resolution`, `test_syntax_and_test_verification_gate` | Conflict markers eliminated, syntax checks pass, clean merge commit created. |
| **M3** | `tests/test_adk_supervisor.py` | `test_adk_state_transitions`, `test_hitl_branch_gate_pause_resume`, `test_knowledge_vault_extraction_and_search`, `test_artifact_caching` | Full pipeline executes end-to-end with live canvas telemetry and HITL gates. |
| **ALL** | All 18 Test Suites (`tests/`) | Regression test across all existing 44 tests + new test files | 100% test pass rate with zero regressions. |

---

## 7. Execution Status

> **Last updated:** 2026-09-21 · branch `v1` · 195 tests passing · merge commit `fc4d022`
>
> **Immediate next action:** `git push` — the branch is 6 commits ahead of `origin/v1`
> and 0 behind after reconciling the remote's parallel work. Safety tag
> `pre-merge-sprint123` marks the pre-merge state.

### Milestone 1 — Obsidian Vault & Dynamic Canvas ✅ (`4f6f192`)
- [x] `FORGE_VAULT_ROOT` in `issueforge/config.py`; tasks/workspace/DB roots derive from it
- [x] `task_graph_edges` table + async CRUD (no `task_runs` — `runs_json` already covers it)
- [x] `issueforge/vault/manager.py`, `linker.py`, `canvas_builder.py`, `canvas_watcher.py`
- [x] Dossier READMEs gain YAML frontmatter and `[[wikilinks]]`
- [x] `issueforge migrate-vault` — 7 real dossiers relocated, sources tombstoned not deleted
- [x] `tests/test_obsidian_vault.py`, `tests/test_canvas_builder.py`

**Deviations from spec:** no `ALTER TABLE` migrations (those columns already existed);
canvas node identity is `sha1(task_id:stage)`, not list position, which the spec's version
corrupts once a directive card is appended; the steering watcher watches the dossier
directory non-recursively (`awatch` on a missing file raises, and recursion drowns in
sandbox churn).

### Milestone 2 — AST Blast Radius & Merge Guard ✅ (`dc750d4`)
- [x] `issueforge/graph/ast_parser.py` (NetworkX) and `blast_radius.py`
- [x] `issueforge/agents/merge_resolver.py` with syntax-verification gate
- [x] `tests/test_ast_blast_radius.py`, `tests/test_merge_resolver.py`

**Deviations:** blast radius is **advisory only** — it never narrows what the Tester runs.
The merge guard runs in `push_and_create_pr` between commit and push, not as pipeline
stage 5: the Coder's changes are uncommitted until then and git refuses to merge over a
dirty tree. Conflicts therefore surface at push time, not on the confirmation card.

### Milestone 3 — Scoped Down ✅ (`b99f954`)
- [x] Real HITL target-branch gate (`AWAITING_BRANCH_SELECTION` + `target_branch_confirmed`)
- [x] Knowledge vault notes — extends the existing `task_learnings` system, does not duplicate it
- [x] `issueforge/core/caching.py` plan reuse across retries
- [x] `issueforge sync-knowledge` backfill command
- [x] `tests/test_knowledge_and_caching.py`
- [ ] ~~Google ADK supervisory orchestrator rewrite of `engine.py`~~ — **deliberately not built**

**Why the rewrite was dropped:** `google-adk` was ruled out, `SupervisoryOrchestrator`
already triages and delegates, and `engine.py` is the most heavily tested part of the
system. The one concrete guarantee the rewrite was meant to deliver — a genuine
target-branch pause — was implemented directly instead. This is a closed decision, not
pending work.

### Beyond the plan
- [x] Native in-browser canvas renderer (`7afa96d`) — Obsidian is now optional
- [x] `POST /api/tasks/{id}/canvas/directive` — steer without Obsidian
- [x] Merge with 13 remote commits from a parallel contributor (`fc4d022`)

---

## 8. Known Debt

> **Last audited:** 2026-09-22 · 198 tests passing

### Open

- `GitRepoManager.probe_and_merge` / `abort_merge` arrived with the merge and are unused;
  `MergeConflictResolver` does its own probing and aborting. Only the tests reference them.
- The HITL branch gate means poller-ingested tasks no longer run unattended; nothing
  confirms a branch for them automatically. Needs an explicit policy (per-repo default,
  auto-confirm rule, or timeout) rather than an accidental default.
- `get_diff()` uses `git diff -U3 HEAD`, which never shows untracked files, while the
  "no git modifications" guard in `engine.py` reads that same diff. A run whose only output
  is newly created files therefore fails the guard even though it produced real work.
  `get_changed_file_paths()` already uses `git status --porcelain` for exactly this reason.

### Resolved

- ~~`orchestrator.py` passes `primary_model=settings.default_model`, which does not exist~~ —
  the property was added in `issueforge/config.py` (aliases `planner_model`), so the LLM intent
  classifier runs instead of always falling through to the heuristic parser.
- ~~`engine.py` imports `telegram_manager` in the `AWAITING_INPUT` branch without using it~~ —
  it now calls `send_question_alert(task, question)`, so a triage question reaches Telegram
  and not only the dashboard. Covered by
  `tests/test_agents.py::test_engine_sends_triage_question_to_telegram`.
- ~~`PLAN.md` / `TEST_RESULTS.md` / `REVIEW.md` are committed along with the real change~~ —
  `GitRepoManager._exclude_agent_artifacts()` writes them into `.git/info/exclude` at clone
  time. Because git only honours ignore rules for untracked paths, a repository that versions
  its own `PLAN.md` still diffs and commits changes to it. Both directions are covered by
  `tests/test_git_manager.py`.
- ~~`issueforge/web/api.py` defines `post_task_comment_api` twice~~ — the `/comments` duplicate is
  removed. The surviving `/comment` route is the one the dashboard calls, and the only one
  that validates empty input, persists `comments_context` and syncs the dossier.
- ~~A literal `/home/solar1/.local/bin/agy` in `AgySessionRunner.get_agy_path()`~~ — removed;
  resolution is `~/.local/bin/agy`, the standard system paths, then `shutil.which`.
