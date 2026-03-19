# Memory System

Servclaw keeps two types of memory, both stored as plain Markdown files on the host and mounted into the container.

## Files

| File | Purpose |
|------|---------|
| `memory/memory.md` | Long-term facts: user preferences, infrastructure notes, key details |
| `memory/session.md` | Current session summary: what happened recently, context for restarts |

## How It Works

- Every new message is checked for facts worth remembering (names, preferences, infra details)
- After long conversations, the session is summarized and written to `session.md`
- On restart, the agent loads both files and picks up where it left off
- You can edit these files directly — they're just Markdown

## Manual Edits

Since they're plain files, you can:
- Clear `session.md` to reset session context without losing long-term memory
- Edit `memory.md` to correct or add facts
- Delete both to give the agent a completely fresh start
