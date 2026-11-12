"""plugin_manager action=rename: move a user plugin to a new name in one step, with a backup.

Moves together: the plugin folder, plugin.yaml ``name``, its line in plugins.enabled / plugins.disabled,
its plugins.entries.<name> settings, ~/.hermes/plugin-data/<name>, and saved "Always allow" approvals
whose rule key ends in the old name (``plugin-manager:<action>:<old>``). Then one reload loads it under
the new name. Bundled plugins and hermes-manager itself are refused.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from . import common

NEW_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}


def find_user_plugin(name: str) -> Optional[dict]:
    from hermes_cli.plugins_cmd import _discover_all_plugins
    for pname, _v, _d, source, path, key in _discover_all_plugins():
        if name in (pname, key):
            return {"name": pname, "key": key, "source": source, "dir": Path(path) if path else None}
    return None


def _taken(new: str) -> bool:
    from hermes_cli.plugins_cmd import _discover_all_plugins
    return any(new in (pname, key) for pname, _v, _d, _s, _p, key in _discover_all_plugins())


def _code_mentions(folder: Path, old: str, limit: int = 8) -> list[str]:
    """file:line places where the plugin's own files spell its old name (paths, data dirs, ...)."""
    hits: list[str] = []
    pat = re.compile(re.escape(old))
    for f in sorted(folder.rglob("*")):
        if len(hits) >= limit:
            break
        if not f.is_file() or set(f.relative_to(folder).parts) & SKIP_DIRS or f.name == "plugin.yaml":
            continue
        if f.suffix not in {".py", ".yaml", ".yml", ".json", ".toml", ".md", ".sh", ".js", ".ts", ".txt"}:
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{f.relative_to(folder)}:{i}: {common.clip(line, 100)}")
                    if len(hits) >= limit:
                        break
        except OSError:
            continue
    return hits


def check(name: str, new: str, self_names: set[str]) -> tuple[Optional[dict], Optional[str]]:
    """(plan, None) or (None, why not). The plan is what the card lists and what ``run`` does."""
    if not NEW_NAME_RE.match(new or ""):
        return None, "new_name must be 2-48 lowercase letters, digits or -, starting with a letter or digit."
    found = find_user_plugin(name)
    if found is None:
        return None, f"No plugin named {name!r} (see action=list)."
    if found["source"] != "user" or found["dir"] is None:
        return None, f"{name!r} is a {found['source']} plugin; only your own plugins in ~/.hermes/plugins can be renamed."
    if found["key"] in self_names or found["name"] in self_names:
        return None, "hermes-manager provides this tool and cannot rename itself."
    if "/" in found["key"]:
        return None, f"{name!r} lives in a nested folder ({found['key']}); rename it by hand."
    if new in (found["key"], found["name"]):
        return None, "That is already its name."
    root = common.home()
    target = found["dir"].parent / new
    if _taken(new) or target.exists():
        return None, f"A plugin named {new!r} already exists."
    data_old, data_new = root / "plugin-data" / found["key"], root / "plugin-data" / new
    if data_new.exists():
        return None, f"~/.hermes/plugin-data/{new} already exists; move it aside first."
    cfg = common.read_config()
    plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    olds = {found["key"], found["name"]}
    lists = [k for k in ("enabled", "disabled") if isinstance(plugins.get(k), list) and olds & set(map(str, plugins[k]))]
    entries = isinstance(plugins.get("entries"), dict) and any(o in plugins["entries"] for o in olds)
    allow = [k for k in (cfg.get("command_allowlist") or []) if isinstance(k, str)
             and k.startswith("plugin-manager:") and k.rsplit(":", 1)[-1] in olds]
    plan = {"old": found["key"], "old_names": sorted(olds), "new": new, "dir": str(found["dir"]),
            "target": str(target), "config_lists": lists, "settings": entries,
            "data_dir": str(data_old) if data_old.exists() else None, "approvals": allow,
            "enabled": "enabled" in lists, "code_mentions": _code_mentions(found["dir"], found["key"])}
    return plan, None


def describe(plan: dict) -> str:
    moves = [f"folder plugins/{plan['old']}", "plugin.yaml name"]
    if plan["config_lists"]:
        moves.append("its plugins." + "/".join(plan["config_lists"]) + " line")
    if plan["settings"]:
        moves.append("its settings")
    if plan["data_dir"]:
        moves.append(f"plugin-data/{plan['old']}")
    if plan["approvals"]:
        moves.append(f"{len(plan['approvals'])} saved approval(s)")
    text = f"Rename the plugin {plan['old']!r} to {plan['new']!r}? Moves: {', '.join(moves)}. Backup first."
    if plan["code_mentions"]:
        text += (f" Warning: its code mentions {plan['old']!r} in {len(plan['code_mentions'])} place(s) "
                 f"(e.g. {plan['code_mentions'][0]}); those are NOT changed.")
    return text


def _rewrite_manifest(folder: Path, new: str) -> None:
    for fn in ("plugin.yaml", "plugin.yml"):
        p = folder / fn
        if p.exists():
            text = p.read_text(encoding="utf-8")
            new_text, n = re.subn(r"(?m)^name:\s*.*$", f"name: {new}", text, count=1)
            p.write_text(new_text if n else f"name: {new}\n" + text, encoding="utf-8")
            return


def _rewrite_config(plan: dict) -> None:
    olds, new = set(plan["old_names"]), plan["new"]
    cfg = common.read_config()
    plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else None
    if plugins is not None:
        for k in ("enabled", "disabled"):
            if isinstance(plugins.get(k), list):
                out: list = []
                for item in plugins[k]:
                    item = new if str(item) in olds else item
                    if item not in out:
                        out.append(item)
                plugins[k] = out
        if isinstance(plugins.get("entries"), dict):
            for o in sorted(olds):
                if o in plugins["entries"]:
                    plugins["entries"].setdefault(new, plugins["entries"].pop(o))
    if isinstance(cfg.get("command_allowlist"), list):
        cfg["command_allowlist"] = [
            (k.rsplit(":", 1)[0] + ":" + new) if isinstance(k, str) and k.startswith("plugin-manager:")
            and k.rsplit(":", 1)[-1] in olds else k for k in cfg["command_allowlist"]]
    common.save_config(cfg)


def run(plan: dict, reload_fn) -> dict[str, Any]:
    """Backup, move everything, reload. On a failure before the reload, undo what was moved."""
    root = common.home()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root / "backups" / "hermes-manager" / f"rename-{plan['old']}-to-{plan['new']}-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    cfg_path = root / "config.yaml"
    if cfg_path.exists():
        shutil.copy2(cfg_path, backup / "config.yaml")
    snapshot = common.read_config()
    (backup / "config-before.json").write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    shutil.copytree(plan["dir"], backup / "plugin" / plan["old"], ignore=shutil.ignore_patterns("__pycache__"))
    if plan["data_dir"]:
        shutil.copytree(plan["data_dir"], backup / "plugin-data" / plan["old"])
    (backup / "PLAN.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    (backup / "UNDO.md").write_text(
        f"Undo this rename: ask Hermes to rename {plan['new']!r} back to {plan['old']!r} (plugin_manager rename), "
        f"or by hand: move {plan['target']} back to {plan['dir']}, copy config.yaml from this folder over "
        f"~/.hermes/config.yaml (this drops config changes made after {stamp}), move plugin-data back, then "
        "run `hermes reload-plugins`.\n", encoding="utf-8")
    done: list[str] = []
    try:
        shutil.move(plan["dir"], plan["target"])
        done.append("dir")
        _rewrite_manifest(Path(plan["target"]), plan["new"])
        if plan["data_dir"]:
            shutil.move(plan["data_dir"], str(root / "plugin-data" / plan["new"]))
            done.append("data")
        _rewrite_config(plan)
        done.append("config")
    except Exception as exc:
        if "config" not in done:
            try:
                common.save_config(snapshot)
            except Exception:
                pass
        stuck = []
        for step, fn in (("data", lambda: shutil.move(str(root / "plugin-data" / plan["new"]), plan["data_dir"])),
                         ("dir", lambda: (shutil.move(plan["target"], plan["dir"]), shutil.copy2(
                             backup / "plugin" / plan["old"] / "plugin.yaml", Path(plan["dir"]) / "plugin.yaml")))):
            if step in done:
                try:
                    fn()
                except Exception as undo_exc:
                    stuck.append(f"{step}: {type(undo_exc).__name__}: {undo_exc}")
        if stuck:
            return {"ok": False, "error": f"Rename failed ({type(exc).__name__}: {exc}) and the rollback did not "
                    f"finish ({'; '.join(stuck)}). Restore by hand from the backup.", "backup": str(backup)}
        return {"ok": False, "error": f"Rename failed and was rolled back: {type(exc).__name__}: {exc}",
                "backup": str(backup)}
    out: dict[str, Any] = {"ok": True, "renamed": f"{plan['old']} -> {plan['new']}", "backup": str(backup),
                           "approvals_moved": len(plan["approvals"])}
    if plan["enabled"]:
        out["reload"] = reload_fn()
    if plan["code_mentions"]:
        out["check_code"] = plan["code_mentions"]
    return out
