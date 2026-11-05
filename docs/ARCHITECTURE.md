# Architecture and operation

## One owner per conversation

`host.py` owns native Claude subprocesses and their stdin/stdout streams.
`client.py` implements `claudeProcessWrapper`, receiving the official binary path
and launch arguments from VS Code.

For an enabled project, the adapter attaches to a hosted UUID or explicitly
starts/resumes it. The host checks native ownership first. Linux ownership uses
Claude's machine-id/PID namespace identity and process start token. Metadata,
authentication and version probes retain a short-lived native lifecycle.

One VS Code view can attach to a conversation at a time. Native Remote Control
connects phone/web to the same worker. Editor detach keeps the worker alive.
Pending control requests survive detach. Replay filtering avoids duplicating
messages loaded by the official UI, while completion events still reach it so
the running indicator settles.

Adapter detach releases only editor-provided SDK hook callbacks. Native hooks
and permissions remain Claude's responsibility. The host does not automatically
approve tools.

## Storage

The default state root is `/var/tmp/<username>/claude-session-host`.
`CLAUDE_SESSION_HOST_STATE` overrides it when set consistently for host, adapter
and client. The example unit uses the default.

| Path | Purpose |
|---|---|
| `app/` | Installed scripts and separately installed Claude executable |
| `host.sock`, `host.lock` | Private transport and single-host lock |
| `projects.json` | Exact enabled project paths |
| `<UUID>/session.json` | Worker identity, folder, model/effort and remote metadata |
| `<UUID>/events.jsonl` | Stable replay journal |
| `<UUID>/stderr.log` | Native diagnostics |

Native transcripts stay in Claude's configuration directory. Journals and logs
can contain sensitive content. Keep the state root private and outside the Git
checkout. The socket accepts the same UID only; this is a single-user host.

## Lifecycle and NFS

The host starts empty. An explicit create/open starts a worker; reopening a live
hosted conversation reuses it. Existing native processes are never adopted.

The example service allows 32 live workers. Direct `host.py` use defaults to one
unless `--max-workers` is provided. Neither mode prestarts workers. New starts
are refused while a worker is starting, unresponsive, in protocol-error state,
or in uninterruptible I/O wait. One storage reader handles native registry reads;
an NFS stall does not trigger extra readers or replacement workers.

The host checks idle workers once a minute using monotonic elapsed time. It
stops them after 72 hours of complete inactivity and clears in-memory replay.
Messages, generation, tool progress, task changes and completed turns reset the
clock. Pending questions/approvals, queued messages, background work or internal
requests prevent cleanup. Uncertain states and I/O stalls are retained for
inspection. Idle tabs, attach, history browsing, heartbeats and unchanged empty
task snapshots do not reset the interval.

After cleanup, the phone connection is offline until explicit resume. An editor
tab left open through expiry must be closed and reopened. There is no automatic
wake-on-message. Host shutdown stops workers; host startup does not recover them
or replay interrupted prompts. Saved history remains available for resume.

## Commands

```sh
python3 /path/to/app/client.py list
python3 /path/to/app/client.py projects
python3 /path/to/app/client.py create --cwd /absolute/project/path --name 'Project chat'
python3 /path/to/app/client.py create --cwd /absolute/project/path --session UUID
python3 /path/to/app/client.py stop --session UUID
```

`--inherit-current` copies recorded model/effort from the hosted parent identified
by `CLAUDE_CODE_SESSION_ID`. Explicit `--model` and `--effort` override inherited
choices. Without a hosted parent, omitted values follow Claude's defaults.
Global model and permission settings are not changed.

Normal `list` reads the host registry. Saved-history catalog reads are explicit,
serialized and bounded to native transcript edges. The main history interface is
the official VS Code project session manager.

## Source map

- `host.py`: ownership, workers, message routing and idle cleanup.
- `client.py`: native wrapper, commands, replay filtering and detach.
- `skill/cd-claude/SKILL.md`: phone-driven creation in an enabled folder.
- `examples/claude-session-host.service`: user-service template.
- `tests/`: offline protocol fixtures and integration tests.
- `probe.py`, `check_live.py`: opt-in real-runtime developer checks.
- `rollback.py`: recorded-installation setting rollback helper.

Private implementation progress is kept in ignored local notes and is not
needed to install or run the project.
