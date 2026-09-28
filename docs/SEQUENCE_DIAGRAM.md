# Issueforge - Sequence Diagrams (SEQUENCE_DIAGRAM) Document

> **Design history, not current behaviour.** This document describes an earlier planned architecture, including a JWT/RBAC login flow, Redis, PostgreSQL, `/api/v1` and gVisor, none of which was built. The implemented system is described in [README.md](../README.md): auth is one shared `FORGE_AUTH_TOKEN`, storage is SQLite, and there is no container sandbox — sandboxes are plain directories and agents run with the user's full permissions.

This document presents detailed Mermaid sequence diagrams illustrating step-by-step subsystem interactions across key operational workflows in **Issueforge**.

---

## 1. Scenario 1: User Task Initialization & Authentication Flow

Illustrates client authentication, token verification, task payload validation, and initial database persistence.

```mermaid
sequenceDiagram
    autonumber
    actor User as User / CLI
    participant GW as API Gateway
    participant Auth as Auth Service
    participant DB as PostgreSQL DB
    participant Queue as Redis Queue
    participant Orch as Agent Orchestrator

    User->>GW: POST /api/v1/auth/login (Credentials)
    activate GW
    GW->>Auth: Validate Credentials
    activate Auth
    Auth->>DB: Query User Profile & Password Hash
    activate DB
    DB-->>Auth: User Record Found
    deactivate DB
    Auth-->>GW: JWT Bearer Token (RS256)
    deactivate Auth
    GW-->>User: 200 OK (JWT Access & Refresh Tokens)
    deactivate GW

    User->>GW: POST /api/v1/tasks (Task Payload + JWT)
    activate GW
    GW->>Auth: Verify JWT & RBAC Permissions
    activate Auth
    Auth-->>GW: Token Valid (User ID, Tenant Permissions)
    deactivate Auth
    
    GW->>DB: Insert Task Record (Status: CREATED)
    activate DB
    DB-->>GW: Task Persisted (Task UUID)
    deactivate DB

    GW->>Queue: Push Task Event (Task UUID)
    activate Queue
    Queue-->>GW: Ack Enqueued
    deactivate Queue

    GW-->>User: 202 Accepted (Task UUID, Status: QUEUED)
    deactivate GW

    Queue->>Orch: Dispatch Task Event
    activate Orch
    Orch->>DB: Update Task Status to QUEUED
    deactivate Orch
```

---

## 2. Scenario 2: Core Task Execution & Agent Subsystem Interaction

Details the core agent decision loop, RAG context retrieval, sandbox command execution, and verification steps.

```mermaid
sequenceDiagram
    autonumber
    participant Worker as Task Execution Worker
    participant RAG as Code RAG Engine
    participant LLM as LLM Orchestrator Engine
    participant Sandbox as Sandbox Manager (gVisor)
    participant Git as Git Workspace Service
    participant DB as PostgreSQL DB

    Worker->>DB: Update Task Status (ANALYZING)
    Worker->>RAG: Retrieve Code Context (Repo ID, Task Prompt)
    activate RAG
    RAG-->>Worker: Context Bundle (AST Chunks & Symbols)
    deactivate RAG

    Worker->>LLM: Formulate Execution Plan (Prompt + Context)
    activate LLM
    LLM-->>Worker: Execution Plan (Step 1..N)
    deactivate LLM

    Worker->>Sandbox: Provision Container Environment
    activate Sandbox
    Sandbox->>Git: Clone Isolation Branch
    activate Git
    Git-->>Sandbox: Workspace Ready
    deactivate Git
    Sandbox-->>Worker: Container Active (ID: sbx-889)
    deactivate Sandbox

    loop Step Execution & Verification Loop
        Worker->>LLM: Generate Code Modification / Tool Action
        activate LLM
        LLM-->>Worker: Tool Call (File Patch / Terminal Command)
        deactivate LLM

        Worker->>Sandbox: Execute Tool Action in Sandbox
        activate Sandbox
        Sandbox-->>Worker: Execution Result (Stdout, Stderr, Exit Code)
        deactivate Sandbox

        Worker->>Sandbox: Run Verification Suite (Tests / Linter)
        activate Sandbox
        Sandbox-->>Worker: Verification Result (Pass/Fail)
        deactivate Sandbox
    end

    Worker->>Sandbox: Extract Git Diff Patch
    activate Sandbox
    Sandbox-->>Worker: Patch File Contents
    deactivate Sandbox

    Worker->>DB: Update Task Status (WAITING_APPROVAL / COMPLETED)
```

---

## 3. Scenario 3: Asynchronous Task Processing & Real-Time Log Streaming

Shows how terminal output generated inside an execution sandbox is streamed via Redis Pub/Sub and WebSockets to the Web Dashboard.

```mermaid
sequenceDiagram
    autonumber
    actor Client as Web Dashboard UI
    participant WS as WebSocket Gateway
    participant Redis as Redis Pub/Sub
    participant Worker as Execution Worker
    participant Sandbox as Container Sandbox

    Client->>WS: Connect ws://api/ws/v1/tasks/{id}/stream
    activate WS
    WS->>Redis: Subscribe channel "task:logs:{id}"
    activate Redis
    Redis-->>WS: Subscribed
    WS-->>Client: Connection Established (Ack)

    Worker->>Sandbox: Execute Command (e.g. `npm test`)
    activate Sandbox

    loop Console Output Stream
        Sandbox-->>Worker: Stdout Buffer Chunk
        Worker->>Redis: Publish "task:logs:{id}" (Log Payload)
        Redis-->>WS: Deliver Message Payload
        WS-->>Client: Frame Message `log_chunk`
    end

    Sandbox-->>Worker: Command Exit Code 0
    deactivate Sandbox

    Worker->>Redis: Publish "task:status:{id}" (COMPLETED)
    Redis-->>WS: Status Change Event
    WS-->>Client: Frame Message `task_status_changed`
    deactivate Redis
    deactivate WS
```

---

## 4. Scenario 4: Data Sync & RAG Code Indexing Pipeline

Illustrates automated codebase ingestion triggered by git webhooks.

```mermaid
sequenceDiagram
    autonumber
    participant GitHost as Git Provider (GitHub/GitLab)
    participant GW as API Gateway
    participant Ingestion as Ingestion Worker
    participant Parser as Tree-Sitter AST Parser
    participant Embed as Embedding Service
    participant VecDB as Qdrant Vector Store

    GitHost->>GW: POST /api/v1/webhooks/git (Push Event Payload)
    activate GW
    GW-->>GitHost: 202 Accepted
    GW->>Ingestion: Dispatch Sync Job (Repo URL, Commit Hash)
    deactivate GW

    activate Ingestion
    Ingestion->>Parser: Parse Changed Files to AST
    activate Parser
    Parser-->>Ingestion: Symbol Definitions & Code Chunks
    deactivate Parser

    Ingestion->>Embed: Request Embeddings (Batch Code Chunks)
    activate Embed
    Embed-->>Ingestion: Dense Vector Arrays
    deactivate Embed

    Ingestion->>VecDB: Upsert Vectors & Metadata (Repo ID, File Paths)
    activate VecDB
    VecDB-->>Ingestion: Upsert Success
    deactivate VecDB
    deactivate Ingestion
```

---

## 5. Scenario 5: Error Propagation, Retries, and Circuit Breaking

Demonstrates handling of transient infrastructure failures, retries, self-correction, and fallback pathways.

```mermaid
sequenceDiagram
    autonumber
    participant Worker as Execution Worker
    participant Sandbox as Sandbox Container
    participant ErrorH struct as Error Handler Service
    participant LLM as LLM API Provider
    participant DLQ as Dead Letter Queue
    participant Notif as Notification Service

    Worker->>Sandbox: Execute Generated Code Change
    activate Sandbox
    Sandbox-->>Worker: Execution Failed (AssertionError in unit test)
    deactivate Sandbox

    Worker->>ErrorH struct: Handle Exception (Category: VerificationFailure)
    activate ErrorH struct
    ErrorH struct->>ErrorH struct: Check Retry Counter (Attempt 1 of 3)

    alt Retry Available (Attempt < Max)
        ErrorH struct-->>Worker: Retry Directive (Self-Healing Prompt)
        Worker->>LLM: Request Code Repair (Prompt + Error Traceback)
        activate LLM
        LLM-->>Worker: Repaired Code Patch
        deactivate LLM
        Worker->>Sandbox: Re-execute Repaired Code
    else Max Retries Exceeded
        ErrorH struct-->>Worker: Fatal Failure Directive
        Worker->>DLQ: Publish Task to Dead Letter Queue
        Worker->>Notif: Trigger Failure Alert (Slack / Email / Webhook)
        Notif-->>Worker: Alert Sent
    end
    deactivate ErrorH struct
```
