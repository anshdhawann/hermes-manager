# hermes-manager

<p align="center"><img src="assets/repo-image.jpg" alt="Hermes agent being handed hermes-manager" width="560"></p>

A plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that lets Hermes manage its own
plugins, skills and MCP servers while it runs: no gateway restart, an approval card before any plugin code
runs and for anything that is yours, a log of every change, and one-step undo.

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

Needs Hermes Agent 0.23.0 or newer. It was built and run against Hermes `main` from November 2026. It uses
some of Hermes' internal helpers (listed under [Hermes internals it uses](#hermes-internals-it-uses)), so a
big refactor upstream can break it. If a tool reports an import error after a Hermes update, please open an
issue.

### The main chat

Changes to things that are yours run only from your **main chat**: the newest chat you have pinned in the
default profile. Pin one chat before you use the manager. With no pinned chat those changes are refused
(they fail closed), and everything else still works.

## What it does on your computer

Read this before you install it.

- **Writes Hermes' config.** `~/.hermes/config.yaml`: `plugins.enabled` / `plugins.disabled`, `skills.disabled`,
  `mcp_servers.<name>` (add, `enabled`), and on a rename the plugin's `plugins.entries.<name>` settings. It always
  writes through Hermes' own `save_config`.
- **Writes plugin folders.** Creates `~/.hermes/plugins/<name>` (`plugin_manager create`), renames them, and on
  delete moves them to `~/.hermes/backups/hermes-manager/`. Rename also moves `~/.hermes/plugin-data/<name>`.
- **Keeps its own files** in `~/.hermes/hermes-manager/`: `ledger.json` (what Hermes made) and `changes.jsonl`
  (every change, with chat id, tool, item and result). Nothing is sent anywhere. No telemetry.
- **Reads** `~/.hermes/state.db` read-only, to find your main chat and which chats are mid-reply.
- **Starts programs** only through MCP: `mcp_manager add` (after your card) and `enable` / `reconnect` start the
  server's command, the same way Hermes does for any MCP server you configure. It never runs a shell itself,
  refuses a shell as an MCP command, and refuses literal secrets (only `${VAR}` references to `~/.hermes/.env`).
- **Network:** none of its own. Remote MCP servers it adds are reached by Hermes' MCP client, and
  `mcp_manager login` uses Hermes' own OAuth flow (`tools.connectors.mcp_oauth`); Hermes stores the token, this
  plugin never sees it. The sign-in link is never written to the log.
- **Reloads plugins** in the running gateway and Desktop backend through Hermes' own `reload-plugins` control
  verb. A reload waits while another chat is mid-reply unless you say `now`.
- **No background work.** No threads, timers or loops of its own.
- **Unattended runs:** cards go through Hermes' approval system, so under cron or other non-interactive runs a
  change that needs a card fails closed instead of waiting.

## Who can change what

1. **No card.** Listing, reloading and reading the log. Turning off or deleting (to a backup) a plugin
   Hermes made with `plugin_manager create`. Any change to the skills Hermes made with `skill_manage create`
   and the MCP servers it added with `mcp_manager add` (you already saw the add card). These work from any chat.
2. **Approval card, in any chat.** Plugin code never runs without a card you saw:
   - turning on a plugin Hermes made. The card lists its files and line count, and its "Always allow" key
     includes a hash of the code (`plugin-manager:enable:<name>:<hash>`), so it never covers code that changed
     after you saw it,
   - editing the files of a plugin Hermes made while it is turned on (the edit would run at the next reload).
     Writing a plugin that is still off needs no card; turning it on does.

   To let Hermes skip these two cards for its own plugins, set `trust_own_items` (see [Settings](#settings)).
3. **Approval card, from the main chat only.** Asked in another chat, these are refused, and that chat is told to ask the main chat.
   - any change to your own plugins, skills or servers,
   - every plugin rename, even of one Hermes made,
   - every MCP `add`, because it runs a program or connects to a URL,
   - enabling any plugin, even one Hermes made, whose `plugin.yaml` asks for env keys, packages to install, capabilities or a sign-in. Capabilities stay off even then: Hermes grants them only after you consent in Terminal (`hermes plugins capabilities <name>`, then `hermes plugins enable <name>`),
   - terminal commands that install code from the internet (pip, uv, npm, pnpm, yarn, bun, `npx`, `uvx`, `git clone`, `curl | sh`) into `~/.hermes`.

   Three more cards also show in any chat, not just the main one:
   - editing files of a plugin Hermes didn't make (write_file, patch, or a terminal command that writes there). "Always allow" covers only that one file (key `hermes-manager:edit:<plugin>:<file hash>`); for a terminal command it covers only that exact command.
   - installing code from the internet into `~/.hermes` (the item above).
   - a terminal `hermes plugins|mcp|skills install|enable|disable|remove|...` command, which would otherwise skip these checks. "Always allow" covers only that exact command.

   Paths are compared after resolving them (the terminal `workdir`, a `cd` in the command, `~`, `$HOME`, `$HERMES_HOME`, `/./`, `..` and symlinks), and a card always shows the whole command. A command too long to show on a card is refused.

   Each card's "Always allow" covers one action on one item. For example, `skill-manager:disable:notes` never covers another skill or another action. An MCP add's key also includes the exact command, so "Always" never pre-approves a different program.
4. **Always.**
   - The manager never disables, deletes or renames itself.
   - Hermes can't delete your plugins.
   - Every change, with or without a card, is added to `~/.hermes/hermes-manager/changes.jsonl` (when, which chat, tool, action, item, whether it needed a card, result).

The ledger file `~/.hermes/hermes-manager/ledger.json` records which items Hermes made. Only this plugin writes it, and a `created_by` label in plugin.yaml is never trusted. File and terminal tool calls that would edit the ledger or log are blocked. That block is a guard against mistakes, not a sandbox: any program running as you could still edit the file.

### What the cards are, and what they are not

The cards check what Hermes **means** to do, from the tool call it sends. They catch mistakes and make sure you see each change to your things before it happens. They are **not a security boundary**. Hermes can run terminal commands and code as you, so a command that builds a path while it runs (`$(...)`, a string joined in Python), a script written in one step and run in the next, or a program that edits files itself is not seen. If you need Hermes to be unable to touch something, run it in a sandbox or as another user. Ownership works the same way: an item counts as Hermes' own only while it is the same thing Hermes made (same plugin folder, same MCP entry), so removing one and making a new one by hand under the same name makes it yours again.

## Settings

One setting, under `plugins.entries.hermes-manager.settings` in `~/.hermes/config.yaml` (the Desktop Plugins tab
shows it as a switch):

```yaml
plugins:
  entries:
    hermes-manager:
      settings:
        trust_own_items: false   # default
```

`trust_own_items: true` lets Hermes turn on, and edit while on, the plugins it made itself with no card, from any
chat. It suits a long-running agent that builds its own helpers, where you have decided to trust what it writes.
Only a real `true` counts; anything else is off. When Hermes tries to change this setting with its file or
terminal tools, that asks you too. Like the ledger guard, that is a speed bump against mistakes, not a sandbox.

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

`plugin_manager action=undo id=<id>` undoes one change; with no id it undoes the newest one that isn't undone yet, and the card pins that exact change. Undo follows the same rules as doing the opposite by hand: undoing a change to your item needs a card in the main chat, undoing a rename always needs a card, undoing a change to Hermes' own item needs none, except that putting back a deleted plugin that was on asks (in any chat) unless `trust_own_items` is set. The undo is logged as a new line with `undoes: <id>`, and a change can be undone only once.

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

## Hermes internals it uses

Hermes has no public plugin API yet for managing other plugins, skills or MCP servers, so this plugin calls
these internals. `hermes plugins validate` lists them as warnings (it never patches or rebinds any of them):

- plugins: `hermes_cli.plugins_cmd._discover_all_plugins`, `dashboard_set_agent_plugin_enabled` (the Desktop
  Plugins switch), `hermes_cli.plugins.get_plugin_manager`, `hermes_cli.plugins_activation.reload_running_gateway`
  and `notify_serve_backend`,
- skills: `hermes_cli.skills_config.get_disabled_skills` / `save_disabled_skills`, `tools.skills_tool.clear_skills_cache`,
  `agent.skill_commands.reload_skills`, `agent.prompt_builder.clear_skills_system_prompt_cache`,
  `agent.skill_utils.ESSENTIAL_SKILLS`,
- MCP: `tools.mcp_tool_discovery` (`get_mcp_status`, `reconcile_mcp_servers_with_config`, `discover_mcp_tools`),
  `tools.mcp_tool_lifecycle.shutdown_mcp_servers`, `tools.mcp_tool._mcp_registry_scope`,
  `tools.mcp_oauth.suppress_interactive_oauth`, `tools.mcp_tool_common.mcp_server_enabled`,
  `hermes_cli.mcp_config.redact_mcp_probe_text`, `hermes_cli.mcp_security.validate_mcp_server_entry`,
  `hermes_cli.mcp_catalog.record_mcp_install`, `tools.connectors.mcp_oauth.start`.

If Hermes adds public seams for these, the plugin will move to them.

## Tests

`~/.hermes/hermes-agent/venv/bin/python -I tests/test_hermes_manager.py` runs the unit tests with Hermes stubbed out. Any Python 3.11+ with PyYAML works; without PyYAML the tests stop and say so.

`scripts/livetest.sh` runs real Hermes in a throwaway home. It covers a real MCP server, a real skill and
a real plugin, and never touches `~/.hermes`. Set `HERMES_AGENT_DIR` if your hermes-agent checkout is not
at `~/.hermes/hermes-agent`.

## Acknowledgements

- Inspired by [DeepSeek Harness](https://www.deepseek.com/en/harness/) (DSH) and dsh-superagent, by DeepSeek.
- Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent), by Nous Research.

If you share or build on this plugin, please keep this credit. Apache 2.0 asks you to keep the [NOTICE](NOTICE) file with it.

## License

Apache License 2.0, see [LICENSE](LICENSE). The license covers the code only: the header image (`assets/repo-image.jpg`) is a widely shared meme that isn't ours, and it is not covered by the license. Not affiliated with DeepSeek or Nous Research.
