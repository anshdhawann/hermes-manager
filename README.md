# hermes-manager

<p align="center"><img src="assets/repo-image.png" alt="Handing an agent the keys to its own plugins" width="560"></p>

A plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that lets Hermes manage its own
plugins, skills and MCP servers while it runs: no gateway restart, an approval card for anything that is
yours, a log of every change, and one-step undo.

| Tool | Actions |
|---|---|
| `plugin_manager` | list, create, enable, disable, delete, rename, reload, log, undo |
| `skill_manager` | list, enable, disable (creating, editing and deleting skills stays with Hermes' own `skill_manage`) |
| `mcp_manager` | list, add, enable, disable, reconnect, login (removing your own server stays with you: `hermes mcp remove <name>`) |

It also adds these commands:
- `hermes reload-plugins [--now]` in a terminal.
- `/reload-plugins [now]` in any chat.
- `/manager-log`, which shows the last 15 changes with their ids.
- `/manager-undo [id]`, which undoes one change (the newest one if no id is given) with no card, because you typed it.

## Install

```bash
hermes plugins install anshdhawann/hermes-manager
hermes gateway restart          # once; after that, /reload-plugins is enough
```

Or by hand: `git clone https://github.com/anshdhawann/hermes-manager ~/.hermes/plugins/hermes-manager`, then
`hermes plugins enable hermes-manager`. `tests/`, `scripts/` and `assets/` are only for development.

Needs Hermes Agent 0.22.5 or newer. It was built and run against Hermes `main` from November 2026. It uses
some of Hermes' internal helpers (plugin discovery, the MCP reconcile step, skills config), so a big
refactor upstream can break it. If a tool reports an import error after a Hermes update, please open an
issue.

### The main chat

Changes to things that are yours run only from your **main chat**: the newest chat you have pinned in the
default profile. Pin one chat before you use the manager. With no pinned chat those changes are refused
(they fail closed), and everything else still works.

## Who can change what

1. **No card.** Listing, reloading and reading the log. Also any change to something Hermes made itself:
   - plugins it made with `plugin_manager create`,
   - skills it made with `skill_manage create`,
   - MCP servers it added with `mcp_manager add`.

   These work from any chat, so a long-running agent can add a plugin that helps it.
2. **Approval card, from the main chat only.** Asked in another chat, these are refused, and that chat is told to ask the main chat.
   - any change to your own plugins, skills or servers,
   - every plugin rename, even of one Hermes made,
   - every MCP `add`, because it runs a program or connects to a URL,
   - enabling any plugin, even one Hermes made, whose `plugin.yaml` asks for env keys, packages to install, capabilities or a sign-in,
   - terminal commands that install code from the internet (pip, uv, npm, pnpm, yarn, bun, `git clone`, `curl | sh`) into `~/.hermes`.

   Two cards also show in any chat, not just the main one:
   - editing files of a plugin Hermes didn't make (write_file, patch, or a terminal command that writes there). "Always allow" covers only that one file (key `hermes-manager:edit:<plugin>:<file hash>`); for a terminal command it covers only that exact command.
   - installing code from the internet into `~/.hermes` (the item above).

   Each card's "Always allow" covers one action on one item. For example, `skill-manager:disable:notes` never covers another skill or another action. An MCP add's key also includes the exact command, so "Always" never pre-approves a different program.
3. **Always.**
   - The manager never disables, deletes or renames itself.
   - Hermes can't delete your plugins.
   - Every change, with or without a card, is added to `~/.hermes/hermes-manager/changes.jsonl` (when, which chat, tool, action, item, whether it needed a card, result).

The ledger file `~/.hermes/hermes-manager/ledger.json` records which items Hermes made. Only this plugin writes it, and a `created_by` label in plugin.yaml is never trusted. File and terminal tool calls that would edit the ledger or log are blocked. That block is a guard against mistakes, not a sandbox: any program running as you could still edit the file.

## Undo

Every line in `changes.jsonl` has an id. A change that landed also stores its opposite:

| Change | Undo |
|---|---|
| plugin enable / disable | disable / enable |
| plugin create | delete (to a backup) |
| plugin delete | put the folder back from its backup, and turn it back on if it was on |
| plugin rename | rename back (with a new backup) |
| skill enable / disable | disable / enable |
| MCP enable / disable | disable / enable |
| MCP add (Hermes' own) | remove it from config and stop it |
| MCP reconnect, plugin reload, a change that changed nothing | no undo |

`plugin_manager action=undo id=<id>` undoes one change; with no id it undoes the newest one that isn't undone yet, and the card pins that exact change. Undo follows the same rules as doing the opposite by hand: undoing a change to your item needs a card in the main chat, undoing a rename always needs a card, undoing a change to Hermes' own item needs none. The undo is logged as a new line with `undoes: <id>`, and a change can be undone only once.

## Sign-in for MCP servers

`mcp_manager action=login name=<server>` uses Hermes' own OAuth code (`tools.connectors.mcp_oauth.start`) and returns the sign-in link. It needs no card and works from any chat, and the link is never written to the log. A server that needs sign-in shows `needs sign-in: use mcp_manager action=login`.

Limit: unless the server pins its own `oauth.redirect_uri`, the link returns to `http://127.0.0.1:<port>/callback` on the computer running Hermes, so it must be opened in a browser there, within the time Hermes waits. To sign in from another device, set `oauth.redirect_uri` on that server to an address the device can reach. A local (stdio) server has no sign-in; it takes env keys (`${VAR}` in `~/.hermes/.env`).

## What each change does to open chats

| Change | New chats | Open chats |
|---|---|---|
| MCP server on, added, or reconnected with the same tools | have its tools | its tools are added at the end of the tool list on the next turn, so the prompt cache is kept |
| MCP server off, or reconnected with changed tools | don't have them | the next turn re-reads the prompt cache from where those tools were (one turn costs more) |
| Skill on or off | the skill list shows the change | prompt stays the same (cache kept); a skill that is off stops loading |
| Plugin code fix + reload | yes | the fixed code runs at once |
| New plugin tool or prompt text | yes | not until a new chat |
| Hermes core change, or a new Python package | needs `hermes gateway restart` | |

MCP changes use Hermes' own `reconcile_mcp_servers_with_config`, the same step the gateway runs every minute. A reconnect restarts only that one server. Neither one forces the full tool rebuild that `/reload-mcp` does, which is why open chats usually keep their cache.

## Rename

`plugin_manager rename name=<old> new_name=<new>` moves all of these in one step:
- the plugin folder,
- the `name` in plugin.yaml,
- its line in `plugins.enabled` and `plugins.disabled`,
- its `plugins.entries.<name>` settings,
- `~/.hermes/plugin-data/<name>`,
- saved "Always allow" approvals whose key ends in the old name.

It then reloads. A backup goes first, to `~/.hermes/backups/hermes-manager/rename-*`, with `UNDO.md` inside. If the plugin's own code spells its old name, the card says where; those lines are not changed. Built-in plugins, nested ones and hermes-manager itself are refused.

## Rule for plugins

A plugin that starts its own thread, timer or loop must stop it in `ctx.on_unload(...)`. Otherwise the old copy keeps running next to the reloaded one.

## Tests

`python3 -I tests/test_hermes_manager.py` runs the unit tests with Hermes stubbed out.

`scripts/livetest.sh` runs real Hermes in a throwaway home. It covers a real MCP server, a real skill and
a real plugin, and never touches `~/.hermes`. Set `HERMES_AGENT_DIR` if your hermes-agent checkout is not
at `~/.hermes/hermes-agent`.

## License

Apache License 2.0, see [LICENSE](LICENSE). The license covers the code only: the header image (`assets/repo-image.png`) is a widely shared meme that isn't ours, and it is not covered by the license. Not affiliated with Nous Research.
