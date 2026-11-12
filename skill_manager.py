"""skill_manager: list skills with their on/off state, and turn one on or off.

Uses the same switch as the Desktop Skills toggle and `hermes skills` (``skills.disabled`` through
``hermes_cli.skills_config.get_disabled_skills`` / ``save_disabled_skills``), then runs what
/reload-skills runs (``agent.skill_commands.reload_skills``) and drops the skills prompt snapshot so
new chats list the change. Creating, editing and deleting skills stays with Hermes' own
``skill_manage`` tool (it has its own write gate).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import common, ledger

log = logging.getLogger("hermes_manager.skills")
TOOL = "skill_manager"
TOOLSET = "skill_manager"
CHANGES = ("enable", "disable")
LIST_CAP = 150

SCHEMA = {
    "name": TOOL,
    "description": (
        "Turn Hermes skills on or off. action=list shows installed skills with enabled true/false and "
        "category (filter with query and state). action=enable / disable switches one skill for every chat "
        "and platform (the same switch as the Desktop Skills toggle) and applies it now. Skills you created "
        "(made_by_hermes true) switch at once from any chat; the user's skills need an approval card and the "
        "main chat. New chats see the change in their skill list; open "
        "chats keep their prompt (cache kept) but a disabled skill no longer loads. To create, edit or delete "
        "a skill use skill_manage instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "enable", "disable"]},
            "name": {"type": "string", "description": "Skill name exactly as action=list shows it."},
            "query": {"type": "string", "description": "list only: words to match in name, category or description."},
            "state": {"type": "string", "enum": ["all", "enabled", "disabled"],
                      "description": "list only (default all)."},
            "reason": {"type": "string", "description": "One short line for the approval card."},
        },
        "required": ["action"],
    },
}


def _catalog() -> list[dict]:
    from tools.skills_tool import find_all_skills
    return find_all_skills(skip_disabled=True)   # every installed skill, on or off


def _disabled(cfg: dict) -> tuple[set[str], dict[str, list[str]]]:
    """Globally disabled names, and per-platform extras ({skill: [platforms]})."""
    from hermes_cli.skills_config import get_disabled_skills
    glob = get_disabled_skills(cfg)
    per: dict[str, list[str]] = {}
    pd = (cfg.get("skills") or {}).get("platform_disabled") if isinstance(cfg.get("skills"), dict) else None
    if isinstance(pd, dict):
        for platform, names in pd.items():
            for n in (names if isinstance(names, list) else [names] if isinstance(names, str) else []):
                per.setdefault(str(n), []).append(str(platform))
    return glob, per


def _essential() -> set[str]:
    try:
        from agent.skill_utils import ESSENTIAL_SKILLS
        return set(ESSENTIAL_SKILLS)
    except Exception:
        return set()


def _list(query: str = "", state: str = "all") -> dict[str, Any]:
    cfg = common.read_config()
    glob, per = _disabled(cfg)
    essential = _essential()
    words = [w for w in query.lower().split() if w]
    rows = []
    for s in _catalog():
        name = s["name"]
        on = name not in glob
        if state == "enabled" and not on or state == "disabled" and on:
            continue
        hay = f"{name} {s.get('category') or ''} {s.get('description') or ''}".lower()
        if words and not all(w in hay for w in words):
            continue
        row = {"name": name, "enabled": on, "category": s.get("category") or "uncategorized",
               "description": common.clip(s.get("description"), 120)}
        if name in per:
            row["off_on_platforms"] = sorted(per[name])
        if name in essential:
            row["essential"] = True
        rows.append(row)
    rows.sort(key=lambda r: (r["category"], r["name"]))
    out: dict[str, Any] = {"skills": rows[:LIST_CAP], "count": len(rows)}
    if len(rows) > LIST_CAP:
        out["note"] = f"Showing {LIST_CAP} of {len(rows)}; narrow with query or state."
    return out


def _refresh() -> dict[str, Any]:
    """What /reload-skills does, plus the prompt snapshot drop the Desktop editor does."""
    out: dict[str, Any] = {}
    try:
        from tools.skills_tool import clear_skills_cache
        clear_skills_cache()
    except Exception:
        pass
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass
    try:
        from agent.skill_commands import reload_skills
        r = reload_skills()
        out = {"added": [i.get("name") for i in r.get("added", [])],
               "removed": [i.get("name") for i in r.get("removed", [])], "total": r.get("total")}
    except Exception as exc:
        out["reload_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _set_enabled(name: str, enabled: bool) -> dict[str, Any]:
    names = {s["name"] for s in _catalog()}
    if name not in names:
        return {"ok": False, "error": f"No installed skill named {name!r} (see action=list)."}
    if not enabled and name in _essential():
        return {"ok": False, "error": f"{name!r} is an essential Hermes skill and cannot be turned off."}
    from hermes_cli.skills_config import save_disabled_skills
    cfg = common.read_config()
    glob, per = _disabled(cfg)
    if (name not in glob) == enabled:
        out = {"ok": True, "unchanged": True, "skill": name, "enabled": enabled}
        if enabled and name in per:
            out["note"] = f"Still off on: {', '.join(sorted(per[name]))} (per-platform setting, `hermes skills`)."
        return out
    (glob.discard if enabled else glob.add)(name)
    save_disabled_skills(cfg, glob)
    out = {"ok": True, "skill": name, "enabled": enabled, "slash_commands": _refresh(),
           "summary": (f"Turned {'on' if enabled else 'off'} {name}. New chats see it "
                       f"{'in' if enabled else 'gone from'} their skill list; open chats keep their prompt.")}
    if enabled and name in per:
        out["note"] = f"Still off on: {', '.join(sorted(per[name]))} (per-platform setting, `hermes skills`)."
    return out


def needs_card(args: dict) -> bool:
    """enable/disable ask unless Hermes created that skill itself (recorded when skill_manage creates it)."""
    return str(args.get("action") or "") in CHANGES and not ledger.owned("skill", str(args.get("name") or "").strip())


def tool(args: dict, session_id: Optional[str] = None, **_: Any) -> str:
    args = args or {}
    action = str(args.get("action") or "")
    if action not in CHANGES:
        try:
            if action == "list":
                state = str(args.get("state") or "all")
                out = _list(str(args.get("query") or ""), state if state in ("enabled", "disabled") else "all")
                own = set(ledger.names("skill"))
                for r in out["skills"]:
                    r["made_by_hermes"] = r["name"] in own
                return json.dumps(out)
            return json.dumps({"error": f"unknown action {action!r}; use list, enable or disable"})
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    name = str(args.get("name") or "").strip()
    card = needs_card(args)
    try:
        refusal = common.not_main(session_id) if card else None
        if refusal:
            out = {"ok": False, "error": refusal}
        elif not name:
            out = {"ok": False, "error": f"{action} needs a skill name (see action=list)."}
        else:
            out = _set_enabled(name, action == "enable")
    except Exception as exc:
        log.warning("skill_manager %s failed: %s", action, exc)
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    undo = None
    if out.get("ok") and not out.get("unchanged"):
        undo = {"tool": TOOL, "args": {"action": "disable" if action == "enable" else "enable", "name": name}}
    out["change_id"] = ledger.log(TOOL, action, name, chat=session_id, card=card, result=out, undo=undo)
    return json.dumps(out)


def card(args: dict, session_id: Optional[str] = None) -> Optional[dict]:
    if not needs_card(args):
        return None
    action = str(args.get("action") or "")
    refusal = common.not_main(session_id)
    if refusal:
        return {"action": "block", "message": refusal}
    name = str(args.get("name") or "").strip()
    reason = common.clip(args.get("reason") or "no reason given", 200)
    what = (f"Turn ON the skill {name!r} for every chat?" if action == "enable"
            else f"Turn OFF the skill {name!r} for every chat? Hermes stops loading it.")
    return {"action": "approve", "rule_key": f"skill-manager:{action}:{name or '?'}",
            "message": f"{what} Reason: {reason}"}


def after_tool(tool_name: str, args: dict, result: Any, session_id: Optional[str]) -> None:
    """post_tool_call: remember skills Hermes creates with skill_manage (and forget deleted ones), so
    it can switch its own skills without a card. Staged (write-gated) creates are not counted. Takes the
    flat call and the ``operations`` batch; in a batch only ops the result reports as done count."""
    if tool_name != "skill_manage" or not isinstance(args, dict):
        return
    ops = args["operations"] if isinstance(args.get("operations"), list) else [args]
    try:
        res = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return
    if not isinstance(res, dict) or not res.get("success") or res.get("staged"):
        return
    done = res.get("results") if isinstance(res.get("results"), list) else None
    for op in ops:
        if not isinstance(op, dict):
            continue
        action, name = str(op.get("action") or ""), str(op.get("name") or "").strip()
        if action not in ("create", "delete") or not name:
            continue
        if done is not None and not any(isinstance(r, dict) and r.get("success") and r.get("staged") is not True
                                        and str(r.get("name") or "").strip() == name and r.get("action") == action
                                        for r in done):
            continue
        if action == "create":
            ledger.add("skill", name, session_id)
        else:
            ledger.drop("skill", name)
        ledger.log("skill_manage", action, name, chat=session_id, card=False, result={"ok": True})
