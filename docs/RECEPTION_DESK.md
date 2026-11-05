# Optional permanent phone reception desk

The shared host stops project workers after 72 hours of complete inactivity.
Saved history remains, but a stopped worker cannot receive phone messages or
run `/cd-claude`. If every project worker has stopped, use this optional native
Remote Control server as a separate entry point. It can create a reception
conversation from your phone, then `/cd-claude` creates the actual project chat
through the shared host.

## Behavior

- One systemd user service runs native `claude remote-control` in a dedicated
  folder, with `--spawn same-dir --capacity 1`. This limits reception chats only;
  the shared host's project-worker limit is unchanged.
- The reception desk is outside the shared host's project allowlist and its
  72-hour cleanup. Native Remote Control manages its conversation lifecycle;
  keep the server running to create another reception chat when needed.
- The native server is separate from the shared host. Its helper chat is not a
  shared VS Code worker; the project chats returned by `/cd-claude` are.
- `Restart=always` restarts this launcher after it exits. It does not change
  the shared host's restart policy, restore project workers, or replay prompts.
- It uses the existing native Claude login and installed personal skill.
  Tool permissions remain enabled. A dedicated folder avoids loading your
  research project's configuration; it is not filesystem isolation.
- This adds a persistent native server and up to one native helper conversation,
  with their memory cost and normal Claude usage when you send requests.

## Install

Complete [the main installation](SETUP.md), including the personal `/cd-claude`
skill. The example uses the existing runtime executable and default state paths.
Adjust both services if your installation uses different paths.

First create the folder on local disk. Do not overwrite an existing setup:

```sh
CSS_DESK="/var/tmp/$(id -un)/claude-reception-desk"
test ! -e "$CSS_DESK" || exit 1
install -d -m 700 "$CSS_DESK"
install -m 600 examples/reception-desk/CLAUDE.md "$CSS_DESK/CLAUDE.md"
```

Keep this folder out of `projects.json`, including wildcard configurations.
From a real terminal, run the server command shown in the service from the new
folder once. Accept workspace trust and Remote Control's one-time consent if
asked, then stop that foreground server before starting the service. No model
prompt is needed.

The launcher uses existing Claude model/effort defaults. To choose its defaults,
use the normal project-local `.claude/settings.local.json` in this folder. Model
choices in an unhosted reception chat are not inherited by `--inherit-current`;
request explicit model/effort when creating a project chat if desired. You may
record your preferred creation defaults in the local reception `CLAUDE.md`.
No global model settings need to change.

Install the service:

```sh
install -d -m 700 "$HOME/.config/systemd/user"
test ! -e "$HOME/.config/systemd/user/claude-reception-desk.service" || exit 1
install -m 600 examples/claude-reception-desk.service \
  "$HOME/.config/systemd/user/claude-reception-desk.service"
```

If your home is shared between hosts, add `ConditionHost=your-hostname` under
`[Unit]` in the installed unit before enabling it. Then run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now claude-reception-desk.service
systemctl --user status claude-reception-desk.service
journalctl --user -u claude-reception-desk.service -n 40 --no-pager
```

Open the native Claude link printed in the private service log, or select
**Claude Reception Desk** in the phone's Remote Control environments. Do not
publish the live link or log. Boot startup without a login needs the same user
lingering setup as the main service; `/var/tmp` can be cleaned by host policy,
so retain the source templates elsewhere.

## Use and verify

1. Open or start the reception chat on your phone.
2. Ask `/cd-claude` to create a conversation in an enabled project.
3. Approve its command if prompted, open the returned link, and send a message.
4. In VS Code on the same host, open that project and find the new chat in the
   official Claude Code Session History.

The desk remains available even if the shared host has zero live project
workers. Verify this through the host's normal idle lifecycle or an isolated
test installation; do not stop active project work just to test it. This is not
a guarantee that an old reception URL survives native expiry or service restart:
if it is offline, use the active reception environment to start another chat.
The server can refuse a second helper chat while its one slot is occupied;
continue or end that helper chat before starting another.

## Stop or remove

```sh
systemctl --user disable --now claude-reception-desk.service
```

This stops only the reception server and its native helper. Hosted project
conversations continue through `claude-session-host.service`. Remove the installed
unit and dedicated folder only after inspecting them for history/configuration
you want to retain. Do not re-enable an older project-specific Remote Control
service for this feature.
