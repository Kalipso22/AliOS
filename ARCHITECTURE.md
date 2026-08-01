# AliOS Architecture

## Executive Summary

AliOS is an open-source AI Operating System for autonomous agents.

It is the runtime infrastructure between users, applications, language models, tools, data sources, and operating systems. It enables agents to plan, reason, remember, collaborate, invoke tools, execute workflows, and operate safely across local and cloud environments.

A chatbot is primarily an interaction surface. A model SDK is primarily an API client. AliOS manages durable state, execution, policy, scheduling, integrations, observability, security, and lifecycle.

```text
Chatbot: conversation interface
AI SDK: model API client
AliOS: agent runtime + planning + memory + tools + workflows + policy
```

## Design Goals

| Goal                 | Architectural response                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------- |
| Scalability          | Stateless APIs, durable jobs, queues, worker pools, and pluggable storage.                  |
| Extensibility        | Stable contracts for models, memory, tools, plugins, MCP, storage, and workflows.           |
| Modularity           | Separate runtime, agents, planning, memory, tools, providers, and interface packages.       |
| Security             | Least privilege, capability-based tool access, secret isolation, audit logs, and approvals. |
| Privacy              | Local-first operation, self-hosted storage, scoped memory, and configurable retention.      |
| Reliability          | Durable state, retries, idempotency, checkpoints, fallbacks, and recovery policies.         |
| Performance          | Streaming, batching, caching, lazy loading, bounded concurrency, and routing.               |
| Offline-first        | Local models, SQLite, local vectors, local plugins, and no required cloud control plane.    |
| Cross-platform       | Python runtime, TypeScript clients, Docker, Linux, macOS, and Windows support.              |
| Developer experience | Typed SDKs, CLI tooling, examples, diagnostics, and structured observability.               |

## High-Level Architecture

```text
                                ┌──────────────────────┐
                                │        User          │
                                └──────────┬───────────┘
                                           │
        ┌──────────────────────────────────┼─────────────────────────────────┐
        │                                  │                                 │
┌───────▼────────┐                ┌────────▼────────┐              ┌────────▼────────┐
│ Desktop UI     │                │ CLI             │              │ REST / WebSocket │
└───────┬────────┘                └────────┬────────┘              └────────┬────────┘
        └──────────────────────────────────┼─────────────────────────────────┘
                                           │
                                ┌──────────▼───────────┐
                                │      Core Runtime     │
                                └──────────┬───────────┘
                                           │
 ┌──────────┬──────────┬──────────┬────────┼────────┬──────────┬──────────┐
 │ Planner  │ Memory   │ Agents   │ Tools  │ Provider│ Workflow │ Scheduler│
 └──────────┴──────────┴──────────┴────────┴─────────┴──────────┴──────────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
       ┌──────▼──────┐              ┌──────▼──────┐              ┌──────▼──────┐
       │ LLMs        │              │ Storage     │              │ Plugins/MCP  │
       │ Local/Cloud │              │ SQL/Vectors │              │ Tools/Apps   │
       └─────────────┘              └─────────────┘              └─────────────┘
```

## Core Runtime

The Core Runtime initializes configuration, constructs dependencies, registers capabilities, dispatches events, schedules work, and coordinates graceful shutdown.

```text
Bootstrap → configuration → secrets/storage → plugins/providers
→ capability registration → health checks → accept work → drain → shutdown
```

Responsibilities include configuration validation, dependency injection, runtime state, global policies, background jobs, event dispatch, readiness checks, and shutdown.

The service container owns interfaces rather than concrete implementations:

```text
Runtime Container
├── ConfigurationService
├── SecretProvider
├── EventBus
├── ModelRouter
├── MemoryManager
├── ToolRegistry
├── PolicyEngine
├── WorkflowEngine
├── Scheduler
├── StorageManager
└── ObservabilityService
```

Configuration is layered from defaults, configuration files, environment variables, secret references, and deployment overrides. Invalid configuration fails before the runtime accepts work.

The event bus emits typed events such as AgentRunCreated, PlanGenerated, MemoryRetrieved, ToolCallCompleted, ProviderFallbackTriggered, and RunCompleted. Every event carries workspace, tenant, user, workflow, agent, and run correlation context.

Runtime state is durable. Process memory is an optimization, never the source of truth for resumable work.

## Agent Architecture

An agent has an identity, role, instructions, model policy, memory policy, allowed tools, approval requirements, budgets, and completion criteria.

```text
Created → context initialized → memory retrieved → plan generated
→ execute actions → validate result → reflect/retry/approve/complete
```

Agents use retrieval-first reasoning, plan-and-execute, tool-first execution, critique-and-revise, or specialist delegation. AliOS stores structured action summaries, evidence, tool output, and policy decisions rather than requiring private chain-of-thought persistence.

| Failure class            | Default response                              |
| ------------------------ | --------------------------------------------- |
| Rate limit               | Backoff and retry within budget               |
| Transient provider error | Retry or use a compatible fallback            |
| Validation failure       | Correct arguments or fail safely              |
| Permission denial        | Do not retry without changed authorization    |
| Tool failure             | Retry only when idempotent and policy permits |
| Semantic failure         | Reflect, re-plan, request input, or escalate  |

Multi-agent work uses a supervisor that owns goal progress and worker agents with narrow responsibilities.

```text
Supervisor Agent
├── Researcher: evidence and retrieval
├── Implementer: execution
├── Reviewer: validation
└── Reporter: final synthesis
```

## Memory System

| Memory type       | Purpose                                      | Lifetime          |
| ----------------- | -------------------------------------------- | ----------------- |
| Working memory    | Active plan, task state, recent tool results | Current run       |
| Short-term memory | Conversation and session context             | Session           |
| Long-term memory  | Durable facts, preferences, decisions        | Persistent        |
| Semantic memory   | Meaning-based vector retrieval               | Persistent        |
| Episodic memory   | Prior runs, failures, and outcomes           | Policy-controlled |

Working memory is checkpointed for recovery but is not automatically promoted to long-term memory. Long-term records include source, confidence, scope, owner, retention, and provenance.

```text
Record → chunking/normalization → embedding provider
→ vector index + metadata → filtered semantic retrieval
```

Retrieval combines semantic similarity, metadata filters, access control, source trust, recency, importance, deduplication, and token budgets.

Context compression keeps recent information verbatim, extracts durable facts, summarizes large tool output, and retrieves older material on demand. Pruning removes expired, duplicate, superseded, low-confidence, or user-deleted records according to policy.

## Planner

The Planner converts a goal into an executable task graph.

```text
Goal → constraints → decomposition → dependencies
→ prioritization → execution → observation → re-plan
```

Each task specifies objective, inputs, output, dependencies, required capabilities, risk level, budget, completion criteria, retry policy, and approval policy.

The planner detects dependency cycles, missing capabilities, blocked approval states, and incompatible prerequisites. It prioritizes work by user intent, risk, cost, latency, deadlines, expected value, and worker capacity.

## Workflow Engine

The Workflow Engine runs durable deterministic and agentic graphs.

```text
Trigger → Start → Agent/Tool/Transform → Condition → Approval → Completion
```

Nodes include agent, tool, transform, condition, approval, delay, event wait, sub-workflow, and terminal nodes. Edges define control flow, data flow, conditions, and error paths.

Supported triggers include API calls, UI actions, CLI commands, cron schedules, webhooks, queues, file events, and upstream workflow completion.

Loops require explicit limits. Parallel branches use bounded concurrency and structured aggregation. Every meaningful state transition is checkpointed. Approval nodes create durable pauses for sensitive actions.

## Tool Framework

```text
Agent request → registry lookup → schema validation → permission check
→ rate limit/sandbox → execution → normalized result + audit trace
```

Tool declarations include a stable name, description, input and output schema, permissions, idempotency classification, timeout, rate limit, handler, and audit metadata.

Permissions are capability-based and scoped by tenant, user, agent, environment, credential, and action. High-risk work can execute in containers, restricted subprocesses, isolated browser contexts, network-limited environments, read-only mounts, or separate service accounts.

Every invocation receives a correlation ID and records caller, policy decision, sanitized arguments, duration, result class, and error metadata.

## Provider Layer

The Provider Layer normalizes model and embedding capabilities.

| Provider         | Primary role                                                           |
| ---------------- | ---------------------------------------------------------------------- |
| OpenAI           | Hosted reasoning, structured output, multimodal capability, embeddings |
| Anthropic        | Long-context analysis and tool use                                     |
| Google           | Gemini-based multimodal and enterprise workflows                       |
| DeepSeek         | Coding and reasoning workloads                                         |
| OpenRouter       | Unified hosted provider routing                                        |
| Groq             | Low-latency hosted inference                                           |
| Ollama           | Local model and embedding execution                                    |
| LM Studio        | Desktop-hosted local model serving                                     |
| vLLM             | High-throughput self-hosted inference                                  |
| llama.cpp        | Lightweight GGUF and edge inference                                    |
| Future providers | Stable provider adapter contract                                       |

```text
Agent request → Model Router → capability/policy/budget evaluation
→ provider adapter → local or external inference runtime
```

Adapters expose normalized chat, streaming, tool calling, structured output, embeddings, token estimation, and model metadata. Provider-specific responses do not leak into core contracts.

## Plugin Architecture

Plugins add capability without changing core code.

```text
Plugin package
├── Manifest
├── Compatibility metadata
├── Declared permissions
├── Dependencies
├── Runtime entry point
├── Optional UI extension
└── Documentation and tests
```

Discovery scans configured locations and installed packages. Manifest validation occurs before code loading. Dependency resolution prevents incompatible versions from activating together.

Lifecycle hooks include install, configure, load, register, activate, health check, deactivate, and uninstall. Plugins must release resources and stop background jobs during shutdown.

## MCP Integration

The Model Context Protocol connects AliOS to external tools, resources, and prompts.

```text
AliOS Agent → MCP Client → Policy Adapter → MCP Server
                           ├── server allowlist
                           ├── permission mapping
                           ├── resource controls
                           ├── rate limits
                           └── audit events
```

AliOS discovers MCP capabilities, maps them to local policy, validates requests, manages sessions, isolates credentials, enforces timeouts, and records usage.

## API Layer

The API exposes versioned resources for agents, runs, workflows, tools, memory, artifacts, plugins, schedules, and configuration.

REST handles request-response operations. WebSocket and server-sent event streams deliver model tokens, agent changes, tool progress, approval requests, workflow transitions, and operational events.

Authentication supports local sessions, API keys, OAuth, OpenID Connect, service tokens, and enterprise identity providers. Authorization evaluates tenant, user, role, ownership, policy, and capability context for every action.

Public endpoints are versioned. Rate limits protect availability and control cost.

## Desktop Architecture

AliOS can use a React frontend within Tauri or Electron.

| Choice   | Strength                                                     |
| -------- | ------------------------------------------------------------ |
| Tauri    | Small distribution, native integration, strong Rust boundary |
| Electron | Mature ecosystem and extensive web tooling                   |

```text
Desktop UI → frontend state → validated IPC bridge
├── Local API client
├── Background agent service
├── Notification service
├── Secure credential bridge
└── Local model discovery
```

Privileged operating system operations must never be exposed directly to frontend JavaScript. They pass through validated IPC commands and policy checks.

## CLI Architecture

```text
alios
├── init
├── serve
├── run
├── agents
├── workflows
├── tools
├── plugins
├── memory
├── providers
├── logs
├── doctor
└── config
```

Interactive mode supports guided setup, run inspection, approvals, and diagnostics. Automation mode emits machine-readable JSON and stable exit codes.

## Storage Layer

| Data                   | Local option               | Scaled option                          |
| ---------------------- | -------------------------- | -------------------------------------- |
| Configuration          | Local files                | Managed configuration service          |
| Run and workflow state | SQLite                     | PostgreSQL                             |
| Cache and queues       | In-memory                  | Redis                                  |
| Semantic vectors       | Local vector store         | Managed or self-hosted vector database |
| Logs                   | Local JSON logs            | Centralized log platform               |
| Artifacts              | Local filesystem           | Object storage                         |
| Secrets                | OS keychain or environment | External secret manager                |

Storage implementations require migrations, backups, encryption, retention, tenant boundaries, and deletion semantics.

## Security Architecture

```text
Identity → authentication → authorization and policy
→ agent/memory/tool/secret scopes → audited execution
```

Encryption protects remote transport and sensitive storage. Secrets are resolved at execution time and are never embedded in prompts, logs, plugin manifests, workflow definitions, or client bundles.

Supply-chain controls include lockfiles, provenance checks, vulnerability scanning, signed releases where available, and review of high-risk packages.

Audit records include identity, target, policy decision, action, timestamp, correlation ID, result, and sanitized metadata.

## Observability

| Signal        | Purpose                                                              |
| ------------- | -------------------------------------------------------------------- |
| Metrics       | Latency, error rate, throughput, queue depth, token use, cost        |
| Traces        | Correlate API, agents, planning, retrieval, models, tools, workflows |
| Logs          | Structured diagnostics and audit information                         |
| Profiles      | CPU, memory, I/O, database, and inference analysis                   |
| Health checks | Readiness and liveness of dependencies                               |

Sensitive prompt and tool data is redacted, hashed, sampled, or disabled according to policy.

## Performance Strategy

AliOS optimizes complete task performance rather than only token generation speed.

- Cache embeddings, metadata, and safe read-only results.
- Lazy-load optional plugins, large indexes, and specialized providers.
- Bound concurrency by tenant, tool, provider, model, and resource.
- Batch embedding and compatible provider requests.
- Stream model output and long-running tool events.
- Use GPU acceleration when available and cost-effective.
- Route simple tasks to appropriate lower-cost models.
- Compress context and retrieve only relevant evidence.
- Apply backpressure when queues or providers are saturated.
- Benchmark realistic end-to-end workflows.

## Deployment Models

| Model         | Description                                                                  |
| ------------- | ---------------------------------------------------------------------------- |
| Local         | One device with API, runtime, SQLite, vectors, and local models.             |
| Docker        | Reproducible local or small-server service topology.                         |
| Remote server | Stateless APIs, durable storage, and queue-backed workers.                   |
| Hybrid        | Private data and tools remain local while approved cloud models assist.      |
| Cloud         | Autoscaled APIs and workers with managed data and observability services.    |
| Enterprise    | Multi-tenancy, identity, segmentation, policy, audit, and regional controls. |

## Repository Structure

```text
AliOS/
├── apps/
│   ├── api/
│   ├── cli/
│   ├── desktop/
│   └── console/
├── packages/
│   ├── core/
│   ├── runtime/
│   ├── agents/
│   ├── planner/
│   ├── memory/
│   ├── tools/
│   ├── models/
│   ├── workflows/
│   ├── plugins/
│   ├── mcp/
│   ├── storage/
│   ├── observability/
│   └── sdk-typescript/
├── python/alios/
├── plugins/
├── examples/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   ├── evaluations/
│   └── benchmarks/
├── docs/
├── docker/
├── scripts/
├── README.md
├── CONTRIBUTING.md
└── ARCHITECTURE.md
```

## Technology Stack

| Technology        | Role                        | Why                                               |
| ----------------- | --------------------------- | ------------------------------------------------- |
| Python            | Runtime and AI integrations | Mature AI ecosystem and readable service code     |
| TypeScript        | SDKs and frontend           | Type safety across web and tooling                |
| FastAPI           | API layer                   | Async support, validation, and OpenAPI generation |
| React             | Operations console          | Mature component ecosystem                        |
| Tauri or Electron | Desktop shell               | Cross-platform local integration                  |
| SQLite            | Local durable state         | Embedded, reliable, and portable                  |
| PostgreSQL        | Server durable state        | Transactional and operationally mature            |
| Docker            | Deployment                  | Reproducible environments                         |
| Redis             | Cache and queues            | Low-latency coordination primitives               |
| Vector database   | Semantic memory             | Similarity search with metadata filtering         |

## Future Architecture

Future architecture extends AliOS with voice agents, speech streaming, vision and document pipelines, mobile clients, cloud orchestration, distributed agents, signed marketplaces, and enterprise administration.

Voice will require interruption handling and low-latency state. Vision will require privacy-aware multimodal ingestion and retrieval. Distributed agents will require durable messaging, capability discovery, shared task boards, supervision, and fault recovery.

## Architecture Principles

1. Treat agents as controlled runtime entities, not prompt strings.
2. Keep models, storage, tools, and providers interchangeable.
3. Prefer explicit plans, policies, permissions, and state transitions.
4. Make local and private deployment first-class.
5. Store durable state outside process memory.
6. Apply least privilege to external capability.
7. Make high-risk actions observable, auditable, and approval-aware.
8. Design for retries, fallbacks, checkpoints, and recovery.
9. Optimize complete workflows, not isolated benchmark numbers.
10. Preserve public contracts and evolve them deliberately.
11. Make extensions additive through plugins and provider interfaces.
12. Keep behavior visible through metrics, traces, logs, and health checks.
13. Let users control data, memory, models, and deployment topology.
14. Prefer composable abstractions over hidden magic.
15. Build infrastructure that remains understandable as it scales.
