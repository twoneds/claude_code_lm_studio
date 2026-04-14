# claude_code_lm_studio

A Claude Code skill that routes tasks to local LLM agents via LM Studio, preserving Claude cloud tokens for tasks that genuinely need them.

## What it does

When a prompt arrives, this skill:

1. Checks LM Studio health — errors clearly if the service is not running
2. Refreshes the live model list from LM Studio on session start, updating the routing table dynamically
3. Analyses task complexity (1–5) using a lightweight triage model
4. Routes to the right local model based on complexity and task type
5. Retries up to 2x on failure, then escalates to Claude cloud with a structured handoff
6. Updates `todo.md` in the active project with routing decisions and escalation reasons
7. Integrates with claude-mem to learn from past session routing patterns

## Hardware target

- MacBook Pro Apple Silicon (M-series), 48–64 GB unified RAM
- LM Studio running locally at `http://localhost:1234/v1`

## Routing table

| Role | Candidates | Use case |
| --- | --- | --- |
| triage | qwen3-4b, qwen3-1.7b | Complexity scoring |
| classify | qwen3-4b | JSON extraction, instruction following |
| fast_code | llama3.1-8b, qwen3-0.6b-coders | Simple code sub-tasks |
| fast_reasoning | qwen3-4b-thinking, qwen3-4b | Reasoning, math |
| code | qwen3-coder-next, qwen3-coder-30b | Complex multi-file code |
| reasoning | qwen3-30b-a3b, qwen3-coder-30b | Novel reasoning, architecture |
| general | gemma-3-12b, qwen3-30b-a3b | General tasks |

The routing table is resolved at runtime against the live model list. If a preferred model is not loaded, the next candidate is used automatically.

## Installation

1. Copy this repo into your Claude Code skills directory:

```bash
cp -r . ~/.claude/skills/local-delegate/
```

2. Ensure LM Studio is running with at least one model loaded.

3. Optionally install [claude-mem](https://github.com/thedotmack/claude-mem) for cross-session routing memory.

## Usage

The skill triggers automatically on non-trivial tasks. You can also force routing with these tags:

| Tag | Behaviour |
| --- | --- |
| !local | Force local model regardless of complexity |
| !cloud | Force Claude cloud, skip delegation |
| !fast | Force fast_code or fast_reasoning role |
| !coder | Force code role (qwen3-coder-next / qwen3-coder-30b) |

## Script

All routing logic is in `scripts/delegate.py` — a self-contained Python 3 script with no external dependencies.

```bash
# Refresh model list and print routing table
python3 scripts/delegate.py models

# Analyse a task without calling a model
python3 scripts/delegate.py route '{"prompt": "refactor auth module", "type": "code"}'

# Full pipeline: route + call + retry
python3 scripts/delegate.py run '{"prompt": "write a parser", "type": "code", "max_retries": 2}'
```

## Tests

```bash
# Unit tests (no LM Studio required)
python3 tests/test_delegate.py unit

# Live integration tests (requires LM Studio)
python3 tests/test_delegate.py live

# All
python3 tests/test_delegate.py all
```

32 unit tests and 8 live integration tests.

## Requirements

- Python 3.9+
- LM Studio running at `localhost:1234`
- No pip dependencies — stdlib only
