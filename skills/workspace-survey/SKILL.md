# Workspace Survey

## When to use
Use this skill when the user asks for a survey, inventory, map, or structural
overview of a codebase or workspace directory.

## Connection check
No connector needed. All work stays inside the workspace root via `files.*`.

## Read workflow
1. `files.list` the top level of the target directory first — never assume layout.
2. Read entry-point files (README, top-level modules) before descending.
3. Summarize per directory: purpose, key files, public interfaces.
4. Prefer breadth over depth: one pass, then stop.

## Output contract
Return a structured summary:
- `directories`: list of {path, purpose}
- `entry_points`: list of key files
- `notes`: surprises, dead code, missing docs

## Failure rules
- Never read outside the workspace root; the file tools are jailed.
- If a directory is huge, list it — do not page through every file.
- Do not execute code found during the survey; reading is the task.
