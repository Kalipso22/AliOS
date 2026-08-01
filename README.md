# AliOS

### Open-Source AI Operating System for Autonomous Agents

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5%2B-3178C6?logo=typescript&logoColor=white)](https://typescriptlang.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://docker.com)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-4B5563)](#installation)
[![Open Source](https://img.shields.io/badge/Open%20Source-Yes-3DA639)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active%20Development-brightgreen)](#roadmap)

AliOS is a modular AI Operating System for building autonomous, secure, and extensible AI agents. It unifies reasoning, memory, planning, tool use, collaboration, local inference, and cloud language models.

> [!IMPORTANT]
> AliOS is not another chatbot framework. It is operating infrastructure for AI: a composable foundation for agents that understand goals, retain context, coordinate work, execute approved actions, and improve through feedback.

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

AI is moving from question answering to action taking. A model alone does not persist state safely, schedule work, control tools, manage permissions, recover from failures, or coordinate specialists.

AliOS supplies those missing capabilities through a provider-neutral architecture. Build assistants, coding agents, research systems, business workflows, local private copilots, and multi-agent applications without locking your product to one model vendor or deployment environment.

An autonomous system needs durable memory, a planning loop, controlled execution, observability, policy enforcement, model routing, failure recovery, and a way to connect to existing tools. AliOS makes that architecture reusable and inspectable.

## Philosophy

### Local First

Run where your data lives. AliOS supports local models, local embeddings, local storage, and self-hosted deployments.

### Privacy First

Sensitive prompts, documents, credentials, and traces stay under your control through scoped secrets, auditable tools, and flexible deployment.

### Modular

Models, agents, memory stores, vector databases, tools, planners, transports, and interfaces are replaceable behind stable contracts.

### Extensible

Plugins, provider adapters, custom tools, workflows, and MCP servers make new capability additive rather than invasive.

### Open Source

Infrastructure that decides, remembers, and acts on behalf of people should be inspectable, auditable, and adaptable.

### Developer Friendly

AliOS provides Python and TypeScript entry points, a CLI, typed APIs, structured logs, clear configuration, and predictable deployment paths.

## Core Features

| Capability | Description |
|---|---|
| Multi-agent architecture | Coordinate specialized agents with roles, shared context, delegation, and boundaries. |
| Memory system | Persist context, facts, documents, preferences, and execution outcomes. |
| Planning | Convert high-level goals into explicit, revisable task plans with checkpoints. |
| Tool calling | Register tools with schemas, validation, permission scopes, and traces. |
| Local LLM support | Run private workloads with Ollama, LM Studio, llama.cpp, vLLM, or Transformers. |
| Cloud LLM support | Connect hosted providers through one consistent model interface. |
| MCP and plugins | Add structured external capabilities without forking core. |
| RAG and vector memory | Ground responses in documents and retrieve semantically relevant memories. |
| Workflows and scheduling | Compose automations and trigger them on intervals, cron, events, or queues. |
| Autonomous execution | Run goal-to-result loops with budgets, approval gates, retries, and stop conditions. |
| API, GUI, and CLI | Operate AliOS through typed APIs, a browser console, or terminal. |
| Observability and logging | Capture traces, usage, latency, decisions, tool outcomes, and failures. |

## Architecture Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│ User Interfaces: Web GUI · CLI · Python SDK · TypeScript SDK         │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│ API: Authentication · Sessions · Streaming · Webhooks · Jobs         │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│ Runtime: Agent Orchestrator · Workflows · Scheduler · Policy Engine  │
└───────┬──────────────┬──────────────┬──────────────┬────────────────┘
        │              │              │              │
┌───────▼──────┐ ┌─────▼──────┐ ┌─────▼───────┐ ┌────▼───────────────┐
│ Planner      │ │ Memory      │ │ Tools       │ │ Model Router       │
│ Goals/Tasks  │ │ Context/RAG │ │ MCP/Plugins │ │ Local/Cloud Models │
└──────────────┴─┴─────────────┴─┴─────────────┴─┴────────────────────┘
```

| Layer | Responsibility |
|---|---|
| Core | Shared types, configuration, lifecycle, events, errors, and extension contracts. |
| Runtime | Loads memory, plans, routes models, executes tools, applies policy, and records traces. |
| Providers | Adapt LLMs, embeddings, vector DBs, SQL, queues, secrets, and observability. |
| Memory | Stores sessions, durable facts, documents, embeddings, and run history. |
| Planner | Turns goals into task graphs, tracks dependencies, and re-plans. |
| Agents | Role-aware entities with instructions, models, tools, memory rules, and limits. |
| Tools | Controlled capabilities for files, search, databases, code, messaging, and services. |
| Models | Explicit selection, fallbacks, policy routing, and local-first inference. |
| API and frontend | Typed client access and an operational console for status and audit. |
| Storage | Pluggable metadata, conversation, document, vector, artifact, and audit backends. |
| Plugins and MCP | Optional capability packages and policy-controlled external integrations. |

## Supported AI Providers

| Provider | Chat / Reasoning | Embeddings | Typical Use |
|---|---:|---:|---|
| OpenAI | Yes | Yes | General reasoning, structured output, multimodal workloads |
| Anthropic | Yes | Via adapter | Long-context analysis and tool use |
| Google | Yes | Yes | Gemini-based multimodal workflows |
| OpenRouter | Yes | Provider-dependent | Unified hosted-model access |
| Groq | Yes | Via adapter | Low-latency hosted inference |
| DeepSeek | Yes | Via adapter | Reasoning and coding workloads |
| Ollama | Yes | Yes | Local development and private inference |
| LM Studio | Yes | Yes | Desktop-hosted model serving |
| vLLM | Yes | Via adapter | High-throughput self-hosted serving |
| llama.cpp | Yes | Via adapter | Lightweight local and edge inference |

| Routing Mode | Behavior |
|---|---|
| Explicit | Every run uses the named provider and model. |
| Fallback | A compatible secondary provider is used after qualifying failure. |
| Policy-based | Route by sensitivity, latency, budget, capability, or region. |
| Hybrid | Keep retrieval local while using an approved cloud model for generation. |

## Local AI Support

### GGUF

GGUF models run through llama.cpp-compatible runtimes and suit desktop, edge, and offline deployments.

### Transformers

Use Transformers-compatible stacks for custom models, fine-tuned models, specialized embeddings, and research workflows.

### CUDA, ROCm, CPU, and Quantization

Use CUDA-capable runtimes for NVIDIA acceleration or ROCm-capable runtimes for supported AMD deployment. CPU-only inference supports lightweight assistants and private edge workloads. Quantization reduces memory needs; select a format supported by your runtime and hardware.

> [!WARNING]
> Local model quality, context length, speed, and tool-use reliability vary by model and hardware. Test production workflows with the exact deployment configuration.

## Agent System

```text
Goal → Understand → Retrieve Memory → Plan → Act → Observe → Reflect
                                           │                   │
                                           └── tools/models ───┘
```

AliOS agents decompose work into verifiable tasks, retain explicit plans with dependencies and budgets, retrieve scoped memory, choose models and tools, validate output, and self-correct.

Reasoning workflows include plan-and-execute, critique-and-revise, retrieval-first, tool-first, and specialist delegation. Retry policy distinguishes transient provider errors from validation, tool, and semantic failures. Reflection can change parameters, tools, models, plans, or request human intervention.

## Memory System

| Memory Type | Purpose | Typical Lifetime |
|---|---|---|
| Short-term | Conversation, active task state, recent tool output | One run or session |
| Long-term | Facts, preferences, decisions, and learned outcomes | Persistent |
| Semantic | Meaning-based records retrieved by similarity | Persistent |
| Episodic | Past actions, failures, and outcomes | Policy-limited |
| Working | Planner state, task graph, intermediate artifacts | Active workflow |

Short-term memory is token-budgeted and may be summarized. Long-term memory retains policy-eligible knowledge. Semantic memory stores records with vector embeddings, allowing relevant retrieval even when wording differs.

AliOS supports interchangeable vector backends and separate embedding providers. Retrieval combines similarity with metadata filters, recency, source trust, access control, and token limits.

## Tool System

```text
Agent Decision → Tool Registry → Schema Validation → Permission Check
                                                        │
                                                        ▼
                                               Tool Execution
                                                        │
                                                        ▼
                                         Result Normalization + Trace
```

```python
from alios import AliOS, tool

app = AliOS()

@tool(
    name="weather.lookup",
    description="Return current weather for a city.",
    permissions=["network:weather"],
)
async def get_weather(city: str) -> dict:
    return {"city": city, "condition": "clear", "temperature_c": 24}

app.tools.register(get_weather)
```

Tools declare typed schemas, output contracts, permissions, and handlers. AliOS validates arguments before execution and normalizes every result into structured records. Permissions can be scoped by agent, user, tenant, environment, credential, or action.

> [!WARNING]
> Never grant unrestricted shell, filesystem, database, financial, messaging, or production-admin access to an autonomous agent. Apply least privilege, allowlists, sandboxing, approval gates, and audit logging.

## Plugin System

Plugins contribute providers, tools, MCP integrations, agents, workflow templates, API routes, CLI commands, GUI panels, and domain-specific policies.

1. Discovery locates installed packages.
2. Manifest validation checks compatibility and declared permissions.
3. Loading imports the package in a controlled context.
4. Registration adds capabilities to runtime registries.
5. Activation starts optional services and hooks.
6. Shutdown releases resources cleanly.

The CLI and GUI can list installed plugins, their capabilities, requirements, permissions, and active state.

## MCP Support

The Model Context Protocol (MCP) is an open standard for connecting AI applications to external tools, resources, and prompts.

```text
AliOS Agent → MCP Client + Policy Layer
                 ├── Filesystem MCP Server
                 ├── GitHub MCP Server
                 ├── Database MCP Server
                 ├── Browser MCP Server
                 └── Internal Company MCP Server
```

AliOS exposes MCP capabilities only to authorized agents. Policies can restrict servers, tools, argument values, credentials, and approval requirements.

## Installation

### Requirements

- Python 3.11 or newer
- Node.js 20 or newer for the console and TypeScript SDK
- Docker Desktop or Docker Engine for containers
- An AI provider credential or compatible local model server

### Linux and macOS

```bash
git clone https://github.com/Kalipso22/AliOS.git
cd AliOS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
alios doctor
```

### Windows PowerShell

```powershell
git clone https://github.com/Kalipso22/AliOS.git
Set-Location AliOS
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[all]"
alios doctor
```

### Docker

```bash
git clone https://github.com/Kalipso22/AliOS.git
cd AliOS
docker compose up --build
```

### Python and Node.js

```bash
python -m pip install alios
npm install @alios/sdk
```

Configure OpenAI:

```bash
export ALIOS_MODEL_PROVIDER=openai
export OPENAI_API_KEY="your-api-key"
export ALIOS_MODEL_NAME="gpt-4.1-mini"
```

Configure local Ollama:

```bash
export ALIOS_MODEL_PROVIDER=ollama
export ALIOS_MODEL_NAME="llama3.2"
export ALIOS_OLLAMA_BASE_URL="http://localhost:11434"
```

> [!NOTE]
> Keep credentials in environment variables or a managed secret store. Never commit API keys or production connection strings.

## Quick Start

```python
import asyncio
from alios import Agent, AliOS

app = AliOS(model={
    "provider": "ollama",
    "name": "llama3.2",
    "base_url": "http://localhost:11434",
})

researcher = Agent(
    name="researcher",
    instructions="Research the question, cite evidence, and state uncertainty clearly.",
    tools=["web.search"],
)

async def main():
    result = await app.run(
        agent=researcher,
        goal="Compare benefits and risks of local AI deployment.",
    )
    print(result.output)

asyncio.run(main())
```

```bash
alios serve --host 0.0.0.0 --port 8000
```

```ts
import { AliOSClient } from "@alios/sdk";

const client = new AliOSClient({ baseUrl: "http://localhost:8000" });
const run = await client.runs.create({
  agent: "researcher",
  goal: "Summarize the latest project activity.",
});

for await (const event of client.runs.stream(run.id)) {
  console.log(event.type, event.data);
}
```

## Project Structure

```text
AliOS/
├── apps/
│   ├── api/                    # API service and transports
│   ├── cli/                    # Command-line interface
│   └── console/                # Web operations console
├── packages/
│   ├── core/                   # Contracts, events, configuration
│   ├── runtime/                # Agent execution and orchestration
│   ├── agents/                 # Definitions and collaboration
│   ├── planner/                # Planning and reflection
│   ├── memory/                 # Context, RAG, retrieval, vectors
│   ├── tools/                  # Registry, policies, execution
│   ├── models/                 # Routing and provider adapters
│   ├── workflows/              # Workflow and scheduler engine
│   ├── plugins/                # Plugin lifecycle
│   ├── mcp/                    # MCP integration
│   └── sdk-typescript/         # TypeScript SDK
├── python/alios/               # Python SDK and runtime
├── plugins/                    # Official optional plugins
├── examples/                   # Runnable examples
├── tests/                      # Unit, integration, evaluation tests
├── docs/                       # Documentation
├── docker/                     # Containers and deployment assets
├── pyproject.toml
├── package.json
├── docker-compose.yml
├── LICENSE
└── README.md
```

## Development Guide

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
npm install
pytest
ruff check .
mypy python/alios
npm run lint
npm run test
```

- Keep core interfaces provider-neutral.
- Test new execution paths and policy behavior.
- Prefer structured events to unstructured logs.
- Preserve compatibility for public SDK contracts.
- Treat tool permissions and tenant boundaries as security-sensitive.
- Document every configuration key and extension point.

## Roadmap

### Core

- [x] Provider-neutral model abstraction
- [x] Agent runtime and tool registry
- [x] Memory and retrieval primitives
- [ ] Durable workflow state and resumable runs
- [ ] Policy-as-code engine
- [ ] Evaluation and regression framework

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

- [ ] Streaming speech-to-text
- [ ] Text-to-speech provider abstraction
- [ ] Real-time voice agent runtime

### Vision

- [ ] Image and document understanding
- [ ] Screen-aware desktop automation
- [ ] Visual retrieval pipelines

### Agents

- [ ] Agent teams with shared task boards
- [ ] Long-running background agents
- [ ] Human-in-the-loop review queues
- [ ] Reusable skill libraries and profiles

### Marketplace

- [ ] Signed plugin distribution
- [ ] Plugin compatibility and security metadata
- [ ] Community workflow templates

### Cloud

- [ ] Managed control plane
- [ ] Team workspaces and organization policies
- [ ] Enterprise identity integrations
- [ ] Hosted observability dashboards

## Security

AliOS makes secure agent behavior possible; secure deployment still requires deliberate configuration.

- Use least-privilege tool permissions.
- Store credentials in a secret manager or environment variables.
- Isolate untrusted code in dedicated sandboxes.
- Require approval for destructive, financial, messaging, and production actions.
- Scope memory by tenant, user, and data classification.
- Encrypt sensitive data in transit and at rest.
- Audit model calls, tool invocations, policy decisions, and approvals.
- Pin provider, plugin, and container versions in production.
- Review third-party MCP servers and plugins before enabling them.
- Set time, token, cost, retry, and recursion limits.

## Performance Goals

| Area | Goal |
|---|---|
| Runtime overhead | Keep orchestration overhead small relative to model and tool latency. |
| Streaming | Deliver model and agent events as they are produced. |
| Tool execution | Support concurrent, policy-controlled independent calls. |
| Retrieval | Keep common vector retrieval low latency with metadata filters. |
| Scalability | Run locally for one user or horizontally across workers. |
| Reliability | Resume durable workloads after transient failures. |
| Cost control | Track usage and enforce per-run or tenant budgets. |
| Observability | Trace each significant action by run and correlation ID. |

## Benchmarks

Agent benchmarks must include the model, provider, prompts, tools, hardware, dataset, evaluation criteria, and run budget.

| Dimension | What is measured |
|---|---|
| Task success | Correctness and completeness |
| Tool reliability | Successful calls, failures, retries, and recovery |
| Grounding | Evidence support for outputs |
| Latency | End-to-end and per-stage timing |
| Cost | Token, model, and tool cost |
| Safety | Blocked policy violations and approvals |
| Stability | Variance across repeated controlled runs |
| Local efficiency | Memory, throughput, and latency on local hardware |

Never compare benchmark results without their complete configuration.

## Contributing

Contributions are welcome across runtime engineering, providers, local inference, documentation, testing, design, examples, plugins, and community support.

1. Fork the repository.
2. Create a focused branch.
3. Make the change with tests and documentation.
4. Run relevant checks.
5. Open a pull request explaining the problem, approach, and validation.

Good first contributions include provider adapters, MCP integrations, quick-start improvements, evaluation fixtures, tests, and documentation.

## Community

AliOS is built for developers, researchers, builders, and organizations working toward open, capable, and trustworthy AI systems.

- GitHub Issues: reproducible bugs and feature requests.
- GitHub Discussions: questions, ideas, and architecture conversations.
- Pull Requests: code, documentation, tests, and integrations.
- Community plugins: reusable tools and domain capabilities.

Be respectful, assume good intent, protect privacy, and help make the project welcoming to contributors at every level.

## FAQ

### What is AliOS?

An open-source operating system layer for autonomous AI agents, providing planning, memory, tools, workflows, model routing, integrations, and observability.

### Is AliOS a chatbot?

No. A chat interface can use AliOS, but AliOS is the system beneath agents that reason, remember, plan, and act.

### Can I use AliOS without a cloud API?

Yes. AliOS supports compatible local providers such as Ollama, LM Studio, llama.cpp, vLLM, and Transformers deployments.

### Does AliOS require a GPU?

No. CPU inference is supported, though a GPU can improve latency and throughput.

### Which languages does AliOS support?

Python and TypeScript, plus language-neutral service integration.

### Can I use OpenAI and local models together?

Yes. Use explicit, fallback, policy-based, or hybrid routing.

### How does AliOS store memory?

Through configurable local files, relational databases, vector databases, and self-hosted or managed services.

### Can users control remembered data?

Yes. Retention, scope, source trust, consent, and deletion are policy-controlled.

### What is RAG in AliOS?

RAG retrieves relevant documents or memory before generation to ground answers in available knowledge.

### What is MCP?

MCP is a standard for external AI tools, resources, and prompts. AliOS connects to MCP servers under permission and audit policy.

### Can AliOS use existing APIs?

Yes. Register an API as a custom tool, package it as a plugin, or expose it through MCP.

### How are tools secured?

Tools declare schemas and permissions. AliOS validates arguments, applies policy, logs outcomes, and can require approval.

### Can an agent run commands on my computer?

Only if you explicitly enable and authorize a suitably sandboxed shell or filesystem tool.

### Does AliOS support multi-agent systems?

Yes. Agents can have specialized roles, scoped memory, separate permissions, and controlled delegation.

### Can agents work asynchronously?

Yes. Scheduled jobs and workflows can run in the background with recorded status and logs.

### How are failures handled?

AliOS captures structured errors, classifies failures, applies retry or fallback policy, and allows re-planning or intervention.

### Is AliOS suitable for production?

It is designed for production-grade architecture, but each deployment must validate providers, permissions, storage, observability, security, and limits.

### Can I deploy with Docker?

Yes. Docker and Docker Compose are supported deployment paths.

### Can I build a private internal copilot?

Yes. Use local models, self-hosted storage, private retrieval sources, and internal tools.

### How do I add a model provider?

Implement the provider interface, register the adapter, validate configuration, and test streaming, structured output, errors, and retries.

### How do I add a plugin?

Create a manifest, declare capabilities and permissions, implement lifecycle hooks, and install it in a configured location.

### Does AliOS collect my data?

Self-hosted deployments are controlled by their operator. Data handling also depends on configured providers, plugins, storage systems, and integrations.

### Can AliOS schedule tasks?

Yes. Workflows and schedulers can support interval, cron, event-driven, and queue-driven execution.

### How can I contribute?

Start an issue or discussion for ideas, then submit a focused pull request with tests and documentation.

## License

AliOS is licensed under the [Apache License 2.0](LICENSE).

## Acknowledgements

AliOS builds on the open-source AI ecosystem and communities advancing responsible, interoperable agent systems.

Special appreciation goes to the creators and maintainers of Python, TypeScript, Docker, Kubernetes, Model Context Protocol, OpenAI, Anthropic, Google, Ollama, LM Studio, llama.cpp, vLLM, Hugging Face, vector databases, and the broader research and maintainer community.

---

AliOS is built on a simple belief: AI should be programmable, inspectable, private when needed, and powerful enough to do meaningful work.

Build agents that do more than talk. Build systems that can think, remember, plan, and act.

