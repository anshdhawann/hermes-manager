"""`python3 -I tests/test_hermes_manager.py` (plain asserts; Hermes modules stubbed, temp HERMES_HOME)."""
import asyncio
import copy
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp())
(HOME / "plugins").mkdir()
CONFIG: dict = {}
calls: list = []
MCP_STATUS: list = []
PLUGINS: list = []   # (name, version, description, source, dir, key)
SKILLS = [{"name": "notes", "description": "Take notes", "category": "productivity"},
          {"name": "hermes-agent", "description": "Core", "category": "core"},
          {"name": "my-skill", "description": "Made by Hermes", "category": None}]


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


@contextmanager
def nullctx():
    yield


def toggle(name, enabled):
    calls.append(("toggle", name, enabled))
    lst = CONFIG.setdefault("plugins", {}).setdefault("enabled", [])
    if enabled and name not in lst:
        lst.append(name)
    if not enabled and name in lst:
        lst.remove(name)
        return {"ok": True, "name": name, "unchanged": False}
    return {"ok": True, "name": name, "unchanged": not enabled, "activation": {}, "restart_required": False}


def reconcile():
    calls.append("reconcile")
    return {"removed": [], "added": [], "pending": []}


def save_disabled(cfg, disabled, platform=None):
    cfg.setdefault("skills", {})["disabled"] = sorted(disabled)
    CONFIG.clear(); CONFIG.update(copy.deepcopy(cfg))


for pkg in ("hermes_cli", "tools", "agent"):
    mod(pkg)
mod("hermes_constants", get_hermes_home=lambda: str(HOME))
mod("hermes_cli.config", read_raw_config=lambda: copy.deepcopy(CONFIG),
    save_config=lambda c: (CONFIG.clear(), CONFIG.update(copy.deepcopy(c))))
mod("hermes_cli.plugins_cmd", _discover_all_plugins=lambda: list(PLUGINS), dashboard_set_agent_plugin_enabled=toggle)
mod("hermes_cli.plugins_activation", reload_running_gateway=lambda: (calls.append("gateway"),
                                                                     {"reloaded": True, "plugins": ["a"]})[1],
    notify_serve_backend=lambda name, home: None)
mod("hermes_cli.plugins", get_plugin_manager=lambda: types.SimpleNamespace(list_plugins=lambda: []))
mod("hermes_cli.skills_config",
    get_disabled_skills=lambda cfg, platform=None: set((cfg.get("skills") or {}).get("disabled") or []) - {"hermes-agent"},
    save_disabled_skills=save_disabled)
mod("hermes_cli.mcp_config", redact_mcp_probe_text=lambda t: str(t).replace("sekrit", "***"))
mod("hermes_cli.mcp_security", validate_mcp_server_entry=lambda n, e: ["IOC"] if "evil" in json.dumps(e) else [])
mod("hermes_cli.mcp_catalog", record_mcp_install=lambda *a, **k: None)
mod("tools.skills_tool", find_all_skills=lambda skip_disabled=False: [dict(s) for s in SKILLS],
    clear_skills_cache=lambda: calls.append("skills-cache"))
mod("agent.skill_commands", reload_skills=lambda: (calls.append("reload-skills"), {"added": [], "removed": [], "total": 3})[1])
mod("agent.prompt_builder", clear_skills_system_prompt_cache=lambda clear_snapshot=False: None)
mod("agent.skill_utils", ESSENTIAL_SKILLS={"hermes-agent"})
mod("tools.mcp_tool_discovery", get_mcp_status=lambda: [dict(s) for s in MCP_STATUS],
    reconcile_mcp_servers_with_config=reconcile, discover_mcp_tools=lambda: calls.append("discover") or [],
    _clear_connect_failure=lambda n: calls.append(("clear", n)))
mod("tools.mcp_tool_lifecycle", shutdown_mcp_servers=lambda scope=None, names=None: calls.append(("shutdown", names)))
mod("tools.mcp_tool", _mcp_registry_scope=lambda: None)
mod("tools.mcp_oauth", suppress_interactive_oauth=nullctx)
mod("tools.mcp_tool_common", mcp_server_enabled=lambda cfg: cfg.get("enabled", True) is not False)

spec = importlib.util.spec_from_file_location("hermes_manager", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
hm = importlib.util.module_from_spec(spec)
sys.modules["hermes_manager"] = hm
spec.loader.exec_module(hm)
from hermes_manager import ledger, mcp_manager, skill_manager  # noqa: E402

con = sqlite3.connect(HOME / "state.db")
con.execute("CREATE TABLE sessions (id TEXT, pinned INT, archived INT, source TEXT, profile_name TEXT, started_at REAL, "
            "title TEXT)")
con.execute("INSERT INTO sessions VALUES ('main', 1, 0, 'mobile-web', NULL, 1, NULL)")
con.execute("INSERT INTO sessions VALUES ('thread', 0, 0, 'mobile-web', NULL, 2, NULL)")
con.execute("CREATE TABLE session_admissions (target_session_id TEXT, status TEXT)")
con.execute("CREATE TABLE worker_executions (session_id TEXT, status TEXT)")
con.commit(); con.close()


def J(s):
    return json.loads(s)


def trust(on):
    """plugins.entries.hermes-manager.settings.trust_own_items (None removes it: the default)."""
    entries = CONFIG.setdefault("plugins", {}).setdefault("entries", {})
    if on is None:
        entries.pop("hermes-manager", None)
    else:
        entries["hermes-manager"] = {"settings": {"trust_own_items": on}}


def make_plugin(key, body="def register(ctx):\n    pass\n", manifest=None):
    d = HOME / "plugins" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(manifest or f"name: {key}\nversion: 1\n")
    (d / "__init__.py").write_text(body)
    PLUGINS.append((key, "1", "x", "user", str(d), key))
    return d


def test_register_adds_three_tools_two_hooks_and_commands():
    seen = {"tools": [], "hooks": [], "slash": []}

    class Ctx:
        def register_command(self, name, handler, **kw): seen["slash"].append(name)
        def register_cli_command(self, name, help, setup_fn, handler_fn): seen["cli"] = name
        def register_tool(self, name, toolset, schema, handler, **kw): seen["tools"].append(name)
        def register_hook(self, name, fn): seen["hooks"].append(name)
    hm.register(Ctx())
    assert seen["tools"] == ["plugin_manager", "skill_manager", "mcp_manager"]
    assert seen["hooks"] == ["pre_tool_call", "post_tool_call"]
    assert seen["slash"] == ["reload-plugins", "manager-log", "manager-undo"] and seen["cli"] == "reload-plugins"


def test_reads_need_no_card_anywhere():
    for tool, args in (("plugin_manager", {"action": "list"}), ("plugin_manager", {"action": "log"}),
                       ("plugin_manager", {"action": "reload"}), ("skill_manager", {"action": "list"}),
                       ("mcp_manager", {"action": "list"})):
        assert hm._pre_tool_call(tool_name=tool, args=args, session_id="thread") is None, (tool, args)


def test_users_things_need_a_card_in_the_main_chat_with_per_item_keys():
    make_plugin("safe-save")
    CONFIG["mcp_servers"] = {"github": {"command": "npx", "args": ["gh"], "env": {"TOKEN": "${GH}"}}}
    keys = set()
    for tool, args in (("plugin_manager", {"action": "disable", "name": "safe-save"}),
                       ("plugin_manager", {"action": "enable", "name": "safe-save"}),
                       ("skill_manager", {"action": "disable", "name": "notes"}),
                       ("mcp_manager", {"action": "disable", "name": "github"}),
                       ("mcp_manager", {"action": "reconnect", "name": "github"})):
        r = hm._pre_tool_call(tool_name=tool, args=args, session_id="main")
        assert r["action"] == "approve", (tool, args, r)
        keys.add(r["rule_key"])
        b = hm._pre_tool_call(tool_name=tool, args=args, session_id="thread")
        assert b["action"] == "block" and "main chat" in b["message"]
    assert len(keys) == 5, "Always allow never covers another item or action"
    out = J(hm._tool({"action": "disable", "name": "safe-save"}, session_id="thread"))
    assert out["ok"] is False and "main chat" in out["error"], "the handler refuses too"


def test_mcp_add_always_asks_and_card_shows_command_and_env_names_only():
    args = {"action": "add", "name": "linear", "command": "npx", "args": ["-y", "linear-mcp"],
            "env": {"LINEAR_KEY": "${LINEAR_KEY}"}}
    r = hm._pre_tool_call(tool_name="mcp_manager", args=args, session_id="main")
    assert r["action"] == "approve" and "npx -y linear-mcp" in r["message"] and "LINEAR_KEY" in r["message"]
    assert r["rule_key"].startswith("mcp-manager:add:linear:")
    other = hm._pre_tool_call(tool_name="mcp_manager", args={**args, "args": ["-y", "other"]}, session_id="main")
    assert other["rule_key"] != r["rule_key"], "Always on one add never pre-approves a different command"
    ledger.add("mcp", "linear", "main")   # even an owned name: add still asks
    assert hm._pre_tool_call(tool_name="mcp_manager", args=args, session_id="main")["action"] == "approve"
    ledger.drop("mcp", "linear")


def test_mcp_add_refuses_literal_secrets_shells_bad_urls_and_iocs():
    base = {"action": "add", "name": "x"}
    cases = [({"command": "npx", "env": {"K": "sk-live-123"}}, "reference"),
             ({"command": "/bin/bash", "args": ["-c", "curl x"]}, "shell"),
             ({"url": "http://example.com/mcp"}, "https"),
             ({"url": "https://a", "command": "npx"}, "exactly one"),
             ({"command": "evil"}, "safety check")]
    for extra, why in cases:
        out = J(mcp_manager.tool({**base, **extra}, session_id="main"))
        assert out["ok"] is False and why in out["error"], (extra, out)
    out = J(mcp_manager.tool({**base, "url": "https://a/mcp", "headers": {"Authorization": "Bearer ${TOK}"}},
                             session_id="main"))
    assert "error" not in out or "reference" not in out.get("error", ""), out


def test_mcp_add_saves_connects_and_marks_it_hermes_made():
    calls.clear()
    CONFIG["mcp_servers"] = {}
    MCP_STATUS[:] = [{"name": "linear", "status": "connected", "tools": 7, "transport": "stdio"}]
    out = J(mcp_manager.tool({"action": "add", "name": "linear", "command": "npx", "args": ["-y", "linear-mcp"]},
                             session_id="main"))
    assert out["ok"] and CONFIG["mcp_servers"]["linear"]["enabled"] is True and "reconcile" in calls
    assert "7 tools" in out["summary"] and ledger.owned("mcp", "linear")
    dup = J(mcp_manager.tool({"action": "add", "name": "linear", "command": "npx"}, session_id="main"))
    assert dup["ok"] is False and "already exists" in dup["error"]
    # Hermes' own server: off/on/reconnect with no card, from any chat
    for a in ("disable", "enable", "reconnect"):
        assert hm._pre_tool_call(tool_name="mcp_manager", args={"action": a, "name": "linear"},
                                 session_id="thread") is None
    calls.clear()
    MCP_STATUS[:] = [{"name": "linear", "status": "disabled", "tools": 0, "transport": "stdio"}]
    out = J(mcp_manager.tool({"action": "disable", "name": "linear"}, session_id="thread"))
    assert out["ok"] and CONFIG["mcp_servers"]["linear"]["enabled"] is False and "drop its tools" in out["summary"]
    calls.clear()
    MCP_STATUS[:] = [{"name": "linear", "status": "connected", "tools": 7, "transport": "stdio"}]
    out = J(mcp_manager.tool({"action": "reconnect", "name": "linear"}, session_id="thread"))
    assert ("shutdown", {"linear"}) in calls and ("clear", "linear") in calls and "discover" in calls


def test_mcp_list_redacts_and_shows_env_names_not_values():
    CONFIG["mcp_servers"] = {"gh": {"url": "https://x/mcp?key=sekrit", "headers": {"X-Key": "${K}"}}}
    MCP_STATUS[:] = [{"name": "gh", "status": "failed", "tools": 0, "transport": "http",
                      "error": "401 sekrit", "needs_auth": True}]
    row = J(mcp_manager.tool({"action": "list"}))["servers"][0]
    assert "sekrit" not in json.dumps(row) and row["needs_auth"] and "X-Key" in row["runs"]


def test_skills_toggle_uses_hermes_switch_reloads_and_guards_essentials():
    calls.clear()
    CONFIG.pop("skills", None)
    out = J(skill_manager.tool({"action": "disable", "name": "notes"}, session_id="main"))
    assert out["ok"] and CONFIG["skills"]["disabled"] == ["notes"] and "reload-skills" in calls
    rows = {r["name"]: r for r in J(skill_manager.tool({"action": "list"}))["skills"]}
    assert rows["notes"]["enabled"] is False and rows["hermes-agent"]["essential"]
    assert J(skill_manager.tool({"action": "list", "state": "disabled"}))["count"] == 1
    out = J(skill_manager.tool({"action": "disable", "name": "hermes-agent"}, session_id="main"))
    assert out["ok"] is False and "essential" in out["error"]
    assert J(skill_manager.tool({"action": "enable", "name": "nope"}, session_id="main"))["ok"] is False
    assert J(skill_manager.tool({"action": "enable", "name": "notes"}, session_id="main"))["ok"]
    assert CONFIG["skills"]["disabled"] == []


def test_skills_hermes_creates_are_its_own_no_card():
    hm._post_tool_call(tool_name="skill_manage", args={"action": "create", "name": "my-skill"},
                       result=json.dumps({"success": True}), session_id="thread")
    assert ledger.owned("skill", "my-skill")
    hm._post_tool_call(tool_name="skill_manage", args={"action": "create", "name": "staged"},
                       result=json.dumps({"success": True, "staged": True}), session_id="thread")
    assert not ledger.owned("skill", "staged"), "a staged (not yet approved) write is not Hermes' yet"
    assert hm._pre_tool_call(tool_name="skill_manager", args={"action": "disable", "name": "my-skill"},
                             session_id="thread") is None
    assert J(skill_manager.tool({"action": "disable", "name": "my-skill"}, session_id="thread"))["ok"]


def test_plugin_create_enable_delete_own_without_card_when_trusted():
    calls.clear()
    trust(True)
    out = J(hm._tool({"action": "create", "name": "helper", "description": "Helps"}, session_id="thread"))
    assert out["ok"] and (HOME / "plugins" / "helper" / "plugin.yaml").exists() and ledger.owned("plugin", "helper")
    assert "created_by: hermes" in (HOME / "plugins" / "helper" / "plugin.yaml").read_text()
    PLUGINS.append(("helper", "0.1.0", "Helps", "user", str(HOME / "plugins" / "helper"), "helper"))
    for a in ("enable", "disable", "delete"):
        assert hm._pre_tool_call(tool_name="plugin_manager", args={"action": a, "name": "helper"},
                                 session_id="thread") is None, a
    assert J(hm._tool({"action": "enable", "name": "helper"}, session_id="thread"))["ok"]
    assert ("toggle", "helper", True) in calls
    assert J(hm._tool({"action": "create", "name": "helper"}, session_id="thread"))["ok"] is False
    out = J(hm._tool({"action": "delete", "name": "helper"}, session_id="thread"))
    assert out["ok"] and not (HOME / "plugins" / "helper").exists() and Path(out["backup"]).exists()
    assert not ledger.owned("plugin", "helper")
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "helper"]
    trust(None)


def test_by_default_turning_on_hermes_own_plugin_asks_in_any_chat_and_pins_the_code():
    trust(None)
    CONFIG["plugins"]["enabled"] = []
    out = J(hm._tool({"action": "create", "name": "fresh", "description": "Does a thing"}, session_id="thread"))
    assert out["ok"], out
    folder = HOME / "plugins" / "fresh"
    PLUGINS.append(("fresh", "0.1.0", "Does a thing", "user", str(folder), "fresh"))
    assert hm._pre_tool_call(tool_name="write_file", args={"path": str(folder / "__init__.py"), "content": "x"}) is None, \
        "writing a Hermes-made plugin that is still off needs no card"
    r = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "enable", "name": "fresh"}, session_id="thread")
    assert r["action"] == "approve", "a card, and not a main-chat block: any chat may ask"
    assert "Hermes made it: Does a thing" in r["message"] and "__init__.py" in r["message"] and "plugin.yaml" in r["message"]
    assert r["rule_key"].startswith("plugin-manager:enable:fresh:") and "exact code" in r["message"]
    (folder / "__init__.py").write_text("def register(ctx):\n    print('changed')\n")
    r2 = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "enable", "name": "fresh"}, session_id="thread")
    assert r2["rule_key"] != r["rule_key"], "Always allow never covers code that changed after the card"
    assert J(hm._tool({"action": "enable", "name": "fresh"}, session_id="thread"))["ok"], \
        "after the card the tool runs from that chat"
    r = hm._pre_tool_call(tool_name="write_file", args={"path": str(folder / "__init__.py"), "content": "y"})
    assert r["action"] == "approve" and "a plugin Hermes made that is turned on" in r["message"]
    r = hm._pre_tool_call(tool_name="terminal", args={"command": f"sed -i '' s/a/b/ {folder}/__init__.py"})
    assert r["action"] == "approve"
    for a in ("disable", "delete"):
        assert hm._pre_tool_call(tool_name="plugin_manager", args={"action": a, "name": "fresh"},
                                 session_id="thread") is None, f"{a} of Hermes' own plugin runs no new code: no card"
    de = J(hm._tool({"action": "delete", "name": "fresh"}, session_id="thread"))
    assert de["ok"] and de["was_enabled"]
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "fresh"]
    r = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "undo", "id": de["change_id"]}, session_id="thread")
    assert r["action"] == "approve" and "turn it ON again" in r["message"], "undoing the delete turns code on: it asks"
    J(hm._tool({"action": "undo", "id": de["change_id"]}, session_id="thread"))
    PLUGINS.append(("fresh", "0.1.0", "Does a thing", "user", str(folder), "fresh"))
    trust(False)   # anything but a real true counts as off
    assert hm._pre_tool_call(tool_name="plugin_manager", args={"action": "enable", "name": "fresh"}, session_id="thread")
    CONFIG["plugins"]["entries"]["hermes-manager"] = {"settings": {"trust_own_items": "yes"}}
    assert hm._pre_tool_call(tool_name="plugin_manager", args={"action": "enable", "name": "fresh"}, session_id="thread")
    trust(True)
    assert hm._pre_tool_call(tool_name="plugin_manager", args={"action": "enable", "name": "fresh"}, session_id="thread") is None
    trust(None)
    J(hm._tool({"action": "delete", "name": "fresh"}, session_id="thread"))
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "fresh"]


def test_changing_the_trust_setting_by_hand_asks():
    r = hm._pre_tool_call(tool_name="terminal",
                          args={"command": "hermes config set plugins.entries.hermes-manager.settings.trust_own_items true"})
    assert r["action"] == "approve" and "trust_own_items" in r["message"]
    r = hm._pre_tool_call(tool_name="patch", args={"path": str(HOME / "config.yaml"), "new_string": "trust_own_items: true"})
    assert r["action"] == "approve"
    assert hm._pre_tool_call(tool_name="terminal", args={"command": "grep trust_own_items ~/.hermes/config.yaml"}) is None


def test_enabling_a_plugin_with_capabilities_says_they_stay_off():
    make_plugin("capy", manifest="name: capy\ncapabilities: [platform_actions]\n")
    out = J(hm._tool({"action": "enable", "name": "capy"}, session_id="main"))
    assert out["ok"] and "hermes plugins capabilities capy" in out["summary"], out


def test_own_plugin_asking_for_env_or_packages_still_needs_a_card():
    make_plugin("keyed", manifest="name: keyed\nrequires_env: [OPENAI_API_KEY]\n")
    ledger.add("plugin", "keyed")
    r = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "enable", "name": "keyed"}, session_id="main")
    assert r["action"] == "approve" and "OPENAI_API_KEY" in r["message"]
    make_plugin("pippy", manifest="name: pippy\npip_dependencies: [numpy]\n")
    ledger.add("plugin", "pippy")
    r = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "enable", "name": "pippy"}, session_id="main")
    assert r["action"] == "approve" and "internet" in r["message"]


def test_cannot_delete_users_plugins_or_touch_itself():
    make_plugin("pages")
    out = J(hm._tool({"action": "delete", "name": "pages"}, session_id="main"))
    assert out["ok"] is False and "hermes plugins remove" in out["error"] and (HOME / "plugins" / "pages").exists()
    for a in ("disable", "delete", "rename"):
        for n in ("hermes-manager", "reload-plugins"):
            out = J(hm._tool({"action": a, "name": n, "new_name": "zz"}, session_id="main"))
            assert out["ok"] is False and "hermes-manager provides" in out["error"], (a, n, out)


def test_rename_moves_folder_config_settings_data_and_approvals_with_backup():
    d = make_plugin("triggers", body="DATA = 'plugin-data/triggers'\ndef register(ctx):\n    pass\n",
                    manifest="name: triggers\nversion: 0.4\n")
    (HOME / "plugin-data" / "triggers").mkdir(parents=True)
    (HOME / "plugin-data" / "triggers" / "db.json").write_text("{}")
    CONFIG["plugins"] = {"enabled": ["pages", "triggers"], "entries": {"triggers": {"settings": {"x": 1}}}}
    CONFIG["command_allowlist"] = ["plugin-manager:disable:triggers", "plugin-manager:disable:pages", "rm -rf"]
    args = {"action": "rename", "name": "triggers", "new_name": "when-then"}
    card = hm._pre_tool_call(tool_name="plugin_manager", args=args, session_id="main")
    assert card["action"] == "approve" and "plugin-data/triggers" in card["message"]
    assert "1 saved approval" in card["message"] and "Warning" in card["message"] and "__init__.py:1" in card["message"]
    calls.clear()
    out = J(hm._tool(args, session_id="main"))
    assert out["ok"], out
    new = HOME / "plugins" / "when-then"
    assert new.exists() and not d.exists() and "name: when-then" in (new / "plugin.yaml").read_text()
    assert (HOME / "plugin-data" / "when-then" / "db.json").exists()
    assert CONFIG["plugins"]["enabled"] == ["pages", "when-then"]
    assert CONFIG["plugins"]["entries"] == {"when-then": {"settings": {"x": 1}}}
    assert CONFIG["command_allowlist"] == ["plugin-manager:disable:when-then", "plugin-manager:disable:pages", "rm -rf"]
    assert "gateway" in calls and out["check_code"] and Path(out["backup"], "config-before.json").exists()
    PLUGINS.append(("when-then", "0.4", "x", "user", str(new), "when-then"))
    out = J(hm._tool({"action": "rename", "name": "when-then", "new_name": "pages"}, session_id="main"))
    assert out["ok"] is False and "already exists" in out["error"]


def test_every_rename_asks_even_for_hermes_own_plugin():
    make_plugin("mine"); ledger.add("plugin", "mine")
    r = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "rename", "name": "mine", "new_name": "mine-2"},
                          session_id="main")
    assert r["action"] == "approve"
    b = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "rename", "name": "mine", "new_name": "mine-2"},
                          session_id="thread")
    assert b["action"] == "block"


def test_bundled_plugins_cannot_be_renamed():
    PLUGINS.append(("web", "1", "x", "bundled", "/b/web", "web"))
    out = J(hm._tool({"action": "rename", "name": "web", "new_name": "web2"}, session_id="main"))
    assert out["ok"] is False and "bundled" in out["error"]


def test_every_change_is_logged_and_log_reads_it():
    rows = J(hm._tool({"action": "log"}))["changes"]
    acts = {(r["tool"], r["action"]) for r in rows}
    assert {("plugin_manager", "create"), ("plugin_manager", "rename"), ("mcp_manager", "add"),
            ("skill_manager", "disable"), ("skill_manage", "create")} <= acts
    assert any(r["card"] for r in rows) and any(not r["card"] for r in rows)
    text = asyncio.run(hm._log_slash(""))
    assert "rename" in text and "(card)" in text


def test_ledger_cannot_be_edited_by_file_or_shell_tools():
    p = str(HOME / "hermes-manager" / "ledger.json")
    assert hm._pre_tool_call(tool_name="write_file", args={"path": p, "content": "{}"})["action"] == "block"
    assert hm._pre_tool_call(tool_name="terminal", args={"command": f"echo x > {p}"})["action"] == "block"
    assert hm._pre_tool_call(tool_name="terminal", args={"command": f"cat {p}"}) is None


def test_installing_code_into_hermes_needs_a_card_elsewhere_not():
    r = hm._pre_tool_call(tool_name="terminal",
                          args={"command": "~/.hermes/hermes-agent/venv/bin/pip install numpy"})
    assert r["action"] == "approve" and "numpy" in r["message"]
    r2 = hm._pre_tool_call(tool_name="terminal", args={"command": "git clone https://x/y", "workdir": "~/.hermes/plugins"})
    assert r2["action"] == "approve" and r2["rule_key"] != r["rule_key"]
    assert hm._pre_tool_call(tool_name="terminal", args={"command": "pnpm install", "workdir": "~/code/app"}) is None
    assert hm._pre_tool_call(tool_name="terminal", args={"command": "ls ~/.hermes/plugins"}) is None


def test_editing_the_users_plugin_needs_a_card_hermes_own_does_not():
    make_plugin("users-plugin")
    make_plugin("hermes-own"); ledger.add("plugin", "hermes-own")
    p = str(HOME / "plugins" / "users-plugin" / "__init__.py")
    r = hm._pre_tool_call(tool_name="write_file", args={"path": p, "content": "x"})
    assert r["action"] == "approve" and r["rule_key"].startswith("hermes-manager:edit:users-plugin:")
    assert "Always allow covers only this file" in r["message"]
    other = hm._pre_tool_call(tool_name="write_file", args={"path": p.replace("__init__", "other"), "content": "x"})
    assert other["rule_key"] != r["rule_key"], "Always allow on one file never covers another file"
    same = hm._pre_tool_call(tool_name="patch", args={"path": p, "old_string": "a", "new_string": "b"})
    assert same["rule_key"] == r["rule_key"], "the key is the resolved file, whichever file tool edits it"
    r = hm._pre_tool_call(tool_name="patch", args={"path": "~/.hermes/plugins/users-plugin/__init__.py", "old_string": "a"})
    assert r["action"] == "approve"
    r = hm._pre_tool_call(tool_name="terminal", args={"command": "sed -i '' s/a/b/ ~/.hermes/plugins/users-plugin/x.py"})
    assert r["action"] == "approve" and "only this exact command" in r["message"]
    assert hm._pre_tool_call(tool_name="terminal", args={"command": "cat ~/.hermes/plugins/users-plugin/x.py"}) is None
    assert hm._pre_tool_call(tool_name="write_file",
                             args={"path": str(HOME / "plugins" / "hermes-own" / "a.py"), "content": "x"}) is None
    assert hm._pre_tool_call(tool_name="write_file", args={"path": "/tmp/notes.md",
                             "content": "see ~/.hermes/plugins/users-plugin"}) is None, "content mentions don't count"
    real = HOME / "elsewhere" / "linked-src"
    real.mkdir(parents=True)
    (HOME / "plugins" / "linked").symlink_to(real)
    r = hm._pre_tool_call(tool_name="write_file", args={"path": str(real / "__init__.py"), "content": "x"})
    assert r and r["rule_key"].startswith("hermes-manager:edit:linked:"), "editing a symlinked plugin via its real path asks"


def test_busy_check_still_guards_reloads():
    con = sqlite3.connect(HOME / "state.db")
    con.execute("INSERT INTO session_admissions VALUES ('phone-chat', 'running')")
    con.commit()
    try:
        out = J(hm._tool({"action": "reload"}, session_id="main"))
        assert out["ok"] is False and out["busy"] == ["phone-chat"]
        assert J(hm._tool({"action": "reload", "now": True}, session_id="main"))["ok"] is True
    finally:
        con.execute("DELETE FROM session_admissions"); con.commit(); con.close()


def test_slash_waits_while_another_chat_is_mid_reply():
    con = sqlite3.connect(HOME / "state.db")
    con.execute("INSERT INTO sessions VALUES ('other', 0, 0, 'tui', NULL, 3, 'Budget')")
    con.executemany("INSERT INTO session_admissions VALUES (?, 'running')", [("me",), ("other",)])
    con.commit()
    try:
        out = asyncio.run(hm._slash("", session_id="me"))
        assert out.startswith("Not reloading: 1 other chat") and "Budget" in out, out
        con.execute("DELETE FROM session_admissions WHERE target_session_id='other'"); con.commit()
        assert "Gateway: reloaded" in asyncio.run(hm._slash("", session_id="me"))
    finally:
        con.execute("DELETE FROM session_admissions"); con.execute("DELETE FROM sessions WHERE id='other'")
        con.commit(); con.close()


def test_no_main_chat_found_fails_closed():
    old = (HOME / "state.db").rename(HOME / "state.db.bak")
    try:
        r = hm._pre_tool_call(tool_name="skill_manager", args={"action": "disable", "name": "notes"}, session_id="main")
        assert r["action"] == "block"
    finally:
        (HOME / "state.db.bak").rename(HOME / "state.db")


def undo(cid, sid="main"):
    return J(hm._tool({"action": "undo", "id": cid}, session_id=sid))


def test_undo_pairs_skills_and_mcp():
    CONFIG["skills"] = {"disabled": []}
    out = J(skill_manager.tool({"action": "disable", "name": "notes"}, session_id="main"))
    u = undo(out["change_id"])
    assert u["ok"] and u["undid"] == out["change_id"] and CONFIG["skills"]["disabled"] == [], u
    again = undo(out["change_id"])
    assert again["ok"] is False and "already undone" in again["error"]
    CONFIG["skills"] = {"disabled": ["notes"]}
    out = J(skill_manager.tool({"action": "enable", "name": "notes"}, session_id="main"))
    assert undo(out["change_id"])["ok"] and CONFIG["skills"]["disabled"] == ["notes"]
    CONFIG["skills"] = {"disabled": []}
    out = J(skill_manager.tool({"action": "enable", "name": "notes"}, session_id="main"))   # already on
    assert "has no undo" in undo(out["change_id"])["error"]

    CONFIG["mcp_servers"] = {"github": {"command": "npx", "enabled": True}}
    MCP_STATUS[:] = [{"name": "github", "status": "disabled", "tools": 0, "transport": "stdio"}]
    out = J(mcp_manager.tool({"action": "disable", "name": "github"}, session_id="main"))
    assert CONFIG["mcp_servers"]["github"]["enabled"] is False
    assert undo(out["change_id"])["ok"] and CONFIG["mcp_servers"]["github"]["enabled"] is True
    CONFIG["mcp_servers"]["github"]["enabled"] = False
    out = J(mcp_manager.tool({"action": "enable", "name": "github"}, session_id="main"))
    assert undo(out["change_id"])["ok"] and CONFIG["mcp_servers"]["github"]["enabled"] is False
    out = J(mcp_manager.tool({"action": "reconnect", "name": "github"}, session_id="main"))
    assert "has no undo" in undo(out["change_id"])["error"]

    out = J(mcp_manager.tool({"action": "add", "name": "scratch", "command": "npx"}, session_id="main"))
    assert "scratch" in CONFIG["mcp_servers"] and ledger.owned("mcp", "scratch") and "saved" not in out
    calls.clear()
    u = undo(out["change_id"], "thread")   # Hermes' own add: no card, any chat
    assert u["ok"] and "scratch" not in CONFIG["mcp_servers"] and not ledger.owned("mcp", "scratch"), u
    assert "reconcile" in calls


def test_mcp_add_refused_for_users_server_is_not_hermes_and_cannot_be_removed():
    CONFIG["mcp_servers"] = {"theirs": {"command": "npx"}}
    out = J(mcp_manager.tool({"action": "add", "name": "theirs", "command": "uvx"}, session_id="main"))
    assert out["ok"] is False and not ledger.owned("mcp", "theirs"), "a refused add never claims the user's server"
    assert "has no undo" in undo(out["change_id"])["error"]
    assert mcp_manager._change("remove", "theirs", {}, "main", False)["ok"] is False
    assert "theirs" in CONFIG["mcp_servers"]
    assert "unknown action" in J(mcp_manager.tool({"action": "remove", "name": "theirs"}, session_id="main"))["error"]
    assert "unknown action" in J(hm._tool({"action": "restore", "name": "x", "backup": "/tmp"}, session_id="main"))["error"]


def test_undo_pairs_plugins_create_enable_delete_restore_rename():
    calls.clear()
    CONFIG["plugins"] = {"enabled": []}
    trust(True)
    cr = J(hm._tool({"action": "create", "name": "undo-me"}, session_id="thread"))
    folder = HOME / "plugins" / "undo-me"
    PLUGINS.append(("undo-me", "0.1.0", "x", "user", str(folder), "undo-me"))
    en = J(hm._tool({"action": "enable", "name": "undo-me"}, session_id="thread"))
    assert undo(en["change_id"], "thread")["ok"] and "undo-me" not in CONFIG["plugins"]["enabled"]
    J(hm._tool({"action": "enable", "name": "undo-me"}, session_id="thread"))
    de = J(hm._tool({"action": "delete", "name": "undo-me"}, session_id="thread"))
    assert de["ok"] and de["was_enabled"] and not folder.exists()
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "undo-me"]
    calls.clear()
    u = undo(de["change_id"], "thread")   # restore: folder back, Hermes' again, turned back on
    assert u["ok"] and (folder / "plugin.yaml").exists() and ledger.owned("plugin", "undo-me"), u
    assert ("toggle", "undo-me", True) in calls and "undo-me" in CONFIG["plugins"]["enabled"]
    PLUGINS.append(("undo-me", "0.1.0", "x", "user", str(folder), "undo-me"))
    u = undo(cr["change_id"], "thread")   # undo the create = delete it
    assert u["ok"] and not folder.exists() and not ledger.owned("plugin", "undo-me"), u
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "undo-me"]

    make_plugin("rn")
    rn = J(hm._tool({"action": "rename", "name": "rn", "new_name": "rn-two"}, session_id="main"))
    assert rn["ok"] and (HOME / "plugins" / "rn-two").exists()
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "rn"] + [("rn-two", "1", "x", "user", str(HOME / "plugins" / "rn-two"), "rn-two")]
    u = undo(rn["change_id"])
    assert u["ok"] and (HOME / "plugins" / "rn").exists() and not (HOME / "plugins" / "rn-two").exists(), u
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "rn-two"] + [("rn", "1", "x", "user", str(HOME / "plugins" / "rn"), "rn")]
    rl = J(hm._tool({"action": "reload", "now": True}, session_id="main"))
    assert "has no undo" in undo(rl["change_id"])["error"]
    trust(None)


def test_undo_cards_follow_the_same_rules():
    CONFIG["skills"] = {"disabled": []}
    trust(True)
    sk = J(skill_manager.tool({"action": "disable", "name": "notes"}, session_id="main"))
    a = {"action": "undo", "id": sk["change_id"]}
    r = hm._pre_tool_call(tool_name="plugin_manager", args=a, session_id="main")
    assert r["action"] == "approve" and r["rule_key"] == f"hermes-manager:undo:{sk['change_id']}", r
    assert r["message"].startswith(f"Undo change {sk['change_id']}") and "Turn ON the skill 'notes'" in r["message"]
    assert hm._pre_tool_call(tool_name="plugin_manager", args=a, session_id="thread")["action"] == "block"
    assert "main chat" in undo(sk["change_id"], "thread")["error"], "the handler refuses outside main too"

    own = J(hm._tool({"action": "create", "name": "own-undo"}, session_id="thread"))
    assert hm._pre_tool_call(tool_name="plugin_manager", args={"action": "undo", "id": own["change_id"]},
                             session_id="thread") is None, "undoing Hermes' own create needs no card"

    make_plugin("own-rn"); ledger.add("plugin", "own-rn")
    rn = J(hm._tool({"action": "rename", "name": "own-rn", "new_name": "own-rn-2"}, session_id="main"))
    PLUGINS[:] = [p for p in PLUGINS if p[0] != "own-rn"] + [("own-rn-2", "1", "x", "user", str(HOME / "plugins" / "own-rn-2"), "own-rn-2")]
    r = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "undo", "id": rn["change_id"]}, session_id="main")
    assert r["action"] == "approve" and "Rename the plugin 'own-rn-2' to 'own-rn'" in r["message"], r
    assert hm._pre_tool_call(tool_name="plugin_manager", args={"action": "undo", "id": rn["change_id"]},
                             session_id="thread")["action"] == "block", "an undo rename always asks"

    # no id: the card pins the change it showed, so a newer change can't be undone in its place
    CONFIG["skills"] = {"disabled": []}   # notes is still off from the card above; turn it back on first
    late = J(skill_manager.tool({"action": "disable", "name": "notes"}, session_id="main"))
    r = hm._pre_tool_call(tool_name="plugin_manager", args={"action": "undo"}, session_id="main")
    assert late["change_id"] in r["message"]
    PLUGINS.append(("own-undo", "0.1.0", "x", "user", str(HOME / "plugins" / "own-undo"), "own-undo"))
    newer = J(hm._tool({"action": "enable", "name": "own-undo"}, session_id="main"))
    u = J(hm._tool({"action": "undo"}, session_id="main"))
    assert u["ok"] and u["undid"] == late["change_id"] and CONFIG["skills"]["disabled"] == [], u

    text = asyncio.run(hm._undo_slash(""))   # the user typed it: no card, newest first
    assert text.startswith(f"Undid {newer['change_id']}"), text
    assert "No change with id" in asyncio.run(hm._undo_slash("nope1234"))
    log_text = asyncio.run(hm._log_slash(""))
    assert newer["change_id"] in log_text and "[undone]" in log_text and "(undo of " in log_text
    f = ledger.folder() / "changes.jsonl"
    f.rename(f.with_suffix(".bak"))
    try:
        assert asyncio.run(hm._undo_slash("")) == "Nothing to undo."
    finally:
        f.with_suffix(".bak").rename(f)
    trust(None)


def test_login_returns_the_link_or_a_clear_reason():
    def start(name):
        if name == "slow":
            raise TimeoutError("x")
        if name == "done":
            return types.SimpleNamespace(auth_url="", flow=None, detail="")
        return types.SimpleNamespace(auth_url="https://auth.example/authorize?state=s1", detail="",
                                     flow=types.SimpleNamespace(redirect_uri="http://127.0.0.1:5555/callback"))
    mod("tools.connectors")
    mod("tools.connectors.mcp_oauth", start=start)
    CONFIG["mcp_servers"] = {"echo": {"command": "python3"}, "remote": {"url": "https://r/mcp"},
                             "slow": {"url": "https://s/mcp"}, "done": {"url": "https://d/mcp"}}
    assert hm._pre_tool_call(tool_name="mcp_manager", args={"action": "login", "name": "remote"},
                             session_id="thread") is None, "login needs no card, any chat"
    out = J(mcp_manager.tool({"action": "login", "name": "echo"}, session_id="thread"))
    assert out["ok"] is False and "doesn't use sign-in" in out["error"]
    out = J(mcp_manager.tool({"action": "login", "name": "remote"}, session_id="thread"))
    assert out["ok"] and out["link"].startswith("https://auth.example/") and out["returns_to"].startswith("http://127.0.0.1")
    assert "browser on that computer" in out["summary"] and "redirect_uri" in out["summary"]
    assert "state=s1" not in (ledger.folder() / "changes.jsonl").read_text(), "a live link never lands in the log"
    assert "already signed in" in J(mcp_manager.tool({"action": "login", "name": "done"}))["summary"]
    assert "30 s" in J(mcp_manager.tool({"action": "login", "name": "slow"}))["error"]
    assert "No MCP server" in J(mcp_manager.tool({"action": "login", "name": "nope"}))["error"]
    MCP_STATUS[:] = [{"name": "remote", "status": "failed", "tools": 0, "transport": "http", "needs_auth": True}]
    assert "mcp_manager action=login" in mcp_manager._result("remote", {}, "Added")["summary"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]   # file order: later tests read the log
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
