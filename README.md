# AliOS

### Open-Source AI Operating System for Autonomous Agents

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5%2B-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-4B5563)](#installation)
[![Open Source](https://img.shields.io/badge/Open%20Source-Yes-3DA639?logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active%20Development-brightgreen)](#roadmap)

AliOS is a modular AI Operating System for building autonomous, secure, and extensible AI agents. It gives agents a unified runtime for reasoning, memory, planning, tool use, collaboration, and interaction with local or cloud language models.

> [!IMPORTANT]
> AliOS is not another chatbot framework. It is operating infrastructure for AI: a composable foundation for agents that can understand goals, retain context, coordinate work, execute approved actions, and improve through feedback.

---

## A platform for agents that do real work

Modern language models are powerful, but a model alone is not an autonomous system. It does not persist state safely, schedule work, control tools, manage permissions, recover from failures, or coordinate specialized agents.

AliOS supplies those missing capabilities through a clean, provider-neutral architecture. Build an assistant, coding agent, research system, business workflow, local private copilot, or multi-agent application without locking your product to one model vendor or one deployment environment.

## Table of Contents

- [Vision](#vision)
- [Philosophy](#philosophy)
- [Core Features](#core-features)
- [Architecture Overview](#architecture-overview)
- [Supported AI Providers](#supported-ai-providers)
- [Local AI Support](#local-ai-support)
- [Agent System](#agent-system)
- [Memory System](#memory-system)
- [Tool System](#tool-system)
- [Plugin System](#plugin-system)
- [MCP Support](#mcp-support)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Development Guide](#development-guide)
- [Roadmap](#roadmap)
- [Security](#security)
- [Performance Goals](#performance-goals)
- [Benchmarks](#benchmarks)
- [Contributing](#contributing)
- [Community](#community)
- [FAQ](#faq)
- [License](#license)
- [Acknowledgements](#acknowledgements)

## Vision

AI is moving from question answering to action taking.

Today’s assistants are usually stateless interfaces around a single model call. They can produce impressive answers, but they often lose context between sessions, struggle to execute multi-step work reliably, cannot safely interact with real systems, and are tightly coupled to a specific cloud provider.

That model is insufficient for the next generation of AI software.

An autonomous system needs durable memory, a planning loop, controlled execution, observability, policy enforcement, model routing, failure recovery, and a way to connect to the tools people already use. It needs an architecture that can run privately on a laptop, at scale in the cloud, or across both environments.

AliOS exists to provide that architecture.

It treats AI agents as first-class computing entities. An agent receives a goal, forms and revises a plan, delegates when useful, retrieves knowledge, invokes tools under explicit permissions, records outcomes, and returns a traceable result.

The goal is not to hide complexity. The goal is to make the right complexity reusable, inspectable, and accessible to every developer.

## Philosophy

### Local First

Your AI system should be able to run where your data lives. AliOS supports local models, local embeddings, local storage, and self-hosted deployments so critical workflows remain useful even without a public cloud dependency.

### Privacy First

Sensitive prompts, documents, credentials, and execution traces belong under your control. AliOS is designed around explicit provider configuration, scoped secrets, auditable tool access, and deployment flexibility.

### Modular

Every major concern is separated behind stable interfaces: models, agents, memory stores, vector databases, tools, planners, transports, and user interfaces can be replaced independently.

### Extensible

A system that cannot be extended eventually becomes a constraint. AliOS uses plugins, provider adapters, custom tools, workflow definitions, and MCP servers to make new capabilities additive rather than invasive.

### Open Source

The infrastructure that decides, remembers, and acts on behalf of people should be inspectable. AliOS is built in the open so its behavior can be understood, audited, improved, and adapted.

### Developer Friendly

A powerful runtime should still feel pleasant to use. AliOS provides clear Python and TypeScript entry points, a CLI, documented configuration, structured logs, typed APIs, and predictable deployment paths.

## Core Features

| Capability | Description |
|---|---|
| Multi-agent architecture | Coordinate specialized agents with roles, shared context, delegation, and clear execution boundaries. |
| Memory system | Persist conversation state, facts, documents, preferences, and execution outcomes across sessions. |
| Planning | Convert high-level goals into explicit, revisable task plans with checkpoints and dependencies. |
| Tool calling | Register local or remote tools with schemas, validation, permission scopes, and execution tracing. |
| Local LLM support | Run private, offline-capable workloads with Ollama, LM Studio, llama.cpp, vLLM, or Transformers. |
| Cloud LLM support | Connect to leading hosted providers through a consistent model interface. |
| MCP | Discover and use Model Context Protocol servers as secure, structured agent capabilities. |
| Plugins | Add integrations, providers, tools, workflows, UI modules, and domain-specific behavior without forking core. |
| RAG | Ground agent responses in documents, databases, files, and knowledge collections. |
| Vector memory | Store and retrieve semantically relevant memories using configurable embedding and vector backends. |
| Workflows | Compose deterministic and agentic steps into reusable, observable automations. |
| Autonomous execution | Run goal-to-result loops with budgets, approval gates, retries, and stop conditions. |
| Scheduling | Trigger agent jobs on intervals, cron schedules, events, or queues. |
| API | Expose agents, runs, memory, tools, workflows, and telemetry through a typed service API. |
| GUI | Inspect conversations, plans, tool calls, memory, providers, and runtime status from a browser-based console. |
| CLI | Develop, run, inspect, test, and deploy AliOS from the terminal. |
| Observability | Capture structured traces, model usage, latency, decisions, tool outcomes, and failures. |
| Logging | Emit searchable JSON logs with correlation IDs across agents, workflows, and tool executions. |

## Architecture Overview

AliOS is organized as layers. Each layer has a narrow responsibility and communicates through typed contracts.

```text
┌──────────────────────────────────────────────────────────────────────┐
│                            User Interfaces                           │
│             Web GUI · CLI · Python SDK · TypeScript SDK              │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│                              API Layer                               │
│        Authentication · Sessions · Streaming · Webhooks · Jobs       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│                            AliOS Runtime                             │
│  Agent Orchestrator · Workflow Engine · Scheduler · Policy Engine    │
└───────┬──────────────┬──────────────┬──────────────┬────────────────┘
        │              │              │              │
┌───────▼──────┐ ┌─────▼──────┐ ┌─────▼───────┐ ┌────▼───────────────┐
│   Planner    │ │   Memory   │ │    Tools    │ │ Model Router       │
│ Goals/Tasks  │ │ Context/RAG│ │ MCP/Plugins │ │ Local/Cloud Models │
└───────┬──────┘ └─────┬──────┘ └─────┬───────┘ └────┬───────────────┘
        │              │              │              │
┌───────▼──────────────▼──────────────▼──────────────▼───────────────┐
│                           Provider Layer                             │
│ LLMs · Embeddings · Vector DBs · SQL · Files · Queues · Integrations │
└──────────────────────────────────────────────────────────────────────┘
```

### Core

The core defines shared types, configuration, lifecycle management, events, errors, policy primitives, and extension contracts. It deliberately avoids application-specific assumptions.

### Runtime

The runtime owns an agent run from creation to completion. It creates execution context, loads memory, invokes the planner, routes model requests, calls tools, records traces, applies policies, and produces an auditable result.

### Providers

Providers adapt external systems to AliOS interfaces. A provider may represent an LLM API, local inference server, embedding model, vector database, relational database, queue, secret manager, or observability backend.

### Memory

The memory layer stores short-lived context, durable user or agent facts, semantic records, documents, embeddings, and run histories. Retrieval is controlled by relevance, recency, source policy, and token budget.

### Planner

The planner turns a goal into a task graph. It tracks dependencies, estimates progress, asks for clarification when necessary, and may re-plan when tool output or environmental changes invalidate assumptions.

### Agents

Agents are role-aware runtime entities. Each agent has instructions, model preferences, allowed tools, memory access rules, execution limits, and optional collaborators.

### Tools

Tools expose controlled capabilities such as file access, search, databases, code execution, messaging, business APIs, and internal services. Every invocation is schema-validated and traceable.

### Models

The model router abstracts provider differences. Applications can select a provider explicitly, use policy-based routing, configure fallbacks, or direct certain workloads to local inference.

### API

The API provides programmatic access to AliOS resources and supports streaming agent events. It is suitable for web apps, internal platforms, mobile clients, CI systems, and backend services.

### Frontend

The frontend is an operational console for agents. It surfaces run status, reasoning summaries, plans, memory retrieval, tool permissions, provider health, and logs without exposing sensitive chain-of-thought content.

### Storage

AliOS supports pluggable storage for configuration, run metadata, conversations, documents, vectors, artifacts, and audit events. Storage may be local, self-hosted, or managed.

### Plugins

Plugins package optional functionality and register with the runtime through a controlled lifecycle. They can contribute providers, agents, tools, API routes, user interface panels, commands, and workflow templates.

### MCP

MCP support makes external capabilities available through a standardized protocol. AliOS can connect to MCP servers, inspect their tools and resources, apply local policy, and expose them to permitted agents.

## Supported AI Providers

AliOS is provider-neutral. The same agent interface can target local inference, a hosted API, or a policy-controlled hybrid route.

| Provider | Chat / Reasoning | Embeddings | Typical Use |
|---|---:|---:|---|
| OpenAI | Yes | Yes | General-purpose reasoning, structured output, multimodal workloads |
| Anthropic | Yes | Via adapter | Long-context analysis and careful tool-use workflows |
| Google | Yes | Yes | Gemini-based multimodal and enterprise workflows |
| OpenRouter | Yes | Provider-dependent | Unified access to multiple hosted model families |
| Groq | Yes | Via adapter | Low-latency hosted inference |
| DeepSeek | Yes | Via adapter | Reasoning and coding workloads |
| Ollama | Yes | Yes | Local development and private inference |
| LM Studio | Yes | Yes | Desktop-hosted local model serving |
| vLLM | Yes | Via adapter | High-throughput self-hosted serving |
| llama.cpp | Yes | Via adapter | Lightweight local and edge inference |

| Routing Mode | Behavior |
|---|---|
| Explicit | A run always uses the named provider and model. |
| Fallback | AliOS retries a compatible secondary provider after a qualifying failure. |
| Policy-based | Route by data sensitivity, latency target, budget, capability, or deployment region. |
| Hybrid | Keep retrieval and private context local while using an approved cloud model for generation. |

> [!NOTE]
> Provider availability depends on credentials, enabled adapters, and the deployment configuration. AliOS does not require a cloud model provider to run locally.

## Local AI Support

AliOS is designed to make local AI a first-class deployment target.

### GGUF

GGUF models can be served through llama.cpp-compatible runtimes and are well suited to desktop, edge, and offline deployments. AliOS connects through a model adapter and treats local endpoints like any other provider.

### Transformers

For Python-native deployments, AliOS can use Hugging Face Transformers-compatible inference stacks. This is useful for custom models, fine-tuned models, specialized embedding models, and research workflows.

### CUDA

NVIDIA GPU acceleration is supported through compatible local runtimes. Use CUDA-enabled backends when maximizing throughput or reducing latency for larger models.

### ROCm

AMD GPU deployments can use ROCm-capable model runtimes where available. AliOS keeps the application-facing model contract consistent across hardware choices.

### CPU Inference

CPU-only inference is a supported use case for lightweight assistants, low-cost servers, edge devices, and privacy-sensitive environments. Select an appropriately sized and quantized model for reliable performance.

### Quantization

Quantized models reduce memory usage and often improve local deployment practicality. AliOS does not prescribe one quantization format; choose the format supported by your selected runtime and hardware.

> [!WARNING]
> Local model quality, context length, speed, and tool-use reliability vary substantially by model and hardware. Test production agent workflows with the exact model, quantization, and runtime you intend to deploy.

## Agent System

An AliOS agent is a controlled execution loop, not a single prompt.

```text
Goal
 │
 ▼
Understand ──► Retrieve relevant memory
 │
 ▼
Plan ──► Decompose tasks ──► Set checkpoints
 │
 ▼
Act ──► Select model / invoke tools / delegate
 │
 ▼
Observe ──► Validate outputs / update memory
 │
 ▼
Reflect ──► Retry, correct, re-plan, or finish
```

### Task decomposition

Agents break large goals into smaller, verifiable tasks. Plans can be linear, branching, or dependency-aware depending on the workflow.

### Planning

Planning is explicit state, not hidden prompt text. Plans include task status, dependencies, expected outputs, time or token budgets, and approval requirements.

### Memory

Agents use scoped memory to avoid both amnesia and uncontrolled context growth. Memory access can be limited by user, tenant, agent, workspace, source, or retention policy.

### Reasoning

AliOS supports structured reasoning workflows such as plan-and-execute, critique-and-revise, retrieval-first, tool-first, and specialist delegation. Reasoning summaries and outcomes can be logged without persisting private chain-of-thought.

### Execution

The runtime selects the next action, invokes models or tools, captures outputs, validates result shapes, and maintains a complete execution trace.

### Reflection

Agents may assess whether a result satisfies the requested objective, detect missing evidence, compare output against a rubric, or request a focused follow-up action.

### Retry

Retries are policy-driven. AliOS distinguishes transient provider errors, rate limits, validation failures, tool failures, and semantic failures so a retry does not blindly repeat unsafe work.

### Self-correction

When an action fails, the agent can inspect the error, revise parameters, choose an alternative tool, request additional context, fall back to another model, or re-plan the remaining work.

## Memory System

Memory makes an agent useful beyond one message.

| Memory Type | Purpose | Typical Lifetime |
|---|---|---|
| Short-term memory | Current conversation, active task state, recent tool output | One run or session |
| Long-term memory | Durable facts, preferences, decisions, and learned outcomes | Persistent |
| Semantic memory | Meaning-based records retrieved by similarity | Persistent |
| Episodic memory | Past actions, runs, failures, and outcomes | Persistent or policy-limited |
| Working memory | Planner state, task graph, intermediate artifacts | Active workflow |

### Short-term memory

Short-term memory provides the immediate context needed to complete a task. It is token-budgeted and may be summarized automatically as a session grows.

### Long-term memory

Long-term memory retains explicitly approved or policy-eligible knowledge, such as preferences, project conventions, stable decisions, and reusable facts.

### Semantic memory

Semantic memory stores records alongside vector embeddings. This lets an agent retrieve relevant knowledge even when wording differs from the original source.

### Vector database

AliOS supports interchangeable vector backends. Choose a local database for private development, a self-hosted service for controlled deployments, or a managed service for operational scale.

### Embeddings

Embedding providers are configured separately from generation models. This allows local document indexing with a local embedding model while using a different model for synthesis.

### Retrieval

Retrieval combines semantic similarity with metadata filters, recency, source trust, access control, and token limits. It is designed to return useful context rather than indiscriminately inject everything into a prompt.

## Tool System

Tools give agents the ability to affect or inspect the world.

```text
Agent Decision
      │
      ▼
Tool Registry ──► Schema Validation ──► Permission Check
                                            │
                                            ▼
                                      Tool Execution
                                            │
                                            ▼
                                 Result Normalization
                                            │
                                            ▼
                                   Trace + Agent Context
```

### Registration

Tools are registered with a stable name, human-readable description, typed input schema, typed output contract, permission requirements, and execution handler.

```python
from alios import AliOS, tool

app = AliOS()

@tool(
    name="weather.lookup",
    description="Return the current weather for a city.",
    permissions=["network:weather"],
)
async def get_weather(city: str) -> dict:
    return {"city": city, "condition": "clear", "temperature_c": 24}

app.tools.register(get_weather)
```

### Execution

Before execution, AliOS validates the agent-provided arguments against the tool schema. Tool results are normalized into structured records so agents and clients can consume predictable output.

### Permissions

Tools operate under explicit policy. Permissions can be scoped by agent, user, tenant, environment, tool category, credential, or action. High-impact actions can require user approval before execution.

> [!WARNING]
> Never grant unrestricted shell, filesystem, database, financial, messaging, or production-admin access to an autonomous agent. Use least privilege, allowlists, sandboxing, approval gates, and audit logging.

## Plugin System

Plugins allow AliOS to grow without turning the core runtime into a monolith.

A plugin can contribute:

- Model, embedding, storage, or vector providers
- Tools and MCP integrations
- Agents and agent templates
- Workflow templates and schedulers
- API routes and authentication adapters
- CLI commands
- GUI panels and operational views
- Domain-specific policies and evaluators

### Plugin lifecycle

1. Discovery finds installed plugin packages.
2. Manifest validation checks metadata, compatibility, and declared permissions.
3. Loading imports the plugin in a controlled runtime context.
4. Registration adds its capabilities to the appropriate registries.
5. Activation starts optional background services or hooks.
6. Shutdown releases resources cleanly during runtime termination.

### Plugin loading

Plugins are loaded from configured directories or installed packages. AliOS validates compatibility before activation and records plugin versions in runtime metadata.

### Plugin discovery

The CLI and GUI can list available plugins, their capabilities, declared permissions, active state, and configuration requirements.

## MCP Support

The Model Context Protocol (MCP) is an open protocol for connecting AI applications to external tools, resources, and prompts through a common interface.

AliOS treats MCP servers as discoverable capability providers.

```text
AliOS Agent
    │
    ▼
MCP Client + Policy Layer
    │
    ├──► Filesystem MCP Server
    ├──► GitHub MCP Server
    ├──► Database MCP Server
    ├──► Browser MCP Server
    └──► Internal Company MCP Server
```

External MCP tools remain subject to AliOS policies. The runtime can restrict which agents see a server, which tools they may invoke, what arguments are permitted, and whether a human must approve the action.

This makes MCP useful for both personal automation and enterprise integration without bypassing local governance.

## Installation

### Requirements

- Python 3.11 or newer
- Node.js 20 or newer for the web console and TypeScript SDK
- Docker Desktop or Docker Engine for containerized deployment
- An AI provider credential or a compatible local model server

### Linux

```bash
git clone https://github.com/aliar/AliOS.git
cd AliOS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
alios doctor
```

### macOS

```bash
git clone https://github.com/aliar/AliOS.git
cd AliOS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
alios doctor
```

### Windows PowerShell

```powershell
git clone https://github.com/aliar/AliOS.git
Set-Location AliOS
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[all]"
alios doctor
```

### Docker

```bash
git clone https://github.com/aliar/AliOS.git
cd AliOS
docker compose up --build
```

The API and web console are then available through the ports configured in `docker-compose.yml`.

### Python package

```bash
python -m pip install alios
```

### Node.js package

```bash
npm install @alios/sdk
```

### Configure a provider

```bash
export ALIOS_MODEL_PROVIDER=openai
export OPENAI_API_KEY="your-api-key"
export ALIOS_MODEL_NAME="gpt-4.1-mini"
```

For a local Ollama deployment:

```bash
export ALIOS_MODEL_PROVIDER=ollama
export ALIOS_MODEL_NAME="llama3.2"
export ALIOS_OLLAMA_BASE_URL="http://localhost:11434"
```

> [!NOTE]
> Keep credentials in environment variables or a managed secret store. Do not commit API keys, access tokens, or production connection strings.

## Quick Start

Create a minimal agent:

```python
import asyncio
from alios import Agent, AliOS

app = AliOS(
    model={
        "provider": "ollama",
        "name": "llama3.2",
        "base_url": "http://localhost:11434",
    }
)

researcher = Agent(
    name="researcher",
    instructions=(
        "Research the user's question, cite the evidence you used, "
        "and state uncertainty clearly."
    ),
    tools=["web.search"],
)

async def main():
    result = await app.run(
        agent=researcher,
        goal="Compare the benefits and risks of local AI deployment."
    )
    print(result.output)

asyncio.run(main())
```

Run the API service:

```bash
alios serve --host 0.0.0.0 --port 8000
```

Create and run a workflow:

```python
from alios import Agent, Workflow

analyst = Agent(name="analyst", instructions="Analyze the supplied data.")
writer = Agent(name="writer", instructions="Create a concise executive report.")

workflow = Workflow("weekly-report")
workflow.add_agent_step("analyze", analyst)
workflow.add_agent_step("write", writer, depends_on=["analyze"])

result = await workflow.run(
    input={"source": "Weekly sales data and customer feedback"}
)

print(result.output)
```

Call AliOS from TypeScript:

```ts
import { AliOSClient } from "@alios/sdk";

const client = new AliOSClient({
  baseUrl: "http://localhost:8000",
});

const run = await client.runs.create({
  agent: "researcher",
  goal: "Summarize the latest project activity from the connected sources.",
});

for await (const event of client.runs.stream(run.id)) {
  console.log(event.type, event.data);
}
```

## Project Structure

```text
AliOS/
├── apps/
│   ├── api/                    # API service and transport adapters
│   ├── cli/                    # Command-line interface
│   └── console/                # Web-based operations console
├── packages/
│   ├── core/                   # Shared contracts, events, configuration
│   ├── runtime/                # Agent execution and orchestration
│   ├── agents/                 # Agent definitions and collaboration
│   ├── planner/                # Planning, task graphs, reflection
│   ├── memory/                 # Context, RAG, retrieval, vector memory
│   ├── tools/                  # Tool registry, policies, execution
│   ├── models/                 # Model routing and provider adapters
│   ├── workflows/              # Durable workflow and scheduler engine
│   ├── plugins/                # Plugin discovery and lifecycle
│   ├── mcp/                    # MCP client and server integration
│   └── sdk-typescript/         # TypeScript client SDK
├── python/
│   └── alios/                  # Python SDK and runtime package
├── plugins/                    # Official optional plugins
├── examples/                   # Runnable examples and starter projects
├── tests/
│   ├── unit/
│   ├── integration/
│   └── evaluation/
├── docs/                       # Architecture and user documentation
├── docker/                     # Container images and deployment assets
├── pyproject.toml
├── package.json
├── docker-compose.yml
├── LICENSE
└── README.md
```

## Development Guide

Set up the Python environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Install frontend and TypeScript dependencies:

```bash
npm install
```

Run checks before submitting changes:

```bash
pytest
ruff check .
mypy python/alios
npm run lint
npm run test
```

Start the local development stack:

```bash
docker compose up --build
```

Run a focused example:

```bash
python examples/agents/research_assistant.py
```

Development principles:

- Keep core interfaces provider-neutral.
- Add tests for new execution paths and policy behavior.
- Prefer structured events over unstructured logs.
- Preserve backward compatibility for public SDK contracts.
- Treat tool permissions and tenant boundaries as security-sensitive code.
- Document every new configuration key and extension point.

## Roadmap

### Core

- [x] Provider-neutral model abstraction
- [x] Agent runtime and tool registry
- [x] Memory and retrieval primitives
- [x] Python and TypeScript SDK foundations
- [ ] Durable workflow state and resumable runs
- [ ] Policy-as-code engine
- [ ] First-class evaluation and regression framework

### Desktop

- [ ] Native desktop application
- [ ] Local model setup assistant
- [ ] Offline workspace and encrypted local storage
- [ ] Visual workflow editor

### Mobile

- [ ] Mobile companion application
- [ ] Secure approval notifications
- [ ] Voice-first agent interactions

### Voice

- [ ] Streaming speech-to-text support
- [ ] Text-to-speech provider abstraction
- [ ] Real-time voice agent runtime
- [ ] Wake-word and local voice options

### Vision

- [ ] Image and document understanding
- [ ] Screen-aware desktop automation
- [ ] Visual retrieval pipelines
- [ ] Multimodal agent evaluation

### Agents

- [ ] Agent teams with shared task boards
- [ ] Long-running background agents
- [ ] Human-in-the-loop review queues
- [ ] Agent skill libraries and reusable profiles

### Marketplace

- [ ] Signed plugin distribution
- [ ] Plugin compatibility and security review metadata
- [ ] Community workflow templates
- [ ] Managed dependency updates

### Cloud

- [ ] Managed control plane
- [ ] Team workspaces and organization policies
- [ ] Enterprise identity integrations
- [ ] Hosted observability and evaluation dashboards

## Security

AliOS is designed to make secure agent behavior possible; secure deployment still requires deliberate configuration.

Key practices:

- Use least-privilege tool permissions.
- Keep credentials in a secret manager or environment variables.
- Isolate untrusted code execution in dedicated sandboxes.
- Require approval for destructive, financial, messaging, or production actions.
- Scope memory and retrieval by tenant, user, and data classification.
- Encrypt sensitive data in transit and at rest.
- Record audit events for model calls, tool invocations, policy decisions, and approvals.
- Pin provider, plugin, and container versions in production.
- Review third-party MCP servers and plugins before enabling them.
- Set explicit time, token, cost, retry, and recursion limits for autonomous runs.

> [!WARNING]
> An agent with broad credentials and unrestricted tools can cause real-world harm. Treat agent permissions with the same rigor as production service accounts.

To report a security issue, use the repository’s private security advisory workflow. Do not disclose unpatched vulnerabilities in public issues.

## Performance Goals

AliOS targets predictable operational behavior rather than a single benchmark number.

| Area | Goal |
|---|---|
| Runtime overhead | Keep orchestration overhead small relative to model and tool latency |
| Streaming | Deliver model and agent events as they are produced |
| Tool execution | Support concurrent, policy-controlled independent tool calls |
| Retrieval | Keep common vector retrieval operations low latency with metadata filtering |
| Scalability | Run locally for one user or horizontally across workers for many concurrent jobs |
| Reliability | Resume durable workloads after transient process or provider failures |
| Cost control | Track model usage and enforce per-run or per-tenant budgets |
| Observability | Make every significant agent action traceable by run and correlation ID |

## Benchmarks

Agent benchmarks must be reproducible and tied to a complete configuration: model, provider, prompts, tools, hardware, dataset, evaluation criteria, and run budget.

AliOS evaluates systems across these dimensions:

| Dimension | What is measured |
|---|---|
| Task success | Whether the requested outcome is correct and complete |
| Tool reliability | Successful calls, validation failures, retries, and recovery |
| Grounding | Whether outputs are supported by retrieved or supplied evidence |
| Latency | End-to-end runtime and per-stage timing |
| Cost | Token usage, model cost, and tool cost per completed task |
| Safety | Blocked policy violations, approval behavior, and sandbox escapes |
| Stability | Variance across repeated runs with controlled inputs |
| Local efficiency | Memory use, throughput, and latency on supported local hardware |

Benchmark results should never be compared without their full run configuration. A fast result from a small model, permissive tool policy, or simplified dataset is not equivalent to a reliable production workflow.

## Contributing

Contributions are welcome across runtime engineering, providers, local inference, documentation, testing, design, examples, plugins, and community support.

1. Fork the repository.
2. Create a focused branch.
3. Make your change with tests and documentation.
4. Run the relevant quality checks.
5. Open a pull request describing the problem, approach, and validation.

Good first contributions include:

- Adding a provider adapter
- Improving a quick-start example
- Expanding test coverage for an edge case
- Creating an MCP integration
- Improving accessibility in the console
- Writing evaluation fixtures
- Clarifying documentation

Please keep pull requests narrowly scoped. Large architectural changes should begin with a design discussion so maintainers and contributors can align before implementation.

## Community

AliOS is built for developers, researchers, builders, and organizations working toward open, capable, and trustworthy AI systems.

Join the project through:

- GitHub Issues for reproducible bugs and feature requests
- GitHub Discussions for questions, ideas, and architecture conversations
- Pull Requests for code, documentation, tests, and integrations
- Community plugins for reusable tools and domain capabilities

Be respectful, assume good intent, protect users’ privacy, and help make the project welcoming to contributors at every experience level.

## FAQ

### What is AliOS?

AliOS is an open-source operating system layer for autonomous AI agents. It provides runtime infrastructure for planning, memory, tool use, workflows, model routing, integrations, and observability.

### Is AliOS a chatbot?

No. A chat interface can be built on AliOS, but AliOS itself is the underlying system for agents that can reason, remember, plan, and act.

### Can I use AliOS without a cloud API?

Yes. AliOS supports local model providers such as Ollama, LM Studio, llama.cpp, vLLM, and compatible Transformers deployments.

### Does AliOS require a GPU?

No. It can run with CPU inference, although a GPU can improve latency and throughput for supported workloads.

### Which programming languages does AliOS support?

AliOS provides Python and TypeScript entry points. The core architecture is designed for language-neutral service integration.

### Can I connect OpenAI and local models in the same application?

Yes. Use explicit routing, fallback routing, or policies that send different tasks to different providers.

### How does AliOS store memory?

Memory is stored through configurable backends. This can include local files, relational databases, vector databases, and self-hosted or managed services.

### Can users control what an agent remembers?

Yes. Memory policies can define retention, scope, source trust, user consent, and deletion behavior.

### What is RAG in AliOS?

Retrieval-augmented generation retrieves relevant documents or memory before a model generates an answer, helping ground responses in available knowledge.

### What is MCP?

The Model Context Protocol is a standard for connecting AI applications to external tools, resources, and prompts. AliOS can connect to MCP servers under its permission and audit policies.

### Can AliOS use my existing APIs?

Yes. Register an API as a custom tool, package it as a plugin, or expose it through an MCP server.

### How are tools secured?

Tools declare permissions and schemas. AliOS validates arguments, applies policy before execution, records outcomes, and can require human approval.

### Can an agent run commands on my computer?

Only if you explicitly enable and authorize a tool that permits it. Shell and filesystem access should be tightly sandboxed and scoped.

### Does AliOS support multi-agent systems?

Yes. Agents can have specialized roles, separate tool permissions, scoped memory, and controlled delegation patterns.

### Can agents work asynchronously?

Yes. Workflows and scheduled jobs can run in the background with status, logs, and outcomes recorded by the runtime.

### How does AliOS handle failures?

It captures structured errors, classifies failures, applies retry or fallback policy where appropriate, and allows agents to re-plan or request intervention.

### Is AliOS suitable for production?

AliOS is designed for production-grade architecture, but every production deployment must validate its selected providers, tool permissions, storage, observability, security controls, and operational limits.

### Can I deploy AliOS with Docker?

Yes. Docker and Docker Compose are supported deployment paths for local and server environments.

### Can I build a private internal copilot with AliOS?

Yes. AliOS is well suited to private copilots using local models, self-hosted storage, private retrieval sources, and internal tools.

### How do I add a new model provider?

Implement the model provider interface, register the adapter, add configuration validation, and include integration tests for streaming, structured output, errors, and retries.

### How do I add a plugin?

Create a plugin manifest, declare its capabilities and permissions, implement lifecycle hooks, and install it in a configured plugin location.

### Does AliOS collect my data?

Self-hosted AliOS deployments are controlled by the operator. Data handling also depends on the providers, plugins, storage systems, and integrations you configure.

### Can AliOS schedule tasks?

Yes. The workflow and scheduler layers support interval, cron, event-driven, and queue-driven agent execution.

### How can I contribute?

Open an issue or discussion for ideas, then submit a focused pull request with tests and documentation.

## License

AliOS is licensed under the [Apache License 2.0](LICENSE).

You may use, modify, distribute, and build commercial products with AliOS under the terms of the license. See [LICENSE](LICENSE) for the full text.

## Acknowledgements

AliOS builds on the work of the open-source AI ecosystem and the communities advancing responsible, interoperable agent systems.

Special appreciation goes to the creators and maintainers of:

- Python and TypeScript
- Docker and Kubernetes
- Model Context Protocol
- OpenAI, Anthropic, Google, and open model communities
- Ollama, LM Studio, llama.cpp, vLLM, and Hugging Face
- Vector database and retrieval infrastructure projects
- The researchers, maintainers, testers, and contributors making open AI infrastructure possible

---

AliOS is built on a simple belief: AI should be programmable, inspectable, private when needed, and powerful enough to do meaningful work.

Build agents that do more than talk. Build systems that can think, remember, plan, and act.
