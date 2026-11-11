"""mcp_manager: list, add, enable, disable and reconnect MCP servers in the running Hermes.

Built on Hermes' own pieces, nothing new underneath:
* status:     ``tools.mcp_tool_discovery.get_mcp_status`` (what the banner and Desktop show)
* on / off:   ``mcp_servers.<name>.enabled`` (the ONE key ``mcp_server_enabled`` reads), then
              ``reconcile_mcp_servers_with_config`` (what the gateway's housekeeping runs every minute)
* add:        the entry ``hermes mcp add`` writes, checked by ``validate_mcp_server_entry`` first
* reconnect:  ``shutdown_mcp_servers(names={name})`` + ``discover_mcp_tools`` for that one server
* login:      ``tools.connectors.mcp_oauth.start`` (what manage_connections authorize and the Desktop
              Sign in button run); returns the sign-in link
* remove:     internal, only as the undo of Hermes' own add (drop the entry, reconcile)

Open chats pick the change up on their next turn through Hermes' between-turns refresh, which keeps
the tool array append-only (new tools go at the end). Unlike /reload-mcp this never forces a full
tool rebuild, so a chat keeps its prompt cache unless the server's tools actually changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any, Optional

from . import common, ledger

log = logging.getLogger("hermes_manager.mcp")
TOOL = "mcp_manager"
TOOLSET = "mcp_manager"
CHANGES = ("add", "enable", "disable", "reconnect")
CONNECT_WAIT_S = 45.0
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "pwsh", "powershell", "cmd", "cmd.exe"}

SCHEMA = {
    "name": TOOL,
    "description": (
        "Manage MCP servers in the running Hermes without a restart. action=list shows every configured "
        "server: status (connected, failed, disabled, lazy, connecting), tool count, the error if it failed, "
        "and what it runs (command or URL; env var NAMES only). action=enable / disable switches a server on "
        "or off and applies it now. action=reconnect restarts one server (use it when a server failed or its "
        "tools stopped answering). action=add saves a NEW server and connects it: give url (remote) OR "
        "command + args (local program); env and headers values must be ${VAR} references to keys in "
        "~/.hermes/.env, never literal secrets (ask the user to add the key first). A change "
        "on a server you added (made_by_hermes true) runs at once from any chat. Every add, and any change to the user's servers, shows an approval card and runs from the main chat only. Open chats see the change on their next "
        "turn: added tools go at the end of the tool list (prompt cache kept); removed tools re-read the "
        "cache from where they sat. A server that needs sign-in reports needs_auth: run action=login to get its sign-in link "
        "(no card, any chat) and give the link to the user. Undo Hermes' own add with plugin_manager "
        "action=undo; removing the user's servers is their call: `hermes mcp remove <name>`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "enable", "disable", "reconnect", "login"]},
            "name": {"type": "string", "description": "Server name as in action=list (lowercase, - or _)."},
            "url": {"type": "string", "description": "add only: the remote server's endpoint (https://...)."},
            "command": {"type": "string", "description": "add only: the program to run, e.g. npx or uvx."},
            "args": {"type": "array", "items": {"type": "string"}, "description": "add only: its arguments."},
            "env": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "add only (local): env vars, every value a ${VAR} reference."},
            "headers": {"type": "object", "additionalProperties": {"type": "string"},
                        "description": "add only (remote): HTTP headers, every value a ${VAR} reference "
                                       "(or 'Bearer ${VAR}')."},
            "reason": {"type": "string", "description": "One short line for the approval card."},
        },
        "required": ["action"],
    },
}


# ------------------------------------------------------------------ reading

def _servers_raw() -> dict[str, dict]:
    servers = common.read_config().get("mcp_servers")
    return servers if isinstance(servers, dict) else {}


def _runs(cfg: dict) -> str:
    """What the server runs, for cards and lists: the command line or URL, env var names only."""
    from hermes_cli.mcp_config import redact_mcp_probe_text
    if cfg.get("url"):
        text = str(cfg["url"])
        if isinstance(cfg.get("headers"), dict) and cfg["headers"]:
            text += f" (headers: {', '.join(sorted(map(str, cfg['headers'])))})"
    else:
        args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
        text = " ".join([str(cfg.get("command") or "?"), *map(str, args)])
        if isinstance(cfg.get("env"), dict) and cfg["env"]:
            text += f" (env: {', '.join(sorted(map(str, cfg['env'])))})"
    return common.clip(redact_mcp_probe_text(text), 300)


def _status(name: Optional[str] = None) -> list[dict]:
    from hermes_cli.mcp_config import redact_mcp_probe_text
    from tools.mcp_tool_discovery import get_mcp_status
    raw = _servers_raw()
    rows = []
    for st in get_mcp_status():
        if name and st.get("name") != name:
            continue
        row = {k: st.get(k) for k in ("name", "status", "tools", "transport")}
        if st.get("error"):
            row["error"] = common.clip(redact_mcp_probe_text(st["error"]), 200)
        if st.get("needs_auth"):
            row["needs_auth"] = True
        cfg = raw.get(st.get("name")) or {}
        if cfg:
            row["runs"] = _runs(cfg)
        rows.append(row)
    return rows


# ------------------------------------------------------------------ changing

def _apply_now(timeout: float = CONNECT_WAIT_S) -> dict[str, Any]:
    """Bring live servers in step with config now (tear down disabled ones, connect enabled ones).
    Bounded: a slow server keeps connecting in the background and the housekeeping pass finishes it."""
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            from tools.mcp_oauth import suppress_interactive_oauth
            from tools.mcp_tool_discovery import reconcile_mcp_servers_with_config
            with suppress_interactive_oauth():
                out["result"] = reconcile_mcp_servers_with_config()
        except Exception as exc:   # never take the turn down
            out["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=run, name="mcp-manager-apply", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        out["still_connecting"] = True
    return out


def _reconnect(name: str, timeout: float = CONNECT_WAIT_S) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            from tools.mcp_oauth import suppress_interactive_oauth
            from tools.mcp_tool import _mcp_registry_scope
            from tools import mcp_tool_discovery as discovery
            from tools.mcp_tool_lifecycle import shutdown_mcp_servers
            shutdown_mcp_servers(scope=_mcp_registry_scope(), names={name})
            discovery._clear_connect_failure(name)
            with suppress_interactive_oauth():
                discovery.discover_mcp_tools()
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=run, name="mcp-manager-reconnect", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        out["still_connecting"] = True
    return out


def _set_enabled(name: str, enabled: bool) -> dict[str, Any]:
    cfg = common.read_config()
    servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
    if name not in servers or not isinstance(servers[name], dict):
        return {"ok": False, "error": f"No MCP server named {name!r} (see action=list)."}
    from tools.mcp_tool_common import mcp_server_enabled
    if mcp_server_enabled(servers[name]) == enabled:
        return {"ok": True, "unchanged": True, "server": (_status(name) or [{}])[0]}
    servers[name]["enabled"] = enabled
    common.save_config(cfg)
    applied = _apply_now()
    return _result(name, applied, "Turned on" if enabled else "Turned off")


def _check_refs(kind: str, values: Any) -> Optional[str]:
    if values in (None, {}):
        return None
    if not isinstance(values, dict):
        return f"{kind} must be an object of NAME: ${{VAR}} pairs."
    def ok(v: Any) -> bool:
        v = str(v or "").strip()
        if kind == "headers" and v[:7].lower() == "bearer ":
            v = v[7:].strip()
        return bool(common.ENV_REF_RE.match(v))
    bad = [k for k, v in values.items() if not ok(v)]
    if bad:
        return (f"{kind} values must each be a ${{VAR}} reference to a key in ~/.hermes/.env, never a literal "
                f"value ({', '.join(map(str, bad))}). Ask the user to add the secret to ~/.hermes/.env first, then pass ${{NAME}}.")
    return None


def _build_entry(args: dict) -> tuple[Optional[dict], Optional[str]]:
    url = str(args.get("url") or "").strip()
    command = str(args.get("command") or "").strip()
    if bool(url) == bool(command):
        return None, "add needs exactly one of url (remote server) or command (local program)."
    entry: dict[str, Any] = {}
    if url:
        if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            return None, "url must be https:// (or http://localhost for a server on this computer)."
        if args.get("env"):
            return None, "env is for local (command) servers only; remote servers take headers."
        err = _check_refs("headers", args.get("headers"))
        if err:
            return None, err
        entry["url"] = url
        if args.get("headers"):
            entry["headers"] = {str(k): str(v).strip() for k, v in args["headers"].items()}
    else:
        base = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in SHELLS:
            return None, ("A shell as the server command is refused from chat. If the user really wants this, "
                          "they can run `hermes mcp add` in Terminal.")
        cmd_args = args.get("args") or []
        if not isinstance(cmd_args, list) or not all(isinstance(a, (str, int, float)) for a in cmd_args):
            return None, "args must be a list of strings."
        if args.get("headers"):
            return None, "headers are for remote (url) servers only; local servers take env."
        err = _check_refs("env", args.get("env"))
        if err:
            return None, err
        entry["command"] = command
        if cmd_args:
            entry["args"] = [str(a) for a in cmd_args]
        if args.get("env"):
            entry["env"] = {str(k): str(v).strip() for k, v in args["env"].items()}
    entry["enabled"] = True
    return entry, None


def _entry_digest(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:8]


def _add(name: str, args: dict) -> dict[str, Any]:
    entry, err = _build_entry(args)
    if err:
        return {"ok": False, "error": err}
    cfg = common.read_config()
    servers = cfg.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        return {"ok": False, "error": "mcp_servers in config.yaml is not a mapping; fix it by hand first."}
    if name in servers:
        return {"ok": False, "error": f"A server named {name!r} already exists. Use enable/reconnect, or ask "
                                     "the user to change it with `hermes mcp add` in Terminal."}
    from hermes_cli.mcp_security import validate_mcp_server_entry
    issues = validate_mcp_server_entry(name, entry)
    if issues:
        return {"ok": False, "error": "Refused by Hermes' MCP safety check: " + "; ".join(issues)}
    servers[name] = entry
    common.save_config(cfg)
    try:
        from hermes_cli.mcp_catalog import record_mcp_install
        record_mcp_install("url" if entry.get("url") else "local", None, "success")
    except Exception:
        pass
    return _result(name, _apply_now(), "Added")


def _result(name: str, applied: dict, verb: str) -> dict[str, Any]:
    row = (_status(name) or [{"name": name, "status": "unknown"}])[0]
    out: dict[str, Any] = {"ok": "error" not in applied, "server": row}
    if applied.get("error"):
        out["error"] = applied["error"]
    status = row.get("status")
    if applied.get("still_connecting") or status == "connecting":
        out["summary"] = f"{verb} {name}; it is still connecting. Check again with action=list in a minute."
    elif row.get("needs_auth"):
        out["summary"] = f"{verb} {name}, but it needs sign-in: use mcp_manager action=login name={name}."
    elif status == "failed":
        out["summary"] = f"{verb} {name}, but it did not connect: {row.get('error') or 'no error given'}."
    elif status == "disabled":
        out["summary"] = f"{verb} {name}. Open chats drop its tools on their next turn."
    else:
        out["summary"] = (f"{verb} {name}: {status}, {row.get('tools') or 0} tools. New chats have them; open "
                          "chats add them at the end of their tool list on their next turn.")
    return out


def _remove(name: str) -> dict[str, Any]:
    """Undo of Hermes' own add only: drop the entry, tear the server down, forget it in the ledger."""
    if not ledger.owned("mcp", name):
        return {"ok": False, "error": f"{name!r} was not added by Hermes; removing it is the user's call "
                                     f"(`hermes mcp remove {name}`)."}
    cfg = common.read_config()
    servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
    if name not in servers:
        ledger.drop("mcp", name)
        return {"ok": True, "unchanged": True, "summary": f"{name} was already gone."}
    servers.pop(name)
    common.save_config(cfg)
    applied = _apply_now()
    ledger.drop("mcp", name)
    out = {"ok": "error" not in applied, "removed": name,
           "summary": f"Removed {name}. Open chats drop its tools on their next turn."}
    if applied.get("error"):
        out["error"] = applied["error"]
    return out


def _login(name: str) -> dict[str, Any]:
    """The sign-in link for a remote server, through Hermes' own OAuth flow. The flow's callback is a
    loopback receiver on the computer running Hermes (or the server's pinned oauth.redirect_uri), so the
    link finishes in a browser on that computer unless the server pins a redirect_uri other devices can reach."""
    cfg = _servers_raw().get(name)
    if not isinstance(cfg, dict):
        return {"ok": False, "error": f"No MCP server named {name!r} (see action=list)."}
    if not cfg.get("url"):
        return {"ok": False, "error": f"{name!r} is a local (stdio) server; it doesn't use sign-in. It takes "
                                     "env keys (${VAR} in ~/.hermes/.env) instead."}
    from tools.connectors import mcp_oauth
    try:
        attempt = mcp_oauth.start(name)
    except TimeoutError:
        return {"ok": False, "error": f"{name} did not hand out a sign-in link within 30 s. Try action=reconnect, "
                                     "then login again."}
    except Exception as exc:
        fc = getattr(exc, "failure_class", None)
        return {"ok": False, "error": f"Sign-in for {name} failed{f' ({fc})' if fc else ''}: {common.clip(str(exc), 200)}"}
    if not attempt.auth_url:
        return {"ok": True, "signed_in": True,
                "summary": f"{name} is already signed in (the saved sign-in still works). Use action=reconnect "
                           "if its tools are missing."}
    back = str((cfg.get("oauth") or {}).get("redirect_uri") or attempt.flow.redirect_uri or "")
    local = back.startswith(("http://127.0.0.1", "http://localhost"))
    out = {"ok": True, "link": attempt.auth_url, "returns_to": back,
           "summary": (f"Sign-in link for {name}: give it to the user. "
                       + ("It returns to the computer running Hermes (" + back + "), so open it in a browser on that "
                          "computer. To sign in from another device, set oauth.redirect_uri on this server to an "
                          "address that device can reach. "
                          if local else "")
                       + "The link works for a few minutes; afterwards run action=list to see it connected.")}
    if attempt.detail:
        out["note"] = attempt.detail
    return out


# ------------------------------------------------------------------ tool + card

def needs_card(args: dict) -> bool:
    """add always asks; enable/disable/reconnect ask unless Hermes added that server itself."""
    action = str(args.get("action") or "")
    if action not in CHANGES:
        return False
    return action == "add" or not ledger.owned("mcp", str(args.get("name") or "").strip())


def tool(args: dict, session_id: Optional[str] = None, **_: Any) -> str:
    args = args or {}
    action = str(args.get("action") or "")
    if action not in CHANGES:
        try:
            if action == "list":
                rows = _status()
                own = set(ledger.names("mcp"))
                for r in rows:
                    r["made_by_hermes"] = r.get("name") in own
                return json.dumps({"servers": rows, "count": len(rows)})
            if action == "login":
                name = str(args.get("name") or "").strip()
                out = _login(name)
                ledger.log(TOOL, "login", name, chat=session_id, card=False,
                           result={k: v for k, v in out.items() if k != "link"})   # no live link in the log
                return json.dumps(out)
            return json.dumps({"error": f"unknown action {action!r}; use list, add, enable, disable, reconnect or login"})
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    name = str(args.get("name") or "").strip()
    card = needs_card(args)
    out = _change(action, name, args, session_id, card)
    out["change_id"] = ledger.log(TOOL, action, name, chat=session_id, card=card, result=out, undo=undo_of(action, name, out))
    return json.dumps(out)


def undo_of(action: str, name: str, out: dict) -> Optional[dict]:
    if action == "add" and out.pop("saved", False):
        return {"tool": TOOL, "args": {"action": "remove", "name": name}}
    if action in ("enable", "disable") and out.get("ok") and not out.get("unchanged"):
        return {"tool": TOOL, "args": {"action": "disable" if action == "enable" else "enable", "name": name}}
    return None   # reconnect has no undo


def _change(action: str, name: str, args: dict, session_id: Optional[str], card: bool) -> dict:
    try:
        if card:
            refusal = common.not_main(session_id)
            if refusal:
                return {"ok": False, "error": refusal}
        if not common.NAME_RE.match(name):
            return {"ok": False, "error": "name must be 1-48 lowercase letters, digits, - or _."}
        if action == "add":
            existed = name in _servers_raw()
            out = _add(name, args)
            if not existed and name in _servers_raw():   # only what this add saved, never the user's server
                ledger.add("mcp", name, session_id)
                out["saved"] = True
            return out
        if action == "remove":   # internal: reached only from undo
            return _remove(name)
        if action in ("enable", "disable"):
            return _set_enabled(name, action == "enable")
        if name not in _servers_raw():
            return {"ok": False, "error": f"No MCP server named {name!r} (see action=list)."}
        return _result(name, _reconnect(name), "Reconnected")
    except Exception as exc:
        log.warning("mcp_manager %s failed: %s", action, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def card(args: dict, session_id: Optional[str] = None) -> Optional[dict]:
    """pre_tool_call directive: nothing for list or for Hermes' own servers; otherwise block outside the
    main chat, else one approval card. The rule key names action + server (+ the exact entry for add),
    so "Always allow" covers only that."""
    if not needs_card(args):
        return None
    action = str(args.get("action") or "")
    refusal = common.not_main(session_id)
    if refusal:
        return {"action": "block", "message": refusal}
    name = str(args.get("name") or "").strip()
    reason = common.clip(args.get("reason") or "no reason given", 200)
    key = f"mcp-manager:{action}:{name or '?'}"
    if action == "add":
        entry, err = _build_entry(args)
        if err:
            return None   # the tool itself refuses with the reason; nothing to approve
        key += f":{_entry_digest(entry)}"
        where = ("connects to " if entry.get("url") else "runs this program on this computer: ") + _runs(entry)
        what = f"Add the MCP server {name!r}? It {where}. Its tools reach new chats now and open chats next turn."
    elif action == "enable":
        what = f"Turn ON the MCP server {name!r} now? Its tools are added at the end of open chats (cache kept)."
    elif action == "disable":
        what = (f"Turn OFF the MCP server {name!r} now? Open chats lose its tools next turn, which re-reads "
                "their prompt cache from where those tools sat (one pricier turn).")
    else:
        what = (f"Reconnect the MCP server {name!r} now? Its tools pause for a few seconds. Open chats keep "
                "their prompt cache if its tools come back the same; if they changed, the next turn re-reads "
                "the cache (one pricier turn).")
    return {"action": "approve", "rule_key": key, "message": f"{what} Reason: {reason}"}
