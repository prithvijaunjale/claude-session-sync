---
name: cd-claude
description: Start another persistent Claude Code conversation in a chosen project and return its official phone link. Use for requests to create a shared or remote conversation, not ordinary coding in the current chat.
---

Use the existing local session host at:

```sh
CSS_CLIENT="${CLAUDE_SESSION_HOST_STATE:-/var/tmp/$(id -un)/claude-session-host}/app/client.py"
```

Resolve this path on the machine running the conversation and use it as
`<client-path>` below.

When the user gives a directory, use that directory. Otherwise run `python3 <client-path> projects` and let the user choose a directory with `AskUserQuestion`, or an ordinary question if that tool is unavailable. Accept a different explicit path too; don't change the current conversation's working directory.

Create exactly one requested conversation:

```sh
python3 <client-path> create --cwd '/chosen/project' --name 'Project name' --inherit-current
```

`--inherit-current` copies the hosted parent conversation's recorded model and effort when available. Explicit user choices belong in `--model MODEL --effort LEVEL` and take precedence. For a parent outside the host, omitted choices follow existing Claude defaults; don't claim they were inherited. Never change global model or permission settings for this operation.

Return the resulting `remote_url` as a clickable link. That is the new conversation in the official Claude phone app or web interface; it also belongs in the official Claude Code session history for its project in VS Code. Leave the parent conversation running.

Only folders enabled in the host's local project list can be created. If a chosen folder is not enabled, report that restriction rather than editing the allowlist yourself. Workers stop after three days of complete inactivity; saved history remains, but the phone connection goes offline until the conversation is resumed from the machine or VS Code. Do not promise that an offline phone message wakes a stopped worker.

If the host reports an I/O stall, ownership conflict, or capacity limit, report it and leave existing workers and services alone. Do not start a separate Claude server as a fallback.
