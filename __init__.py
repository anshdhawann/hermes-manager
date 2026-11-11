"""hermes-manager: let Hermes manage its own plugins, skills and MCP servers without a restart.

* ``plugin_manager`` tool: list, create, enable, disable, delete, rename, reload, log, undo
* ``skill_manager`` tool:  list, enable, disable (skill_manage keeps create/edit/delete)
* ``mcp_manager`` tool:    list, add, enable, disable, reconnect, login (sign-in link)
* ``hermes reload-plugins`` (Terminal) and ``/reload-plugins`` (any chat); ``/manager-log`` shows changes
  with their ids, ``/manager-undo [id]`` undoes one (no card: the user typed it)

Permissions:
1. No card: list, reload, log; turning off or deleting a plugin Hermes made; changes to the skills and MCP
   servers Hermes made (the ledger, not a label).
2. Card, main chat only: any change to the user's plugins, skills or servers; every rename; every MCP
   add; enabling a plugin that asks for env keys or capabilities. A card in any chat: installing code from
   the internet into ~/.hermes, editing the files of a plugin Hermes did not make and, unless the user set
   ``trust_own_items: true``, turning on a plugin Hermes made or editing one that is on. So no plugin code
   runs that the user has not seen a card for.
   The card's rule key names the action and the item, so "Always allow" never covers another.
3. Always: the manager never disables, deletes or renames itself, and every change is logged to
   ~/.hermes/hermes-manager/changes.jsonl.
4. Undo (plugin_manager action=undo, /manager-undo) runs the inverse change under the same rules: it asks
   when the inverse touches the user's items or is a rename, and not when it only touches Hermes' own.

Reloads use Hermes' own ``discover_plugins(force=True)`` path through the gateway's ``reload-plugins``
control-socket verb; what a reload does and does not cover is in README.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from . import common, guards, ledger, mcp_manager, rename as _rename, skill_manager

log = logging.getLogger("hermes_manager")
NAME = "hermes-manager"
TOOL = "plugin_manager"
TOOLSET = "plugin_manager"
CHANGES = ("create", "enable", "disable", "delete", "rename", "reload")
SELF = {"hermes-manager", "reload-plugins"}   # turning this off would take the tools away mid-chat
NOTE_UNLOAD = ("Plugins that start their own threads or timers must stop them in ctx.on_unload, or each "
               "reload leaves an extra copy running.")

SCHEMA = {
    "name": TOOL,
    "description": (
        "Manage Hermes plugins in the running gateway without a restart. action=list shows installed "
        "plugins (source=user by default; bundled or all on request), whether each is loaded, and "
        "made_by_hermes. action=create makes a new empty plugin you own in ~/.hermes/plugins/<name> (then "
        "write its code with your file tools, then enable). action=enable / disable switches a plugin on or "
        "off now. action=delete removes a plugin you made (moved to a backup). action=rename moves a plugin to "
        "new_name with its config, settings, data folder and saved approvals. action=reload reloads every "
        "plugin from disk after you edited plugin files. action=log shows recent manager changes with their ids. action=undo reverts one change made by any "
        "of the three manager tools (the newest not yet undone, or the given id); it asks the user when the "
        "reverse step touches their items or is a rename. Plugins "
        "you made (made_by_hermes true): turning one on, or editing one that is on, shows the user an approval "
        "card in this chat (unless they set trust_own_items); turning one off or deleting it runs at once. The "
        "user's plugins need an approval card and the main chat, and you can't delete them. Every rename needs the card. Editing the files of a plugin you did not make (write_file, patch, terminal) shows the user a card too. After a change: fixed tool and hook code runs at once, even in this chat; new tools and "
        "new prompt text only appear in NEW chats; Hermes core changes and new Python packages need the user "
        "to run `hermes gateway restart`. Reloads wait while another chat is mid-reply unless now=true; pass "
        "now=true only when the user asked. Never disable a safety plugin (safe-save, outbound-check, "
        "proof-gate, hermes-security) unless the user asked for exactly that. " + NOTE_UNLOAD
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["list", "create", "enable", "disable", "delete", "rename", "reload", "log", "undo"]},
            "id": {"type": "string", "description": "undo only: the change id from action=log (default: newest)."},
            "name": {"type": "string", "description": "Plugin key or manifest name."},
            "new_name": {"type": "string", "description": "rename only: the new name (lowercase, digits, -)."},
            "description": {"type": "string", "description": "create only: one line on what it does."},
            "source": {"type": "string", "enum": ["user", "bundled", "all"],
                       "description": "list only: which plugins to show (default user)."},
            "reason": {"type": "string",
                       "description": "One short line for the approval card: what changed and why."},
            "now": {"type": "boolean",
                    "description": "Change even while other chats are mid-reply. Only when the user asked."},
        },
        "required": ["action"],
    },
}


def _home() -> Path:
    return common.home()


def reload_everywhere() -> dict[str, Any]:
    """Force plugin re-discovery in every running Hermes process of this profile: the Desktop/dashboard
    backend (``hermes serve``, if one runs) and the gateway. Never raises.

    Returns ``{"backend": answer | None, "gateway": answer | None}``: None means that process did not
    answer (not running, or too old for the verb)."""
    from hermes_cli.plugins_activation import notify_serve_backend, reload_running_gateway
    out: dict[str, Any] = {"backend": None, "gateway": None}
    try:
        # load_and_go_live(NAME) in the backend: a forced pass over ALL plugins; NAME only picks whose
        # activation summary comes back (ours has no MCP servers or skills, so no note goes to chats).
        out["backend"] = notify_serve_backend(NAME, _home())
    except Exception as exc:
        log.warning("reload-plugins: backend reload failed: %s", exc)
    try:
        out["gateway"] = reload_running_gateway()
    except Exception as exc:
        log.warning("reload-plugins: gateway reload failed: %s", exc)
    return out


def summary(result: dict[str, Any]) -> str:
    """Plain lines for Terminal and chat."""
    lines = []
    gw = result.get("gateway")
    if gw is None:
        lines.append("Gateway: did not answer (not running?). Nothing was reloaded there.")
    elif not gw.get("reloaded"):
        lines.append(f"Gateway: refused to reload: {gw.get('error') or 'no reason given'}.")
    else:
        names = gw.get("plugins") or []
        lines.append(f"Gateway: reloaded {len(names)} plugins"
                     + (f", re-wired {gw['adapters_rewired']} platform adapters" if gw.get("adapters_rewired") else "")
                     + ".")
        blocked = [a for a in gw.get("activations") or () if a.get("missing_dependencies")]
        for a in blocked:
            lines.append(f"  {a.get('name') or a.get('key')}: needs {', '.join(a['missing_dependencies'])}"
                         " installed and a gateway restart.")
    be = result.get("backend")
    if be is not None:
        ok = be.get("ok") and be.get("activation") is not None
        lines.append("Desktop backend: reloaded." if ok else "Desktop backend: answered but did not reload.")
    lines.append("Fixed tool and hook code runs now, even in open chats. New tools and prompt text show up "
                 "in new chats. Hermes core changes and new Python packages still need a restart.")
    return "\n".join(lines)


def _busy_chats(current: Optional[str] = None) -> list[str]:
    """Chats mid-reply (other than ``current``): a reload briefly drops every hook and tool, so a turn
    running through it can miss one. Reads the durable turn ledger in state.db, which the gateway and
    the TUI gateway both write, read-only. Best effort: empty when it cannot be read."""
    import sqlite3
    busy: set[str] = set()
    names: dict[str, str] = {}
    sql = ("SELECT target_session_id FROM session_admissions WHERE status IN ('started','running','registered') "
           "UNION SELECT session_id FROM worker_executions WHERE status!='terminal'")
    try:
        con = sqlite3.connect(f"file:{_home() / 'state.db'}?mode=ro", uri=True, timeout=2)
        try:
            busy.update(str(r[0]) for r in con.execute(sql) if r[0])
            busy.discard(str(current or ""))
            if busy:
                try:
                    q = ",".join("?" * len(busy))
                    names.update((str(i), str(t)) for i, t in
                                 con.execute(f"SELECT id, title FROM sessions WHERE id IN ({q})", sorted(busy)) if t)
                except sqlite3.Error:
                    pass   # older state.db without titles: show ids
        finally:
            con.close()
    except Exception:
        pass
    return sorted(names.get(b, b) for b in busy)


async def _slash(raw_args: str = "", session_id: Optional[str] = None, session_key: Optional[str] = None) -> str:
    """``/reload-plugins [now]``. Async so the blocking reload runs off the gateway's event loop (the
    gateway answers its own control socket on that loop's executor and re-wires adapters on the loop)."""
    force = (raw_args or "").strip().lower() in {"now", "force", "-f", "--force"}
    busy = [] if force else _busy_chats(session_id or session_key)
    if busy:
        return (f"Not reloading: {len(busy)} other chat(s) are mid-reply ({', '.join(busy[:3])}). "
                "Try again when they finish, or send /reload-plugins now.")
    result = await asyncio.to_thread(reload_everywhere)
    return summary(result)


def _list(source: str = "user") -> dict[str, Any]:
    """Installed plugins with their loaded state, from Hermes' own resolver and the running manager."""
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.plugins_cmd import _discover_all_plugins
    loaded = {str(p.get("key") or p.get("name")): p for p in get_plugin_manager().list_plugins()}
    rows = []
    for name, version, description, src, _dir, key in _discover_all_plugins():
        if source != "all" and src != source:
            continue
        p = loaded.get(key) or {}
        error = p.get("error")
        rows.append({"key": key, "name": name, "version": version, "source": src,
                     "loaded": bool(p.get("enabled")) and not error,
                     **({"note": str(error)[:160]} if error else {}),
                     "description": str(description or "")[:160]})
    return {"plugins": rows, "count": len(rows)}


def _set_enabled(name: str, enabled: bool) -> dict[str, Any]:
    """Hermes' own toggle (the Desktop Plugins switch): writes config.yaml with the canonical key and, on
    enable, loads the plugin here and in the gateway. Disable is config-only in Hermes, so it is
    followed by a reload that unloads the plugin's tools and hooks now."""
    from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled
    result = dashboard_set_agent_plugin_enabled(name, enabled=enabled)
    out: dict[str, Any] = {"ok": bool(result.get("ok")), "plugin": result.get("name") or name,
                           "unchanged": bool(result.get("unchanged"))}
    if not result.get("ok"):
        out["error"] = result.get("error") or "toggle failed"
        return out
    if enabled:
        act = result.get("activation") or {}
        out["missing_dependencies"] = act.get("missing_dependencies") or []
        out["restart_required"] = bool(result.get("restart_required"))
        out["summary"] = ("Enabled and loaded now. Its tools appear in new chats." if not out["restart_required"]
                          else "Enabled in config, but it needs `hermes gateway restart` to load"
                               + (f" (missing {', '.join(out['missing_dependencies'])})." if out["missing_dependencies"] else "."))
        if "capabilities" in _asks_for_more(name):
            # Hermes' toggle never grants capabilities: they stay off (fail closed) until the user consents.
            out["summary"] += (f" It declares capabilities; Hermes keeps them off until the user grants them in "
                               f"Terminal: `hermes plugins capabilities {out['plugin']}` to review, then "
                               f"`hermes plugins enable {out['plugin']}`.")
    elif not out["unchanged"]:
        out["summary"] = summary(reload_everywhere()) + (
            "\nDisabled: its tools and hooks are unloaded; Telegram buttons it wired stay until a restart.")
    else:
        out["summary"] = "Already disabled."
    return out


MANIFEST = """name: {name}
version: 0.1.0
description: {description}
author: hermes
created_by: hermes
"""

INIT = '''"""{description}

Made by Hermes with plugin_manager create. Register tools and hooks in register(ctx). Anything that starts
a thread, timer or loop must stop it in ctx.on_unload(...), or every reload leaves a copy running.
"""


def register(ctx) -> None:
    pass
'''


def _manifest(folder: Path) -> dict:
    for fn in ("plugin.yaml", "plugin.yml"):
        p = folder / fn
        if p.exists():
            try:
                import yaml
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
    return {}


def _asks_for_more(name: str) -> list[str]:
    """What enabling would hand the plugin beyond code: env keys or declared capabilities."""
    found = _rename.find_user_plugin(name)
    if not found or not found["dir"]:
        return []
    m = _manifest(found["dir"])
    asks = []
    for key in ("requires_env", "optional_env"):
        if m.get(key):
            vals = m[key] if isinstance(m[key], list) else [m[key]]
            asks.append("env keys " + ", ".join(str(v.get("name", v) if isinstance(v, dict) else v) for v in vals)[:120])
    for key in ("pip_dependencies", "python_dependencies", "external_dependencies"):
        if m.get(key):
            asks.append("packages to install from the internet")
            break
    for key, label in (("capabilities", "capabilities"), ("requires_auth", "sign-in"), ("oauth", "sign-in")):
        if m.get(key) and label not in asks:
            asks.append(label)
    return asks


def _key_of(name: str) -> str:
    found = _rename.find_user_plugin(name) if name else None
    return found["key"] if found else name


def _trusted(trust: Optional[bool]) -> bool:
    return common.trust_own_items() if trust is None else trust


def needs_card(args: dict, trust: Optional[bool] = None) -> bool:
    """Whether this plugin_manager call shows the user a card. ``trust`` overrides the trust_own_items
    setting (None reads it): with it off (the default) turning on a plugin Hermes made asks too."""
    action = str(args.get("action") or "")
    name = str(args.get("name") or "").strip()
    if action == "rename":
        return True   # every rename asks, even for Hermes' own plugins
    if action in ("enable", "disable"):
        if not ledger.owned("plugin", _key_of(name)):
            return True
        if action == "disable":
            return False   # turning Hermes' own plugin off runs no new code
        return bool(_asks_for_more(name)) or not _trusted(trust)
    return False   # list, log, reload, create; delete only works on Hermes' own plugins


def needs_main(args: dict) -> bool:
    """Whether the card must come from the main chat: the card rules with Hermes' own plugins trusted. A card
    that shows only because trust_own_items is off (enabling a plugin Hermes made) works in any chat."""
    return needs_card(args, trust=True)


def _own_code(name: str) -> tuple[str, str]:
    """(what the card says about a plugin Hermes made, a hash of its code). The hash goes in the rule key,
    so "Always allow" covers this exact code and a later edit asks again."""
    found = _rename.find_user_plugin(name)
    folder = found["dir"] if found else None
    if not folder or not Path(folder).is_dir():
        return "", "nofiles"
    digest = hashlib.sha256()
    files, lines = [], 0
    for f in sorted(Path(folder).rglob("*")):
        if not f.is_file() or any(part in _rename.SKIP_DIRS for part in f.relative_to(folder).parts):
            continue
        data = f.read_bytes()
        rel = f.relative_to(folder).as_posix()
        digest.update(rel.encode() + b"\0" + data + b"\0")
        files.append(rel)
        lines += data.count(b"\n")
    desc = common.clip(_manifest(Path(folder)).get("description") or "", 160)
    shown = ", ".join(files[:8]) + (f" (+{len(files) - 8} more)" if len(files) > 8 else "")
    text = (f"Hermes made it{': ' + desc if desc else ''}. It runs as Python with your access. "
            f"Files in {folder}: {shown or 'none'} ({lines} lines).")
    return text, digest.hexdigest()[:8]


def _create(name: str, description: str, chat: Optional[str]) -> dict[str, Any]:
    if not _rename.NEW_NAME_RE.match(name):
        return {"ok": False, "error": "name must be 2-48 lowercase letters, digits or -, starting with a letter or digit."}
    folder = common.plugins_dir() / name
    if folder.exists() or _rename.find_user_plugin(name) is not None:
        return {"ok": False, "error": f"A plugin named {name!r} already exists."}
    desc = common.clip(description or f"{name} (made by Hermes)", 200).replace('"', "'")
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(MANIFEST.format(name=name, description=json.dumps(desc)), encoding="utf-8")
    (folder / "__init__.py").write_text(INIT.format(description=desc), encoding="utf-8")
    ledger.add("plugin", name, chat)
    return {"ok": True, "plugin": name, "dir": str(folder),
            "summary": f"Created {folder} (not enabled). Write its code, then plugin_manager enable. " + NOTE_UNLOAD}


def _delete(name: str) -> dict[str, Any]:
    key = _key_of(name)
    if not ledger.owned("plugin", key):
        return {"ok": False, "error": f"{name!r} was not made by Hermes, so it can't be deleted from chat. "
                                     f"The user can run `hermes plugins remove {name}`."}
    found = _rename.find_user_plugin(key)
    out: dict[str, Any] = {"ok": True, "plugin": key}
    if found is None or not found["dir"]:
        ledger.drop("plugin", key)
        out["summary"] = "Its folder was already gone; forgot it."
        return out
    plugins = common.read_config().get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    out["was_enabled"] = isinstance(enabled, list) and key in map(str, enabled)
    off = _set_enabled(key, False)
    backup = common.home() / "backups" / "hermes-manager" / f"deleted-{key}-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(found["dir"]), str(backup))
    ledger.drop("plugin", key)
    out["backup"] = str(backup)
    out["summary"] = f"Deleted {key} (moved to {backup}). " + str(off.get("summary") or "")
    return out


def _restore(name: str, backup: Any, was_enabled: bool, chat: Optional[str]) -> dict[str, Any]:
    """Undo of a delete: move the backup folder back, own it again, turn it on if it was on."""
    if not _rename.NEW_NAME_RE.match(name or ""):
        return {"ok": False, "error": f"Bad plugin name in the change log: {name!r}."}
    root = (common.home() / "backups" / "hermes-manager").resolve()
    src = Path(str(backup or "")).expanduser()
    if not src.is_dir() or not src.resolve().is_relative_to(root):
        return {"ok": False, "error": f"The backup {src} is gone (or not a hermes-manager backup); can't restore {name}."}
    target = common.plugins_dir() / name
    if target.exists() or _rename.find_user_plugin(name) is not None:
        return {"ok": False, "error": f"A plugin named {name!r} exists again; rename or delete it first."}
    shutil.move(str(src), str(target))
    ledger.add("plugin", name, chat, restored_from=str(src))
    out: dict[str, Any] = {"ok": True, "plugin": name, "dir": str(target), "summary": f"Restored {name} from {src}."}
    if was_enabled:
        on = _set_enabled(name, True)
        out["summary"] += " " + str(on.get("summary") or on.get("error") or "")
        if not on.get("ok"):
            out["note"] = "Restored but not turned back on: " + str(on.get("error"))
    return out


def _undo_of(action: str, name: str, out: dict) -> Optional[dict]:
    """The inverse call stored with a change; None when there is nothing to undo."""
    if not out.get("ok") or out.get("unchanged"):
        return None
    if action in ("enable", "disable"):
        return {"tool": TOOL, "args": {"action": "disable" if action == "enable" else "enable",
                                       "name": out.get("plugin") or name}}
    if action == "create":
        return {"tool": TOOL, "args": {"action": "delete", "name": name}}
    if action == "delete" and out.get("backup"):
        return {"tool": TOOL, "args": {"action": "restore", "name": out.get("plugin") or name,
                                       "backup": out["backup"], "was_enabled": bool(out.get("was_enabled"))}}
    if action == "rename" and " -> " in str(out.get("renamed") or ""):
        old, new = out["renamed"].split(" -> ", 1)
        return {"tool": TOOL, "args": {"action": "rename", "name": new, "new_name": old}}
    return None   # reload has no undo


def _undo_needs_card(inv: dict, trust: Optional[bool] = None) -> bool:
    args = inv.get("args") or {}
    if args.get("action") == "remove":
        return False   # only ever reverses Hermes' own MCP add
    if args.get("action") == "restore":   # reverses Hermes' own delete; asks when it turns code back on
        return bool(args.get("was_enabled")) and not _trusted(trust)
    if inv.get("tool") == TOOL:
        return needs_card(args, trust)
    return {mcp_manager.TOOL: mcp_manager.needs_card, skill_manager.TOOL: skill_manager.needs_card}[inv["tool"]](args)


def _undo(cid: Optional[str], session_id: Optional[str], conversation_id: Optional[str], user: bool) -> dict[str, Any]:
    """Run a logged change's inverse. ``user`` = the user typed /manager-undo, so no card or main check."""
    row, why = ledger.undo_target(cid)
    if row is None:
        return {"ok": False, "error": why, "nothing": True}
    inv = row["undo"]
    t, a = inv.get("tool"), dict(inv.get("args") or {})
    act, name = str(a.get("action") or ""), str(a.get("name") or "")
    card = False
    try:
        card = not user and _undo_needs_card(inv)
        if act == "restore":
            out = _restore(name, a.get("backup"), bool(a.get("was_enabled")), session_id)
        elif t == TOOL:
            out = _change(act, name, a, session_id, conversation_id, not user and _undo_needs_card(inv, trust=True))
        elif t == skill_manager.TOOL:
            refusal = common.not_main(session_id) if card else None
            out = {"ok": False, "error": refusal} if refusal else skill_manager._set_enabled(name, act == "enable")
        elif t == mcp_manager.TOOL:
            out = mcp_manager._change(act, name, a, session_id, card)
        else:
            out = {"ok": False, "error": f"Unknown tool {t!r} in the change log."}
    except Exception as exc:
        log.warning("undo of %s failed: %s", row.get("id"), exc)
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    target = f"{name} -> {a.get('new_name')}" if act == "rename" else name
    ledger.log(t or "?", act, target, chat=session_id, card=card, result=out, undoes=row["id"])
    what = f"{row.get('tool')} {row.get('action')} {row.get('target')}"
    out.update(undid=row["id"], undid_what=what)
    out["summary"] = (f"Undid {row['id']} ({what}). " if out.get("ok") else f"Could not undo {row['id']} ({what}). ") \
        + str(out.get("summary") or out.get("error") or out.get("message") or "")
    return out


_pinned_undo: dict[str, str] = {}   # session -> the change id its undo card was about


def _undo_card(args: dict, session_id: Optional[str]) -> Optional[dict]:
    """The card for action=undo is the card the inverse call would get, keyed to this one change. The
    approved id is pinned so a change landing between card and tool can't be undone in its place."""
    row, _why = ledger.undo_target(args.get("id"))
    if row is None:
        return None   # the tool says why
    inv = row["undo"]
    d = None
    if _undo_needs_card(inv):
        inv_args = {**(inv.get("args") or {}), "reason": args.get("reason") or f"undo change {row['id']}"}
        d = {TOOL: _plugin_card, mcp_manager.TOOL: mcp_manager.card,
             skill_manager.TOOL: skill_manager.card}[inv["tool"]](inv_args, session_id)
        if d and d.get("action") == "approve":
            d = {**d, "rule_key": f"hermes-manager:undo:{row['id']}",
                 "message": f"Undo change {row['id']} ({row.get('tool')} {row.get('action')} {row.get('target')})? "
                            + str(d.get("message") or "")}
    if d is None or d.get("action") == "approve":
        _pinned_undo[session_id or ""] = row["id"]
    return d


def _do_rename(name: str, new: str) -> dict[str, Any]:
    plan, why = _rename.check(name, new, SELF)
    if why:
        return {"ok": False, "error": why}
    out = _rename.run(plan, lambda: summary(reload_everywhere()))
    if out.get("ok"):
        ledger.rename("plugin", plan["old"], plan["new"])
    return out


def _tool(args: dict, session_id: Optional[str] = None, conversation_id: Optional[str] = None, **_: Any) -> str:
    """The model's ``plugin_manager``. Card-needing changes only run after the user approved the card
    (``_pre_tool_call``). Called on a tool worker thread, never the gateway loop, so blocking is fine."""
    args = args or {}
    action = str(args.get("action") or "")
    try:
        if action == "list":
            out = _list(str(args.get("source") or "user"))
            own = set(ledger.names("plugin"))
            for p in out["plugins"]:
                p["made_by_hermes"] = p["key"] in own
            return json.dumps(out)
        if action == "log":
            return json.dumps({"changes": ledger.recent(30), "file": str(ledger.folder() / "changes.jsonl")})
        if action == "undo":
            pinned = _pinned_undo.pop(session_id or "", None)
            return json.dumps(_undo(str(args.get("id") or "") or pinned, session_id, conversation_id, user=False))
        if action not in CHANGES:
            return json.dumps({"error": f"unknown action {action!r}; use list, create, enable, disable, delete, "
                                       "rename, reload, log or undo"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    name = str(args.get("name") or "").strip()
    card = False
    try:
        card = needs_card(args)
        out = _change(action, name, args, session_id, conversation_id, needs_main(args))
    except Exception as exc:   # a tool never takes the turn down
        log.warning("plugin_manager %s failed: %s", action, exc)
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    target = f"{name} -> {args.get('new_name')}" if action == "rename" else (name or "all")
    out["change_id"] = ledger.log(TOOL, action, target, chat=session_id, card=card, result=out,
                                  undo=_undo_of(action, name, out))
    return json.dumps(out)


def _change(action: str, name: str, args: dict, session_id: Optional[str], conversation_id: Optional[str],
            main_only: bool) -> dict[str, Any]:
    if main_only:
        refusal = common.not_main(session_id)
        if refusal:
            return {"ok": False, "error": refusal}
    if action != "reload" and not name:
        return {"ok": False, "error": f"{action} needs a plugin name (see action=list)"}
    if action in ("disable", "delete", "rename") and (name in SELF or _key_of(name) in SELF):
        return {"ok": False, "error": "hermes-manager provides these tools; the user can turn it off with "
                                      "`hermes plugins disable hermes-manager`."}
    if action == "create":
        return _create(name, str(args.get("description") or ""), session_id)
    if action in ("reload", "disable", "delete", "rename"):
        busy = [] if args.get("now") is True else [
            b for b in _busy_chats(session_id) if b not in (conversation_id, session_id)]   # never this chat
        if busy:
            return {"ok": False, "busy": busy,
                    "message": f"{len(busy)} other chat(s) are mid-reply ({', '.join(busy[:3])}). "
                               "Wait and call again; pass now=true only if the user asked."}
    if action == "reload":
        result = reload_everywhere()
        return {"ok": bool((result.get("gateway") or {}).get("reloaded")), "summary": summary(result)}
    if action == "delete":
        return _delete(name)
    if action == "rename":
        return _do_rename(name, str(args.get("new_name") or "").strip())
    return _set_enabled(name, action == "enable")


def _plugin_card(args: dict, session_id: Optional[str]) -> Optional[dict]:
    action = str(args.get("action") or "")
    name = str(args.get("name") or "").strip()
    reason = common.clip(args.get("reason") or "no reason given", 200)
    if action == "restore":   # only reached as an undo of Hermes' own delete
        if not _undo_needs_card({"tool": TOOL, "args": args}):
            return None
        return {"action": "approve", "rule_key": f"plugin-manager:restore:{name}",
                "message": f"Put back the plugin {name!r} that Hermes made and deleted, and turn it ON again? "
                           f"Reason: {reason}"}
    if not needs_card(args):
        return None
    if needs_main(args):
        refusal = common.not_main(session_id)
        if refusal:
            return {"action": "block", "message": refusal}
    if action == "rename":
        new = str(args.get("new_name") or "").strip()
        plan, why = _rename.check(name, new, SELF)
        if why:
            return None   # the tool refuses with the reason; nothing to approve
        return {"action": "approve", "rule_key": f"plugin-manager:rename:{plan['old']}:{new}",
                "message": f"{_rename.describe(plan)} Reason: {reason}"}
    key = f"plugin-manager:{action}:{name or 'all'}"
    if action == "enable":
        asks = _asks_for_more(name)
        what = f"Turn ON the plugin {name!r} now?" + (f" It asks for {' and '.join(asks)}." if asks else "")
        if ledger.owned("plugin", _key_of(name)):
            about, code = _own_code(name)
            what += f" {about} Always allow covers only this exact code."
            key += f":{code}"
    else:
        what = f"Turn OFF the plugin {name!r} now?"
    return {"action": "approve", "rule_key": key, "message": f"{what} Reason: {reason}"}


def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None, session_id: str = "", **_: Any) -> Optional[dict]:
    """One hook for all three tools, plus the ledger guard and the install-from-internet card.
    An "approve" goes to Hermes' human-approval card and fails closed on deny or timeout."""
    args = args if isinstance(args, dict) else {}
    try:
        if tool_name == TOOL:
            return _undo_card(args, session_id) if args.get("action") == "undo" else _plugin_card(args, session_id)
        if tool_name == mcp_manager.TOOL:
            return mcp_manager.card(args, session_id)
        if tool_name == skill_manager.TOOL:
            return skill_manager.card(args, session_id)
        return (ledger.guard_files(tool_name, args) or guards.setting_card(tool_name, args)
                or guards.install_card(tool_name, args) or guards.edit_card(tool_name, args))
    except Exception as exc:
        log.warning("hermes-manager pre_tool_call failed for %s: %s", tool_name, exc)
        if tool_name in (TOOL, mcp_manager.TOOL, skill_manager.TOOL):
            return {"action": "block", "message": f"hermes-manager could not check this change: {exc}"}
        return None


def _post_tool_call(tool_name: str = "", args: Optional[dict] = None, result: Any = None, session_id: str = "",
                    **_: Any) -> None:
    try:
        skill_manager.after_tool(tool_name, args or {}, result, session_id)
    except Exception:
        pass


async def _log_slash(raw_args: str = "", **_: Any) -> str:
    rows = ledger.recent(15)
    if not rows:
        return "No manager changes yet."
    undone = {r["undoes"] for r in ledger.recent(None) if r.get("undoes") and r.get("ok")}
    return "\n".join(f"{r.get('id', '--------')}  {r.get('at', '')[:16]}  {r.get('tool')} {r.get('action')} {r.get('target')}"
                     f"{'  (undo of ' + r['undoes'] + ')' if r.get('undoes') else ''}"
                     f"{'  [undone]' if r.get('id') in undone else ''}"
                     f"{'  (card)' if r.get('card') else ''}  {'ok' if r.get('ok') else 'FAILED'}"
                     f"{'  ' + r['note'] if r.get('note') and not r.get('ok') else ''}" for r in rows)


async def _undo_slash(raw_args: str = "", session_id: Optional[str] = None, **_: Any) -> str:
    out = await asyncio.to_thread(_undo, (raw_args or "").strip() or None, session_id, None, True)
    if out.get("nothing"):
        return "Nothing to undo." if not (raw_args or "").strip() else str(out.get("error"))
    return str(out.get("summary"))


def _cli_setup(parser) -> None:
    parser.description = ("Reload every plugin in the running gateway (and Desktop backend) without a "
                          "restart. Waits for no chat to be mid-reply unless --now.")
    parser.add_argument("--now", action="store_true", help="reload even while a chat is mid-reply")


def _cli_run(args) -> None:
    busy = [] if getattr(args, "now", False) else _busy_chats()
    if busy:
        print(f"Not reloading: {len(busy)} chat(s) are mid-reply ({', '.join(busy[:3])}). "
              "Try again when they finish, or run: hermes reload-plugins --now")
        return
    print(summary(reload_everywhere()))


def register(ctx) -> None:
    ctx.register_command("reload-plugins", handler=_slash, description="Reload every plugin without a gateway restart",
                         args_hint="[now]")
    ctx.register_command("manager-log", handler=_log_slash,
                         description="Recent plugin, skill and MCP changes made by Hermes")
    ctx.register_command("manager-undo", handler=_undo_slash, args_hint="[id]",
                         description="Undo the last manager change (or the one with this id from /manager-log)")
    ctx.register_cli_command(name="reload-plugins", help="Reload every plugin without restarting the gateway",
                             setup_fn=_cli_setup, handler_fn=_cli_run)
    ctx.register_tool(name=TOOL, toolset=TOOLSET, schema=SCHEMA, handler=_tool,
                      description="Manage Hermes plugins without a restart", emoji="🔄")
    ctx.register_tool(name=skill_manager.TOOL, toolset=skill_manager.TOOLSET, schema=skill_manager.SCHEMA,
                      handler=skill_manager.tool, description="Turn Hermes skills on or off", emoji="🧠")
    ctx.register_tool(name=mcp_manager.TOOL, toolset=mcp_manager.TOOLSET, schema=mcp_manager.SCHEMA,
                      handler=mcp_manager.tool, description="Manage MCP servers without a restart", emoji="🔌")
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
