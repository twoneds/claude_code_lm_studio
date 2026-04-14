---
name: local-delegate
description: >
  Orchestration skill that routes tasks to local LLM agents via LM Studio instead of consuming
  Claude cloud tokens. Use this skill whenever a user prompt arrives and you need to decide
  whether to handle it locally or with Claude cloud. Always invoke this skill BEFORE starting
  work on any non-trivial task. It handles: complexity analysis, model routing, multi-agent
  retry logic, escalation to Claude cloud on failure, todo.md task tracking, and claude-mem
  integration for learning from past sessions. TRIGGER on: any coding task, reasoning task,
  multi-step plan execution, or when the user has not explicitly requested Claude cloud.
  Do NOT trigger for: single-turn conversational replies, skill/config management tasks,
  tasks the user has explicitly asked Claude to handle directly.
---

# Local Delegate — Orchestration Skill

Route tasks to local LLM agents running in LM Studio. Preserve Claude cloud tokens for
tasks that genuinely need them.

## Prerequisites Check (run first, every time)

Before routing any task, verify:

```bash
python3 ~/.claude/skills/local-delegate/scripts/delegate.py models
```

If this returns `"error": "LM Studio not reachable"`:
- **Stop.** Tell the user: "LM Studio is not running. Start it and ensure at least one model
  is loaded, then retry. Falling back to Claude cloud for this task."
- Handle the task with Claude cloud and do not attempt further delegation.

If it succeeds, the output gives you:
- `available_models` — live list from LM Studio (cache is also written)
- `routing_table` — each role with its resolved model

Store the routing table in your working context for this session.

---

## Session Initialisation (on first prompt of session)

On the **first user prompt** of a session (not on SessionStart hook — only after prompt analysis):

1. Run the models command above to refresh the routing table.
2. Search claude-mem for relevant past work using the `mem-search` skill:
   - Query for the current project name and task type
   - Look for past routing decisions, failures, and escalations
   - Note any patterns: e.g. "last 3 times we routed auth tasks to fast_code, 2 failed"
3. Load that context into your routing decisions for this session.

This makes routing smarter over time — past failures inform current model selection.

---

## Routing Logic

When a task arrives, run the full pipeline:

```bash
python3 ~/.claude/skills/local-delegate/scripts/delegate.py run '<task_json>'
```

Where `task_json` is:
```json
{
  "prompt": "<the full task prompt>",
  "type": "code|reasoning|classify|general",
  "max_retries": 2
}
```

The script handles:
- Triage model scores complexity 1–5
- Complexity + type → role → model selection
- Up to `max_retries + 1` attempts with 1s back-off
- Returns `escalate: true` if all attempts fail or no model is available

### When NOT to delegate locally

Always keep these with Claude cloud:
- Novel architectural decisions with no clear spec
- Tasks explicitly involving multiple conflicting outputs that need resolution
- Requests that reference ambiguity across the full codebase
- Any task the user has prefixed with `!cloud` or similar explicit tag

### User explicit routing tags

| Tag | Behaviour |
|-----|-----------|
| `!local` | Force local regardless of complexity |
| `!cloud` | Force Claude cloud, skip delegation |
| `!fast` | Force `fast_code` or `fast_reasoning` role |
| `!coder` | Force `code` role (qwen3-coder-next / qwen3-coder-30b) |

---

## Multi-Agent Task Decomposition

For tasks that benefit from parallelism (detected when the prompt contains multiple
independent sub-tasks or the complexity score is ≥ 4):

1. Break the prompt into discrete sub-tasks
2. Route each sub-task independently (different roles/models as appropriate)
3. Run sub-tasks via the `run` command, collecting all results
4. If results are **inconsistent** (e.g. multiple valid but conflicting implementations):
   - Pass all variants to Claude cloud with: "These local agents returned conflicting
     outputs. Resolve and return the canonical implementation."
5. If any sub-task fails > 2 times, escalate that sub-task to Claude cloud and continue
   the rest locally.

---

## Retry and Escalation Protocol

The delegate script handles per-call retries. At the orchestration level:

```
attempt 1-3 → local model (delegate.py handles internally)
if escalate=true:
  → assign task to Claude cloud
  → update todo.md (see below)
  → note the failure pattern in session context
```

When escalating, tell the user clearly:
> "Local delegation failed after N attempts (model: X, role: Y). Handling with Claude cloud."

---

## todo.md Integration

todo.md lives in the **active project directory**. Update it when:

- A task is escalated to Claude cloud (mark with `[escalated]` tag and reason)
- A task completes successfully via local delegation (mark with `[local]` and model used)
- A multi-agent task resolves a conflict via cloud (mark with `[resolved-cloud]`)

Format:
```markdown
## Task Log
- [local] qwen3-coder-next: Implement auth middleware (complexity 4)
- [escalated] fast_code→cloud: Parse nested JSON schema — 3 attempts failed (malformed output)
- [resolved-cloud] Conflicting implementations for route handler — resolved by Claude
```

This file is the audit trail. It is not a todo list — it is a routing decision log.

---

## claude-mem Integration

claude-mem runs as a background service (port 37777) and hooks into every tool call and
session boundary automatically. Your job is to use its search capability to inform routing.

### At session start
Use the `mem-search` skill to query:
```
search(query="local delegation failure", project="<current project>", limit=10)
```
Look for patterns in past escalations — if a certain task type consistently fails locally,
pre-route it to a higher tier or cloud.

### After escalation
Use the `do` skill's observation pattern or note in your context: what failed, what model,
what complexity score. claude-mem will capture this automatically via its PostToolUse hook.

### Querying past routing decisions
If the user asks "how did we handle X last time?" or you detect a similar past task:
```
search(query="<task keywords>", project="<project>", types="decision,bugfix")
```
Use the result to pre-select a model that worked, or avoid one that failed.

---

## Output Contract

Always return results to the user in full — do not truncate local model output.
If the response from a local model contains code, validate it (run it, test it) before
presenting as complete. If validation fails, that counts as a failed attempt — retry or escalate.

---

## Script Reference

All commands are in `~/.claude/skills/local-delegate/scripts/delegate.py`:

| Command | Purpose |
|---------|---------|
| `models` | Refresh model list, print routing table |
| `route '<task_json>'` | Analyse task, return routing decision (no call) |
| `call <model_id> '<prompt>'` | Call a specific model directly |
| `run '<task_json>'` | Full pipeline: route + call + retry |

The model cache is written to `~/.claude/skills/local-delegate/model_cache.json` and
updated every time `models` or `run` is called.
