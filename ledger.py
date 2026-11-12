"""What Hermes made itself (no card needed to change it), and the change log of every manager change.

~/.hermes/hermes-manager/ledger.json   {"plugin": {name: {...}}, "skill": {...}, "mcp": {...}}
~/.hermes/hermes-manager/changes.jsonl one line per change: id, when, chat, tool, action, target, card,
                                       result, and its undo (the inverse call) or what it undoes

Only this plugin's code writes the ledger: a plugin.yaml label is never trusted, and file / terminal
tool calls that touch this folder are blocked (``guard_files``), so Hermes cannot relabel your things
as its own. That guard is a speed bump against mistakes, not a sandbox: any program running as you can
still edit the file.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import common

KINDS = ("plugin", "skill", "mcp")
DIRNAME = "hermes-manager"
_lock = threading.Lock()


def folder() -> Path:
    return common.home() / DIRNAME


def _path() -> Path:
    return folder() / "ledger.json"


def _read() -> dict[str, dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {k: dict(data.get(k) or {}) if isinstance(data.get(k), dict) else {} for k in KINDS}


def _write(data: dict) -> None:
    folder().mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _path())


def _fingerprint(kind: str, name: str) -> Optional[str]:
    """What the item is now, so a record cannot vouch for a different thing later given the same name:
    the plugin folder's inode, or the MCP entry's digest. None when it does not exist (or for skills)."""
    try:
        if kind == "plugin":
            st = (common.home() / "plugins" / name).stat()
            return f"{st.st_dev}:{st.st_ino}"
        if kind == "mcp":
            entry = (common.read_config().get("mcp_servers") or {}).get(name)
            return common.digest({k: v for k, v in entry.items() if k != "enabled"}) if isinstance(entry, dict) else None
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    return None


def owned(kind: str, name: str) -> bool:
    """Hermes made it, and it is still the same thing (removed and re-made by hand under the same name is not).
    ponytail: skills carry no fingerprint (only skill_manage writes them), add one if hand-made skills matter."""
    rec = _read().get(kind, {}).get(name) if name else None
    if rec is None:
        return False
    was = rec.get("fp") if isinstance(rec, dict) else None
    now = _fingerprint(kind, name) if was else None
    return was is None or now is None or was == now


def names(kind: str) -> list[str]:
    return sorted(_read().get(kind, {}))


def add(kind: str, name: str, chat: Optional[str] = None, **extra: Any) -> None:
    with _lock:
        data = _read()
        data[kind][name] = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "chat": chat,
                            "fp": _fingerprint(kind, name), **extra}
        _write(data)


def drop(kind: str, name: str) -> None:
    with _lock:
        data = _read()
        if data[kind].pop(name, None) is not None:
            _write(data)


def rename(kind: str, old: str, new: str) -> None:
    with _lock:
        data = _read()
        if old in data[kind]:
            data[kind][new] = {**data[kind].pop(old), "renamed_from": old}
            data[kind][new]["fp"] = _fingerprint(kind, new) or data[kind][new].get("fp")
            _write(data)


def log(tool: str, action: str, target: str, *, chat: Optional[str], card: bool, result: Any,
        undo: Optional[dict] = None, undoes: Optional[str] = None) -> str:
    """Append one change and return its id. Never raises: a full disk must not fail the change itself.

    ``undo`` is the inverse ({"tool", "args"}), kept only when the change worked. ``undoes`` marks this
    line as the undo of an earlier id; such lines carry no undo of their own, so undo walks backwards."""
    cid = uuid.uuid4().hex[:8]
    try:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = {"text": result}
        ok = bool(result.get("ok", "error" not in result)) if isinstance(result, dict) else True
        line = {"id": cid, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "chat": chat, "tool": tool,
                "action": action, "target": target, "card": card, "ok": ok}
        if undo:   # callers pass one only when the change landed (an add that saved but failed to connect counts)
            line["undo"] = undo
        if undoes:
            line["undoes"] = undoes
        if isinstance(result, dict):
            msg = result.get("error") or result.get("summary") or result.get("renamed")
            if msg:
                line["note"] = common.clip(msg, 240)
        folder().mkdir(parents=True, exist_ok=True)
        with _lock, open(folder() / "changes.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:
        pass
    return cid


def recent(n: Optional[int] = 20) -> list[dict]:
    try:
        lines = (folder() / "changes.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:] if n else lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def undo_target(cid: Optional[str] = None) -> tuple[Optional[dict], Optional[str]]:
    """The change to undo: ``cid``, or the newest undoable change not yet undone. (line, why-not)."""
    # Note: reads the whole log each time; fine for thousands of lines, index it if it grows huge
    rows = recent(None)
    undone = {r["undoes"] for r in rows if r.get("undoes") and r.get("ok")}
    cid = str(cid or "").strip()
    if cid:
        row = next((r for r in rows if r.get("id") == cid), None)
        if row is None:
            return None, f"No change with id {cid!r} (see /manager-log)."
        if cid in undone:
            return None, f"Change {cid} was already undone."
        if not row.get("undo"):
            return None, f"Change {cid} ({row.get('tool')} {row.get('action')}) has no undo."
        return row, None
    for r in reversed(rows):
        if r.get("undo") and r.get("id") not in undone:
            return r, None
    return None, "Nothing to undo."


FILE_TOOLS = {"write_file", "patch", "terminal", "execute_code"}


def guard_files(tool_name: str, args: dict) -> Optional[dict]:
    """Block file and shell tool calls that would edit the ledger or change log by hand. Paths are compared
    resolved (workdir, cd, ~, $HERMES_HOME, /./, symlinks); the old text match stays as a second net."""
    if tool_name not in FILE_TOOLS or not isinstance(args, dict):
        return None
    from . import guards
    try:
        blob = json.dumps(args)
    except (TypeError, ValueError):
        return None
    if tool_name in ("write_file", "patch"):
        blob = json.dumps([args.get("path"), args.get("patch")])
    roots = {Path(os.path.normpath(folder())), Path(os.path.realpath(folder()))}
    hit = (f"{DIRNAME}/ledger" in blob or f"{DIRNAME}/changes" in blob or f"{DIRNAME}\\\\ledger" in blob
           or any(p in roots or p.parent in roots and p.name.startswith(("ledger", "changes"))
                  for p in guards.touched(tool_name, args, guess_home=True)))
    if not hit:
        return None
    if tool_name == "terminal" and not guards.writes(str(args.get("command") or "")):
        return None   # reading it (cat, tail, jq) is fine
    return {"action": "block",
            "message": "The hermes-manager ledger and change log are written only by the manager tools. "
                       "Read them with plugin_manager action=log."}
