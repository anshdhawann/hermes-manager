"""A card before Hermes installs code from the internet into its own home (plugin or MCP dependencies).

Only terminal commands that both download code (pip/uv/npm/pnpm/yarn/bun install, git clone, curl|sh)
AND point into ~/.hermes are carded, so coding work in other repos is untouched. "Always allow" covers
that exact command only.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

from . import common

DOWNLOAD = re.compile(
    r"\b(?:pip3?|python3?\s+-m\s+pip|uv\s+pip|uv)\s+(?:install|add|sync)\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add)\b"
    r"|\bgit\s+clone\b"
    r"|\b(?:curl|wget)\b[^|;&]*\|\s*(?:ba|z|da)?sh\b")
HERMES_PATH = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+)/\.hermes\b|(?:^|[\s'\"=])\.hermes/")


def install_card(tool_name: str, args: dict) -> Optional[dict]:
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    command = str(args.get("command") or "")
    where = f"{command} {args.get('workdir') or ''}"
    if not DOWNLOAD.search(command) or not HERMES_PATH.search(where):
        return None
    digest = hashlib.sha256(command.encode()).hexdigest()[:10]
    return {"action": "approve", "rule_key": f"hermes-manager:install:{digest}",
            "message": f"Install code from the internet into Hermes? Command: {common.clip(command, 300)}"}


# ---- edits to the user's plugins --------------------------------------------------------------------
# Editing a plugin the user made (anything not in the ledger) needs their approval.
WRITE_OPS = re.compile(r">|\btee\b|\bsed\s+-i|\bperl\s+-p?i|\bmv\b|\bcp\b|\brm\b|\brsync\b|\btruncate\b|\bpatch\b"
                       r"|\bgit\s+(?:apply|checkout|restore|reset)\b|\bpython3?\b|\bnode\b|\bln\b|\bchmod\b|\btouch\b"
                       r"|write_text|open\([^)]*['\"][wa]")


def _plugin_keys(text: str) -> set[str]:
    root = common.home() / "plugins"
    pats = {str(root), "~/.hermes/plugins", "$HOME/.hermes/plugins", "${HOME}/.hermes/plugins", ".hermes/plugins"}
    keys: set[str] = set()
    for base in pats:
        for m in re.finditer(re.escape(base) + r"/([A-Za-z0-9_.-]+)", text):
            keys.add(m.group(1))
    # A symlinked plugin (my-plugin -> ~/code/my-plugin) is also edited through its real path.
    # Note: plain substring match, so a sibling folder with the same prefix also asks (fails toward a card)
    try:
        for d in root.iterdir():
            if d.is_symlink():
                home = str(Path.home())
                for real in {str(d.resolve()), os.path.abspath(root / d.readlink())}:   # /private/var vs /var
                    tilde = "~" + real[len(home):] if real.startswith(home + "/") else real
                    if real in text or tilde in text:
                        keys.add(d.name)
    except OSError:
        pass
    return {k for k in keys if k not in (".", "..") and (root / k).is_dir()}


def edit_card(tool_name: str, args: dict) -> Optional[dict]:
    if tool_name not in ("write_file", "patch", "terminal", "execute_code") or not isinstance(args, dict):
        return None
    from . import ledger
    if tool_name == "write_file":
        text = str(args.get("path") or "")
    elif tool_name == "patch":
        text = " ".join(str(v) for v in args.values())
    else:
        text = f"{args.get('command') or args.get('code') or ''} {args.get('workdir') or ''}"
        if not WRITE_OPS.search(str(args.get("command") or args.get("code") or "")):
            return None
    theirs = sorted(k for k in _plugin_keys(text) if not ledger.owned("plugin", k))
    if not theirs:
        return None
    what = common.clip(args.get("command") or args.get("code") or args.get("path") or "", 200)
    # "Always allow" covers one file, not the whole plugin.
    if args.get("path") and tool_name in ("write_file", "patch"):
        grain, scope = str(Path(str(args["path"])).expanduser().resolve()), "this file"
    else:   # terminal / execute_code / multi-file patch: no single file, so pin the exact text
        grain, scope = str(args.get("command") or args.get("code") or args.get("patch") or ""), "this exact command"
    sha8 = hashlib.sha256(grain.encode()).hexdigest()[:8]
    return {"action": "approve", "rule_key": f"hermes-manager:edit:{','.join(theirs)}:{sha8}",
            "message": f"Edit your plugin {', '.join(theirs)}? Hermes didn't make it. {tool_name}: {what}. "
                       f"Always allow covers only {scope}."}
