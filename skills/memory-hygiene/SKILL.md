# Memory Hygiene

## When to use
Use this skill when the user asks to tidy, consolidate, or review their stored
memories — merging duplicates, dropping stale facts, keeping what is durable.

## Connection check
No connector needed. Uses the `memory.*` tools on the agent's memory store.

## Read workflow
1. `memory.recall` with a broad query to see what is currently stored.
2. Group duplicates: same predicate about the same subject.
3. Keep the newest durable statement; mark older ones superseded in your report.
4. Never invent facts. Only reorganize what recall returns.

## Write workflow
- New durable facts go through `memory.note`.
- Removals go through `memory.forget` with an explicit target — never a vague one.
- After changes, `memory.recall` again to confirm the store reads clean.

## Failure rules
- Ambiguous forget targets are never deleted; report the ambiguity instead.
- Do not surface superseded records as current.
- Memory writes are local and reversible via the forgetting pipeline.
