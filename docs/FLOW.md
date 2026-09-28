# Issueforge - Process & System Workflows (FLOW) Document

> **Design history, not current behaviour.** This document describes an earlier planned architecture, including Redis and gVisor, neither of which was built. The implemented system is described in [README.md](../README.md): auth is one shared `FORGE_AUTH_TOKEN`, storage is SQLite, and there is no container sandbox — sandboxes are plain directories and agents run with the user's full permissions.

## 1. End-to-End Task Execution Flow

This document details the operational execution flows, state transitions, decision trees, and data processing pipelines within **Issueforge**.

### 1.1 Complete Task Lifecycle Diagram

```mermaid
flowchart TD
    Start(["User Submits Task"]) --> SubmitAPI["API Gateway Validates Request"]
    SubmitAPI --> AuthCheck{"Authenticated & Authorized?"}
    
    AuthCheck -- No --> Reject401["Return 401/403 Error"]
    AuthCheck -- Yes --> SaveDB["Persist Task (Status: CREATED)"]
    
    SaveDB --> Enqueue["Publish Task to Redis Queue"]
    Enqueue --> WorkerPick["Worker Picks Task (Status: QUEUED)"]
    
    WorkerPick --> Analysis["RAG Engine Retrieves Code Context (Status: ANALYZING)"]
    Analysis --> PlanGen["Agent Generates Execution Plan"]
    
    PlanGen --> Provision["Sandbox Manager Provisions gVisor Container (Status: SANDBOX_PROVISIONED)"]
    Provision --> ExecLoop["Execute Plan Step Loop (Status: EXECUTING)"]
    
    subgraph ExecutionLoop["Sandbox Execution & Self-Correction Loop"]
        ExecLoop --> RunCmd["Run Command / Modify File"]
        RunCmd --> Verify{"Verification Check (Tests/Linter) Pass?"}
        Verify -- Fail (Attempts < 3) --> Feedback["Feed Error back to LLM for Self-Healing"]
        Feedback --> RunCmd
        Verify -- Fail (Attempts >= 3) --> StepFail["Mark Step as Failed"]
    end
    
    StepFail --> TaskFail["Task Status set to FAILED"]
    TaskFail --> TeardownFail["Teardown Sandbox Container"] --> NotifyUser["Notify User via WebSocket / Slack"]
    
    Verify -- Pass --> AllDone{"All Steps Completed?"}
    AllDone -- No --> ExecLoop
    AllDone -- Yes --> GenDiff["Generate Git Patch & Diff Summary"]
    
    GenDiff --> StatusCheck{"Auto-Commit Enabled?"}
    
    StatusCheck -- Yes --> PushGit["Commit & Push to Target Branch"] --> MarkDone["Task Status set to COMPLETED"]
    StatusCheck -- No --> ReviewReq["Task Status set to WAITING_APPROVAL"]
    
    ReviewReq --> UserApprove{"User Approves Diff?"}
    UserApprove -- Approved --> PushGit
    UserApprove -- Rejected --> TaskReject["Task Status set to REJECTED"] --> TeardownPass
    
    MarkDone --> TeardownPass["Teardown Sandbox Container"] --> End(["Task Completed Successfully"])
```

---

## 2. State Transition Model

The state transition diagram below describes all permissible status states for an Issueforge task and the events triggering state changes.

```mermaid
stateDiagram-v2
    [*] --> CREATED : User submits task
    CREATED --> QUEUED : Enqueued in task broker
    QUEUED --> ANALYZING : Worker assigns task & starts RAG lookup
    ANALYZING --> SANDBOX_PROVISIONED : Container environment initialized
    SANDBOX_PROVISIONED --> EXECUTING : First execution step begins
    
    state EXECUTING {
        [*] --> STEP_IN_PROGRESS
        STEP_IN_PROGRESS --> VERIFYING : Code written / command run
        VERIFYING --> SELF_HEALING : Tests/linter failed
        SELF_HEALING --> STEP_IN_PROGRESS : Retry with fixed prompt
        VERIFYING --> STEP_COMPLETED : Verification succeeded
        STEP_COMPLETED --> [*]
    }
    
    EXECUTING --> WAITING_APPROVAL : All steps done (Auto-Commit disabled)
    EXECUTING --> COMMITTING : All steps done (Auto-Commit enabled)
    
    WAITING_APPROVAL --> COMMITTING : User approves diff
    WAITING_APPROVAL --> REJECTED : User rejects diff
    
    COMMITTING --> COMPLETED : Push to Git remote succeeds
    
    EXECUTING --> FAILED : Max retries exceeded or unrecoverable error
    ANALYZING --> FAILED : RAG lookup exception
    SANDBOX_PROVISIONED --> FAILED : Container startup failure
    COMMITTING --> FAILED : Git push rejection / lock conflict
    
    CREATED --> CANCELLED : User cancels task
    QUEUED --> CANCELLED : User cancels task
    EXECUTING --> CANCELLED : User cancels task
    
    COMPLETED --> [*]
    FAILED --> [*]
    REJECTED --> [*]
    CANCELLED --> [*]
```

---

## 3. Data Processing & RAG Ingestion Pipeline

Issueforge indexes git repositories to provide precise, low-latency codebase retrieval during agent execution.

```mermaid
flowchart LR
    subgraph RepoTrigger["Repository Event"]
        Push["Git Push Event / Sync Trigger"]
    end

    subgraph IngestionPipeline["Codebase Indexing Pipeline"]
        Push --> Fetch["Fetch Latest Repository Snapshot"]
        Fetch --> TreeParse["Parse Abstract Syntax Tree (AST)"]
        TreeParse --> Chunking["Chunk Code by Functions, Classes, and Docs"]
        Chunking --> Embed["Generate Dense Vector Embeddings"]
    end

    subgraph Storage["Vector & Context Storage"]
        Embed --> Upsert["Upsert Vectors to Qdrant/pgvector"]
        TreeParse --> GraphIndex["Build Symbol Dependency Graph"]
    end

    subgraph QueryPipeline["Agent RAG Retrieval Pipeline"]
        TaskReq["Agent Request Context"] --> HybridSearch["Hybrid Search (Vector Similarity + AST Graph)"]
        Upsert --> HybridSearch
        GraphIndex --> HybridSearch
        HybridSearch --> ContextBundle["Formatted Context Snippets"]
    end
```

### 3.1 RAG Ingestion Steps:
1. **Tree Parsing**: Converts source code into Abstract Syntax Trees (AST) using Tree-sitter parsers.
2. **Semantic Chunking**: Splits code cleanly along logical function, class, and docstring boundaries rather than arbitrary line counts.
3. **Embedding Generation**: Computes code embeddings using specialized code-embedding models (e.g., `text-embedding-3-large` or `CodeBERT`).
4. **Hybrid Context Retrieval**: Combines cosine similarity vector search with AST graph traversal to return both target code blocks and dependent interface definitions.

---

## 4. Decision-Tree & Error Recovery Logic

```mermaid
flowchart TD
    ErrDetected["Error Detected During Sandbox Step Execution"] --> ErrType{"Identify Error Category"}

    ErrType -- "Compilation / Syntax / Test Error" --> CheckAttempts{"Self-Healing Attempts < 3?"}
    CheckAttempts -- Yes --> ExtractTrace["Extract Error Traceback & Failing Test Name"]
    ExtractTrace --> PromptRepair["Construct Self-Correction LLM Prompt"]
    PromptRepair --> ReExecute["Execute Corrected Step in Sandbox"]
    CheckAttempts -- No --> EscalateFail["Mark Task Status: FAILED (Max Retries Reached)"]

    ErrType -- "Resource Limit Exceeded (OOM / CPU)" --> IncreaseQuota{"Can Scale Sandbox Limits?"}
    IncreaseQuota -- Yes --> ScaleContainer["Resize Container RAM/CPU Limits"] --> ReExecute
    IncreaseQuota -- No --> EscalateFail

    ErrType -- "Git Merge Conflict" --> Rebase{"Attempt Auto-Rebase?"}
    Rebase -- Success --> ReExecute
    Rebase -- Failure --> HumanIntervention["Flag Task: NEEDS_HUMAN_REBASE"]

    ErrType -- "LLM API Rate Limit (429)" --> Backoff["Trigger Exponential Backoff & Jitter Delay"]
    Backoff --> ReExecute
```

### 4.1 Error Recovery Rules:
* **Rule 1 (Syntax / Logic Errors)**: Automatically captured by test runners or linters, passed back to LLM context up to 3 times before failing the step.
* **Rule 2 (System Resource Exhaustion)**: Dynamically resizes container memory limits up to configured maximum threshold before aborting execution.
* **Rule 3 (External API Failures)**: Applies exponential backoff with full jitter to protect against external rate limits and API service degrades.
