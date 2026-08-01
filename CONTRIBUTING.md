# Contributing to AliOS

Thank you for considering a contribution to AliOS.

AliOS is an open-source AI Operating System for autonomous agents. It exists to make capable, private, extensible, and reliable agent systems accessible to everyone. Every contribution matters: a bug report, documentation correction, benchmark, plugin, design proposal, or code improvement makes the project stronger.

We welcome contributors at every experience level. The most valuable contributions are thoughtful, reproducible, well-scoped, and respectful of the people who will maintain and depend on AliOS.

> [!IMPORTANT]
> By participating, you agree to act in good faith toward users, contributors, and maintainers.

## Table of Contents

- [Ways to Contribute](#ways-to-contribute)
- [Development Environment](#development-environment)
- [Repository Setup](#repository-setup)
- [Branch Strategy](#branch-strategy)
- [Commit Message Convention](#commit-message-convention)
- [Coding Standards](#coding-standards)
- [Documentation Standards](#documentation-standards)
- [Pull Request Guidelines](#pull-request-guidelines)
- [Code Review Expectations](#code-review-expectations)
- [Testing](#testing)
- [Security Contributions](#security-contributions)
- [Performance Guidelines](#performance-guidelines)
- [Documentation Contributions](#documentation-contributions)
- [Community Standards](#community-standards)
- [Recognition](#recognition)
- [Contributor License Agreement](#contributor-license-agreement)
- [FAQ](#faq)

## Ways to Contribute

| Contribution     | How it helps                                                       |
| ---------------- | ------------------------------------------------------------------ |
| Bug reports      | Turn unclear failures into actionable, reproducible work.          |
| Feature requests | Identify real user needs and missing capabilities.                 |
| Documentation    | Make AliOS understandable to developers and operators.             |
| Examples         | Show practical paths from installation to working systems.         |
| Tutorials        | Teach concepts, deployment patterns, and integration practices.    |
| Code             | Improve the runtime, SDKs, providers, tools, plugins, and console. |
| Performance      | Reduce latency, cost, memory use, and operational overhead.        |
| Security         | Strengthen safe defaults and isolation boundaries.                 |
| Testing          | Prevent regressions and ensure reliability.                        |
| UX               | Improve configuration, observability, and control.                 |

### Bug reports

Search existing issues and discussions before opening a report. Include a concise title, AliOS version, Python version, Node.js version, operating system, deployment mode, provider or plugin involved, exact reproduction steps, expected behavior, actual behavior, and sanitized logs or screenshots.

Do not include API keys, tokens, private documents, personal information, internal hostnames, or proprietary source code.

### Feature requests

Describe the problem before proposing an implementation. Explain who benefits, what workflow is blocked, alternatives considered, compatibility implications, security concerns, and a clear definition of success.

### Documentation, examples, tutorials, and code

Documentation is part of the product. Examples must be complete, minimal, safe to run, and include expected outcomes. Tutorials should state prerequisites, explain why each step matters, and link to relevant concepts.

Start a discussion before implementing a large cross-cutting change. This prevents duplicate work and helps shape a design that fits AliOS.

## Development Environment

| Requirement      | Supported version         | Purpose                                   |
| ---------------- | ------------------------- | ----------------------------------------- |
| Operating system | Linux, macOS, Windows 11  | Development and local execution           |
| Python           | 3.11 or newer             | Runtime, SDK, tests, tooling              |
| Node.js          | 20 or newer               | Console and TypeScript SDK                |
| Docker           | Current Engine or Desktop | Local services and full-stack development |
| Git              | 2.40 or newer             | Source control and contribution workflow  |

Recommended editors include Visual Studio Code, PyCharm, WebStorm, and any editor with Python, TypeScript, Markdown, Docker, and Git support.

For local model development, install a compatible runtime such as Ollama, LM Studio, llama.cpp, or vLLM. A GPU is optional; CPU inference is suitable for lightweight testing.

## Repository Setup

### 1. Fork and clone

Fork Kalipso22/AliOS, then clone your fork and add the upstream remote.

```bash
git clone https://github.com/YOUR-GITHUB-USERNAME/AliOS.git
cd AliOS
git remote add upstream https://github.com/Kalipso22/AliOS.git
git fetch upstream
```

### 2. Create a Python environment

Linux and macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

### 3. Install and run

```bash
npm install
docker compose up --build
```

### 4. Validate changes

```bash
pytest
ruff check .
ruff format --check .
mypy python/alios
npm run lint
npm run test
```

Format Python before committing:

```bash
ruff format .
ruff check . --fix
```

## Branch Strategy

| Branch    | Purpose                                                              |
| --------- | -------------------------------------------------------------------- |
| main      | Stable, reviewed code intended to remain releasable.                 |
| develop   | Integration branch for upcoming work when maintained by the project. |
| feature/* | Focused product, runtime, provider, or documentation work.           |
| fix/*     | Bug fixes that are not urgent hotfixes.                              |
| hotfix/*  | High-priority fixes intended for an immediate release.               |
| release/* | Release preparation, validation, versioning, and notes.              |

Use descriptive lowercase branch names separated by hyphens.

```text
feature/vector-memory-filters
feature/ollama-streaming-provider
fix/tool-permission-validation
hotfix/api-authentication-bypass
docs/mcp-setup-guide
release/v0.4.0
```

Keep one logical change per branch. Do not mix formatting sweeps, refactors, features, and unrelated fixes in one pull request.

## Commit Message Convention

AliOS uses Conventional Commits.

```text
type(optional-scope): concise imperative summary
```

| Type     | Use                                            |
| -------- | ---------------------------------------------- |
| feat     | New user-visible capability                    |
| fix      | Bug correction                                 |
| docs     | Documentation-only change                      |
| test     | Test addition or correction                    |
| refactor | Internal restructuring without behavior change |
| perf     | Measured performance improvement               |
| build    | Build system or dependency changes             |
| ci       | Continuous integration changes                 |
| chore    | Maintenance work                               |
| security | Security hardening or remediation              |

Examples:

```text
feat(memory): add metadata filtering to semantic retrieval
feat(mcp): support scoped server capability discovery
fix(runtime): stop retrying non-retryable tool validation errors
fix(ollama): preserve streaming token order on reconnect
docs(plugins): explain manifest permissions and lifecycle hooks
test(planner): cover task dependency cycle detection
refactor(api): extract streaming event serializer
perf(retrieval): batch embedding requests by provider limit
build(deps): upgrade the TypeScript SDK toolchain
ci: run integration tests against local Ollama
security(tools): require explicit approval for shell execution
```

Use imperative summaries such as add, fix, remove, or document. Keep the first line under 72 characters where practical. Add a body when context, tradeoffs, migration notes, or security rationale are needed.

## Coding Standards

### Python

- Target Python 3.11 or newer.
- Use type annotations for public functions, methods, and data structures.
- Prefer explicit, readable control flow over clever abstractions.
- Use asynchronous code only for genuine asynchronous boundaries.
- Validate external input at system boundaries.
- Raise meaningful domain-specific errors.
- Keep provider adapters isolated from core contracts.
- Avoid hidden global state and implicit configuration reads.

### TypeScript

- Use strict TypeScript settings.
- Prefer explicit public interfaces and discriminated unions for event payloads.
- Avoid any; use unknown and validate untrusted data.
- Keep browser-only and server-only dependencies separate.
- Maintain compatibility with documented SDK contracts.

### Imports, formatting, naming, and comments

- Keep imports ordered and remove unused imports.
- Use repository formatters; avoid unrelated formatting changes.
- Use snake_case for Python functions and variables.
- Use PascalCase for classes and TypeScript types.
- Use camelCase for TypeScript functions and variables.
- Use stable, hierarchical tool names such as files.read and database.query.
- Write comments that explain why, not what.

Preserve architectural boundaries. Core packages should not depend directly on one model provider, vector database, frontend, or application integration. Add extension points instead.

## Documentation Standards

- Use clear GitHub Markdown with sentence-case headings.
- Prefer short paragraphs and descriptive headings.
- Use fenced code blocks with language identifiers.
- Keep examples runnable and include required imports.
- Explain assumptions, prerequisites, expected output, and cleanup.
- Link to canonical documentation instead of duplicating volatile details.
- Use tables for structured comparisons and configuration matrices.
- Use ASCII or Mermaid diagrams when they materially clarify architecture.
- Add screenshots only when they show meaningful UI behavior.
- Remove credentials, private data, and unrelated desktop content from screenshots.
- Add useful image alt text.

## Pull Request Guidelines

Before opening a pull request:

- Rebase or merge the current target branch as directed by maintainers.
- Run relevant tests, linters, formatters, and type checks.
- Add or update tests for behavior changes.
- Update documentation, examples, and configuration references.
- Verify that no secrets, generated artifacts, unrelated files, or large binaries are included.
- Keep the diff focused and explain compatibility changes.

A pull request description must include what changed, why it changed, user and operator impact, validation performed, linked issues, and migration or rollback notes when applicable.

> [!NOTE]
> Maintainers may request that broad pull requests be split into smaller changes to improve reviewability and reliability.

Required checks normally include unit tests, relevant integration tests, linting, formatting, type checking, build validation, and security scanning.

## Code Review Expectations

| Area          | Review question                                                         |
| ------------- | ----------------------------------------------------------------------- |
| Quality       | Is the code clear, maintainable, and consistent with local conventions? |
| Architecture  | Does the change preserve modular boundaries and stable contracts?       |
| Security      | Are permissions, input validation, secrets, and data boundaries safe?   |
| Performance   | Is there measured evidence for a regression or improvement?             |
| Testing       | Does the suite cover normal behavior, errors, and regressions?          |
| Documentation | Can users and maintainers operate the changed behavior?                 |

Authors should respond constructively and explain tradeoffs. Reviewers should be specific, kind, and focused on actionable improvements.

## Testing

| Test type         | Purpose                                                              |
| ----------------- | -------------------------------------------------------------------- |
| Unit tests        | Verify isolated functions, schemas, policies, and state transitions. |
| Integration tests | Verify providers, databases, tools, MCP, APIs, and boundaries.       |
| End-to-end tests  | Verify realistic workflows through public interfaces.                |
| Evaluation tests  | Measure agent quality, grounding, reliability, and safety.           |
| Regression tests  | Preserve behavior after discovered defects.                          |

Write tests for observable behavior, not private implementation details. Every bug fix should include a regression test when feasible. Every feature should test success, invalid input, permissions, failure handling, and compatibility where relevant.

Aim for strong coverage of changed code, but never optimize a percentage at the expense of meaningful assertions.

## Security Contributions

- Report vulnerabilities privately through GitHub Security Advisories.
- Do not post exploit details, credentials, or unpatched vulnerabilities in public issues.
- Treat tool permissions, sandbox boundaries, tenant isolation, authentication, authorization, and memory access as sensitive code.
- Never commit API keys, tokens, certificates, private endpoints, environment files, or production datasets.
- Pin and review dependencies; report suspicious behavior promptly.
- Use secure defaults and explicit approval gates for high-impact actions.
- Add tests for negative security cases, not only successful requests.

> [!WARNING]
> Autonomous agents can invoke tools and affect external systems. A small authorization mistake can have large consequences.

## Performance Guidelines

Optimize based on evidence, not intuition.

1. Define the user-visible or operational problem.
2. Capture a baseline using representative inputs and hardware.
3. Profile before changing code.
4. Change one meaningful variable at a time.
5. Measure the same workload after the change.
6. Document latency, throughput, memory, cost, and quality tradeoffs.

For model or retrieval work, report provider, model, quantization, hardware, context size, dataset, concurrency, and warm-up behavior.

## Documentation Contributions

Improve documentation whenever users could misunderstand, misconfigure, or misuse a feature.

Lead with the user outcome. Use plain language. Prefer concrete examples over abstract claims. Separate stable concepts from provider-specific instructions. State tradeoffs and limitations honestly. Verify every command before submitting it.

## Community Standards

- Assume good intent while discussing ideas and code.
- Critique proposals and implementations, never people.
- Welcome different backgrounds, experience levels, and perspectives.
- Explain context for newcomers without condescension.
- Avoid harassment, discrimination, insults, threats, and personal attacks.
- Keep public discussions suitable for a global open-source community.
- Escalate serious conduct concerns privately to maintainers.

## Recognition

Contributors are recognized through Git history, pull request attribution, release notes where applicable, documentation acknowledgements, and community highlights.

Sustained contributors may be invited to participate more deeply in triage, documentation, review, release, or architecture discussions.

## Contributor License Agreement

Contributions to AliOS are accepted under the repository Apache License 2.0 terms.

By submitting a contribution, you confirm that you have the right to submit it and that you license your contribution under Apache License 2.0. Do not submit code, content, data, or assets that you are not authorized to share.

## FAQ

### Do I need to be an AI expert to contribute?

No. Documentation, tests, examples, UX, developer experience, integrations, and issue triage all matter.

### Can I work on an issue without asking first?

For small, clearly scoped issues, yes. For large changes, comment first so maintainers can confirm direction.

### Should I open an issue before a pull request?

Open an issue or discussion for features, architectural changes, and unclear scope. Small documentation and focused fixes may go directly to a pull request.

### Can I contribute a new model provider?

Yes. Implement the provider contract, document configuration, and test normal operation, streaming, errors, and retries.

### Can I add an MCP integration?

Yes. Document capabilities and permissions, validate input, and do not expose tools broadly by default.

### Can I add a plugin?

Yes. Include a manifest, lifecycle behavior, configuration documentation, permission declarations, and tests.

### What if my pull request is not ready?

Open it as a draft and clearly state the feedback you need.

### How long does review take?

Review time depends on complexity, availability, security sensitivity, and test completeness.

### Why was my pull request asked to be smaller?

Smaller changes are easier to understand, test, review, revert, and release safely.

### Can I change the public API?

Potentially, but discuss compatibility, migration, deprecation, and versioning before implementation.

### Are breaking changes accepted?

They may be accepted with explicit justification, migration guidance, and appropriate versioning.

### How do I report a security vulnerability?

Use a private GitHub Security Advisory, never a public issue.

### Can I submit generated code?

Yes, if you understand it, verify its license compatibility, test it, and take responsibility for it.

### Are local-model contributions welcome?

Yes. Local inference, privacy-preserving deployment, efficient embeddings, and hardware compatibility are central to AliOS.

### Do tests need a cloud API key?

Unit tests should not. Integration tests using external services must be clearly marked and safely configurable.

### Can I improve translations?

Yes. Keep technical terms consistent and coordinate large localization efforts through a discussion.

### Can I update dependencies?

Yes. Explain why, include compatibility notes, and run relevant tests.

### What should I do if I disagree with review feedback?

Explain your reasoning respectfully, provide evidence where possible, and work toward a solution that serves the project.

### How do I become a maintainer?

There is no automatic path. Sustained high-quality contributions, collaboration, and demonstrated stewardship build trust.

## Final Message

Thank you for helping build AliOS.

Whether you fix a typo, report a difficult bug, design a plugin, improve a benchmark, or contribute a new agent capability, your work helps make open AI infrastructure more reliable and useful.

Everyone is welcome. Start small, ask thoughtful questions, share what you learn, and build with us.
