# Claude Reception Desk

This folder is the permanent phone entry point for Claude Session Sync.
Use this conversation to start shared project conversations with `/cd-claude`.

- Use the installed personal `cd-claude` skill. List enabled projects when the
  user has not supplied a folder; create exactly the requested conversation and
  return its official Claude phone link.
- Project work belongs in the new shared conversation. Do not change this
  conversation's working directory or edit project files as part of launching it.
- This reception conversation is served by native Remote Control, outside the
  shared-session host. Its model/effort are not inherited by `--inherit-current`.
  Pass a user's requested model/effort explicitly; otherwise shared conversations
  follow the host's normal defaults.
- Do not edit the project allowlist, stop other sessions, bypass permissions, or
  start additional Remote Control servers to work around a reported error.
- An expired project's old phone link does not wake its worker. `/cd-claude`
  creates a new conversation; resuming saved chats requires VS Code or the local
  client. Do not promise automatic resume, reboot recovery or a permanent URL.

The directory is an organizational boundary, not a filesystem sandbox.
