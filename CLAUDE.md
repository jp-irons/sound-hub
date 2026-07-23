# Claude Guidelines — sound-hub

## Confirm before doing

**Always propose and get explicit approval before editing any code files.**

For any task involving code changes:
1. Read the relevant files
2. Describe exactly what you plan to change and why
3. Wait for approval before making edits

This applies to all file modifications: `.js`, `.jsx`, `.py`, `.html`, `.css`, config files, etc.

Exception: purely informational tasks (reading files, answering questions, reviewing plans) need no confirmation.

## Git operations

**Never run git commands that mutate repository state** — `commit`, `push`, `tag`, `add` (as a prelude to committing), `checkout -- <path>`/`reset`/`merge`/`rebase`, or anything else that writes to `.git` or discards working-tree changes. Doing this from the sandbox has repeatedly left stale `index.lock` files that then block Jon's own local git usage.

Read-only git commands (`status`, `log`, `diff`, `show`, `blame`) are fine. For anything that changes repo state — including reverting an edit — either use the `Edit`/`Write` tools to restore file contents directly, or ask Jon to run the git command himself.
