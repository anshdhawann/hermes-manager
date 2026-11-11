"""Live test of hermes-manager against REAL Hermes code in a throwaway HERMES_HOME (see livetest.sh).

Never touches ~/.hermes: livetest.sh points HOME, HERMES_HOME and HERMES_GATEWAY_LOCK_DIR at a temp dir
and this script refuses to run otherwise. Calls the plugin's tool functions directly (no approval card is
raised), checks the real pre_tool_call directive, real config writes, a real stdio MCP server, a real
skill and a real plugin load/rename/delete.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HERMES_HOME"])
assert "/.hermes" not in str(HOME) and str(HOME).startswith(("/tmp", "/var/folders", "/private")), HOME
results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))


def J(s):
    return json.loads(s)


# ---- throwaway home --------------------------------------------------------------------------------
(HOME / "plugins").mkdir(parents=True, exist_ok=True)
(HOME / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-manager\n")
con = sqlite3.connect(HOME / "state.db")
con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT, pinned INT, archived INT, source TEXT, "
            "profile_name TEXT, started_at REAL)")
con.execute("INSERT INTO sessions VALUES ('main', 1, 0, 'mobile-web', NULL, 1)")
con.commit(); con.close()
skill = HOME / "skills" / "demo-skill"
skill.mkdir(parents=True)
(skill / "SKILL.md").write_text("---\nname: demo-skill\ndescription: A demo skill for the live test\n---\n\nHi.\n")
server = HOME / "echo_server.py"
server.write_text("try:\n    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x\n"
                  "except ImportError:\n    from mcp.server.fastmcp import FastMCP  # mcp 1.x\n"
                  "m = FastMCP('echo')\n\n@m.tool()\n"
                  "def echo(text: str) -> str:\n    return text\n\nm.run()\n")
import shutil  # noqa: E402
shutil.copytree(Path(sys.argv[1]), HOME / "plugins" / "hermes-manager",
                ignore=shutil.ignore_patterns("__pycache__", "tests"))

# ---- load through Hermes' own plugin manager ------------------------------------------------------
from hermes_cli.plugins import discover_plugins, get_plugin_manager, _get_pre_tool_call_directive_details  # noqa: E402
from tools.registry import registry  # noqa: E402

discover_plugins(force=True)
p = get_plugin_manager()._plugins.get("hermes-manager")
check("hermes-manager loads through Hermes' PluginManager", p is not None and not getattr(p, "error", None),
      getattr(p, "error", "missing"))
hm = p.module
for t in ("plugin_manager", "skill_manager", "mcp_manager"):
    check(f"tool {t} is registered", registry.get_entry(t) is not None)


def directive(tool, args, sid):
    d = _get_pre_tool_call_directive_details(tool, args, session_id=sid)
    return d.action, d.message or "", d.rule_key or ""


# ---- permissions through the real hook chain ------------------------------------------------------
a, m, k = directive("skill_manager", {"action": "disable", "name": "demo-skill"}, "main")
check("user's skill: approval card in the main chat", a == "approve" and k == "skill-manager:disable:demo-skill", (a, k))
a, m, _ = directive("skill_manager", {"action": "disable", "name": "demo-skill"}, "thread-x")
check("user's skill: blocked outside the main chat", a == "block" and "main chat" in m, (a, m))
check("list needs no card", directive("mcp_manager", {"action": "list"}, "thread-x")[0] is None)

# ---- skills -----------------------------------------------------------------------------------------
import yaml  # noqa: E402
rows = {r["name"]: r for r in J(hm.skill_manager.tool({"action": "list"}))["skills"]}
check("skill list shows demo-skill on", rows.get("demo-skill", {}).get("enabled") is True, list(rows)[:5])
out = J(hm.skill_manager.tool({"action": "disable", "name": "demo-skill"}, session_id="main"))
cfg = yaml.safe_load((HOME / "config.yaml").read_text())
check("disable writes skills.disabled in config.yaml", out.get("ok") and "demo-skill" in (cfg.get("skills") or {}).get("disabled", []), out)
from tools.skills_tool import find_all_skills  # noqa: E402
check("Hermes no longer offers the disabled skill", "demo-skill" not in {s["name"] for s in find_all_skills()})
out = J(hm.skill_manager.tool({"action": "enable", "name": "demo-skill"}, session_id="main"))
check("enable brings it back", out.get("ok") and "demo-skill" in {s["name"] for s in find_all_skills()}, out)
check("plugins.enabled survived the skills write",
      "hermes-manager" in (yaml.safe_load((HOME / "config.yaml").read_text())["plugins"]["enabled"]))

# ---- MCP: a real stdio server ---------------------------------------------------------------------
add = {"action": "add", "name": "echo", "command": sys.executable, "args": [str(server)]}
a, m, k = directive("mcp_manager", add, "main")
check("MCP add: card shows the exact command", a == "approve" and str(server) in m and k.startswith("mcp-manager:add:echo:"), m)
out = J(hm.mcp_manager.tool(add, session_id="main"))
srv = out.get("server") or {}
check("MCP add connects a real server", out.get("ok") and srv.get("status") == "connected" and srv.get("tools", 0) >= 1, out)
names = [n for n in registry.get_all_tool_names() if "echo" in n] if hasattr(registry, "get_all_tool_names") else []
check("its tool is in Hermes' registry", any(n.startswith("mcp") for n in names), names)
check("Hermes-added server: no card, any chat", directive("mcp_manager", {"action": "disable", "name": "echo"}, "thread-x")[0] is None)
out = J(hm.mcp_manager.tool({"action": "disable", "name": "echo"}, session_id="thread-x"))
check("MCP disable tears it down", out.get("ok") and out["server"].get("status") == "disabled", out)
out = J(hm.mcp_manager.tool({"action": "enable", "name": "echo"}, session_id="thread-x"))
check("MCP enable reconnects it", out.get("ok") and out["server"].get("status") == "connected", out)
out = J(hm.mcp_manager.tool({"action": "reconnect", "name": "echo"}, session_id="thread-x"))
check("MCP reconnect comes back connected", out.get("ok") and out["server"].get("status") == "connected", out)
out = J(hm.mcp_manager.tool({"action": "add", "name": "leaky", "command": "npx", "env": {"K": "sk-123"}}, session_id="main"))
check("MCP add refuses a literal secret", out.get("ok") is False and "reference" in out.get("error", ""), out)

# ---- plugins: create, load, rename, delete --------------------------------------------------------
out = J(hm._tool({"action": "create", "name": "helper", "description": "Live test helper"}, session_id="thread-x"))
check("create makes an empty plugin", out.get("ok") and (HOME / "plugins" / "helper" / "plugin.yaml").exists(), out)
(HOME / "plugins" / "helper" / "__init__.py").write_text(
    "def register(ctx):\n    ctx.register_tool(name='helper_ping', toolset='helper', handler=lambda a, **k: 'pong',\n"
    "        schema={'name': 'helper_ping', 'description': 'ping', 'parameters': {'type': 'object', 'properties': {}}})\n")
check("enabling Hermes' own plugin needs no card", directive("plugin_manager", {"action": "enable", "name": "helper"}, "thread-x")[0] is None)
out = J(hm._tool({"action": "enable", "name": "helper"}, session_id="thread-x"))
discover_plugins(force=True)
hm = get_plugin_manager()._plugins["hermes-manager"].module
check("enabled plugin loads and registers its tool", registry.get_entry("helper_ping") is not None, out)
a, m, k = directive("plugin_manager", {"action": "rename", "name": "hermes-manager", "new_name": "x"}, "main")
out = J(hm._tool({"action": "rename", "name": "hermes-manager", "new_name": "xx"}, session_id="main"))
check("manager refuses to rename itself", out.get("ok") is False, out)
check("renaming even Hermes' own plugin needs a card",
      directive("plugin_manager", {"action": "rename", "name": "helper", "new_name": "helper-two"}, "main")[0] == "approve")
out = J(hm._tool({"action": "rename", "name": "helper", "new_name": "helper-two"}, session_id="main"))
discover_plugins(force=True)
hm = get_plugin_manager()._plugins["hermes-manager"].module
cfg = yaml.safe_load((HOME / "config.yaml").read_text())
check("rename moves folder + config and it loads under the new name",
      out.get("ok") and (HOME / "plugins" / "helper-two").exists() and "helper-two" in cfg["plugins"]["enabled"]
      and "helper" not in cfg["plugins"]["enabled"] and "helper-two" in get_plugin_manager()._plugins, out)
check("tool still registered after rename", registry.get_entry("helper_ping") is not None)
out = J(hm._tool({"action": "delete", "name": "helper-two"}, session_id="thread-x"))
discover_plugins(force=True)
hm = get_plugin_manager()._plugins["hermes-manager"].module
check("delete moves it to a backup and unloads it", out.get("ok") and not (HOME / "plugins" / "helper-two").exists()
      and Path(out.get("backup", "/nope")).exists() and registry.get_entry("helper_ping") is None, out)

# ---- undo and sign-in ----------------------------------------------------------------------------
def undo(cid, sid="main"):
    return J(hm._tool({"action": "undo", "id": cid}, session_id=sid))


out = J(hm.skill_manager.tool({"action": "disable", "name": "demo-skill"}, session_id="main"))
u = undo(out["change_id"])
check("undo of a skill disable turns it back on", u.get("ok") and "demo-skill" in {s["name"] for s in find_all_skills()}, u)
out = J(hm.mcp_manager.tool({"action": "disable", "name": "echo"}, session_id="thread-x"))
u = undo(out["change_id"], "thread-x")
srv = {r["name"]: r for r in J(hm.mcp_manager.tool({"action": "list"}))["servers"]}.get("echo", {})
check("undo of an MCP disable reconnects it", u.get("ok") and srv.get("status") == "connected", (u, srv))
out = J(hm.mcp_manager.tool({"action": "login", "name": "echo"}, session_id="thread-x"))
check("login on a stdio server says it doesn't use sign-in", out.get("ok") is False and "doesn't use sign-in" in out.get("error", ""), out)
out = J(hm.mcp_manager.tool({"action": "login", "name": "nope"}, session_id="thread-x"))
check("login on a missing server is a clear error", out.get("ok") is False and "No MCP server" in out.get("error", ""), out)
add_id = next(r["id"] for r in reversed(J(hm._tool({"action": "log"}))["changes"])
              if r["tool"] == "mcp_manager" and r["action"] == "add" and r["target"] == "echo" and r.get("ok"))
u = undo(add_id, "thread-x")
cfg = yaml.safe_load((HOME / "config.yaml").read_text())
listed = {r["name"] for r in J(hm.mcp_manager.tool({"action": "list"}))["servers"]}
check("undo of an MCP add removes it from config and status",
      u.get("ok") and "echo" not in (cfg.get("mcp_servers") or {}) and "echo" not in listed, (u, listed))
out = J(hm._tool({"action": "create", "name": "scratch-plugin"}, session_id="thread-x"))
u = undo(out["change_id"], "thread-x")
check("undo of a create deletes the plugin", u.get("ok") and not (HOME / "plugins" / "scratch-plugin").exists(), u)
del_id = next(r["id"] for r in reversed(J(hm._tool({"action": "log"}))["changes"])
              if r["action"] == "delete" and r["target"] == "helper-two" and r.get("ok"))
u = undo(del_id, "thread-x")
discover_plugins(force=True)
hm = get_plugin_manager()._plugins["hermes-manager"].module
cfg = yaml.safe_load((HOME / "config.yaml").read_text())
check("undo of a delete restores the plugin and it loads again",
      u.get("ok") and (HOME / "plugins" / "helper-two" / "plugin.yaml").exists() and "helper-two" in cfg["plugins"]["enabled"]
      and registry.get_entry("helper_ping") is not None, u)
check("undoing the same change twice is refused", "already undone" in undo(del_id, "thread-x").get("error", ""))
log_text = __import__("asyncio").run(hm._log_slash(""))
check("/manager-log shows ids and undone marks", del_id in log_text and "[undone]" in log_text, log_text[:300])

# ---- guards and log -------------------------------------------------------------------------------
a, m, _ = directive("write_file", {"path": str(HOME / "hermes-manager" / "ledger.json"), "content": "{}"}, "main")
check("writing the ledger by hand is blocked", a == "block", (a, m))
a, m, _ = directive("terminal", {"command": "~/.hermes/hermes-agent/venv/bin/pip install numpy"}, "main")
check("pip install into ~/.hermes needs a card", a == "approve", (a, m))
log = J(hm._tool({"action": "log"}))["changes"]
check("every change is in the log", len(log) >= 10 and {"skill_manager", "mcp_manager", "plugin_manager"} <= {r["tool"] for r in log},
      len(log))

try:
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    shutdown_mcp_servers()
except Exception:
    pass
passed = sum(ok for _, ok in results)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
