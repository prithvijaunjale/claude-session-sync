# Install Claude Session Sync

Install on the Linux machine that will run Claude. For Remote-SSH, this is the
remote extension host, not the laptop displaying the editor.

These steps are for a **fresh installation**. Inspect any existing deployment
before replacing files or restarting its service: it may have live workers.
Moving this source checkout does not require moving an installed runtime.

## Requirements

- Linux with `/proc`, Unix sockets and systemd user services.
- Python 3.11+. The host needs no pip or npm packages.
- Official Claude Code extension; tested here with version 2.1.318.
- A Claude login eligible for [Remote Control](https://code.claude.com/docs/en/remote-control#requirements).
  Sign in on the machine with `claude` and `/login`. API keys alone do not enable
  native phone Remote Control.
- A trusted project folder already present on the machine.

Keep the checkout, executable, socket and journals on local disk if home or
projects are on NFS. Existing Claude history/configuration can remain in place.
`/var/tmp` is convenient local storage, not a backup or a guarantee against host
cleanup; retain a separate copy of your source.

## 1. Install the runtime scripts

Open a terminal in the cloned repository and replace both example paths below.
Use an official Claude executable matching your platform and a compatible
version, such as the executable bundled in the tested extension installation.

```sh
CSS_STATE="/var/tmp/$(id -un)/claude-session-host"
CSS_PROJECT="/absolute/path/to/your/project"
CSS_CLAUDE="/absolute/path/to/the/tested/claude/executable"

# Do not overwrite an existing deployment.
test ! -e "$CSS_STATE/app" || exit 1
test -x "$CSS_CLAUDE" || exit 1
test -d "$CSS_PROJECT" || exit 1

install -d -m 700 "$CSS_STATE" "$CSS_STATE/app" "$CSS_STATE/app/runtime"
install -m 700 host.py client.py rollback.py "$CSS_STATE/app/"
cp "$CSS_CLAUDE" "$CSS_STATE/app/runtime/claude"
chmod 700 "$CSS_STATE/app/runtime/claude"

python3 - "$CSS_STATE" "$CSS_PROJECT" <<'PY'
import json, os, sys
from pathlib import Path
state = Path(sys.argv[1])
project = os.path.abspath(sys.argv[2])
path = state / 'projects.json'
path.write_text(json.dumps([project], indent=2) + '\n')
path.chmod(0o600)
PY
```

The local executable copy survives extension-directory cleanup after an update.
Do not commit or redistribute it. The product is called Claude Session Sync;
`claude-session-host` runtime paths and the service name remain for compatibility.

## 2. Start the user service

```sh
install -d -m 700 "$HOME/.config/systemd/user"
install -m 600 examples/claude-session-host.service \
  "$HOME/.config/systemd/user/claude-session-host.service"
systemctl --user daemon-reload
systemctl --user start claude-session-host.service
systemctl --user status claude-session-host.service
```

The example uses systemd's `%u` and `%h` user substitutions. If your home is shared
across hosts, add `ConditionHost=your-hostname` to `[Unit]` before enabling
startup, so another machine does not launch the shared unit.

To opt into startup:

```sh
systemctl --user enable claude-session-host.service
```

Startup at machine boot without interactive login also needs user lingering.
Check `loginctl show-user "$(id -un)" -p Linger`; ask your machine administrator
to enable it if needed. Starting the service launches an empty host. It does not
recover workers or resend prompts. There is no automatic service restart policy.

## 3. Connect the official VS Code extension

In settings for the machine that runs the extension, add:

```json
{
  "claudeCode.claudeProcessWrapper": "/var/tmp/YOUR_USERNAME/claude-session-host/app/client.py"
}
```

Replace `YOUR_USERNAME` with the Linux username. Merge this key into existing
settings, preserve other keys, and record any previous wrapper value. For
Remote-SSH, use remote machine settings. The extension ignores this
machine-scoped option in workspace settings.

Add exact absolute project paths to `projects.json`. Use canonical paths from
`pwd -P`. Subdirectories need their own entries. Other projects pass through to
the native executable.

Start with a new conversation. To migrate an existing chat, close its original
process, then reopen it from official Session History. The host refuses to
start a competing worker while that process remains alive.

## 4. Connect your phone

In the official Claude Code panel, use `/remote-control` or enable Remote
Control for all sessions in its settings. Open the resulting native Claude link
on your phone with the same account and organization.

Send a message from each interface. Close only the conversation's VS Code tab,
continue from the phone, then reopen through official Session History. The
worker should retain its PID across editor detach and reattach.

Use Session History in the intended project window; generic VS Code URL handlers
can open the wrong SSH window.

## 5. Install `/cd-claude`

This optional personal Claude skill creates another hosted conversation in an
enabled folder and returns its phone link. It is usable from an already running
shared conversation on your phone.

```sh
test ! -e "$HOME/.claude/skills/cd-claude" || exit 1
install -d -m 700 "$HOME/.claude/skills/cd-claude"
install -m 600 skill/cd-claude/SKILL.md "$HOME/.claude/skills/cd-claude/SKILL.md"
```

Specify an enabled folder, or let Claude ask you to choose one. If the skill is
not discovered in an open conversation, reopen it. The skill does not enable
new folders by itself.

## Stop, resume, or roll back

```sh
python3 "$CSS_STATE/app/client.py" list
python3 "$CSS_STATE/app/client.py" projects
python3 "$CSS_STATE/app/client.py" stop --session UUID
python3 "$CSS_STATE/app/client.py" create --cwd /absolute/project/path --session UUID
```

Stopping preserves history but takes phone access offline. Reopen the saved
conversation in VS Code or resume through the client. Its old phone identity is
reattached when the native service permits; a failed reconnect is reported.

To return VS Code to native launches, remove only the wrapper key above (or
restore its previous value). Keep the host running until work you want to
preserve finishes. To stop the host, all its workers and startup:

```sh
systemctl --user disable --now claude-session-host.service
```

Remove the personal skill if no longer wanted. Native history is preserved.
Inspect private state before deleting it; it may contain conversation content.
`rollback.py` is a helper for installations with an `editor-setting-backup.json`
record. This manual setup uses the manual setting rollback described above.
