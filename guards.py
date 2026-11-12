"""Cards for file and terminal tool calls: installing code from the internet into Hermes' home, editing
plugin files, and changing hermes-manager's own trust setting.

Only terminal commands that both download code (pip/uv/npm/pnpm/yarn/bun install, npx/uvx, git clone,
curl|sh) AND point into ~/.hermes are carded, so coding work in other repos is untouched. `hermes plugins|mcp|
skills` commands that change something are carded anywhere. "Always allow" covers that exact command only.

Paths are compared after resolving them: ``workdir``, a ``cd <dir>`` in the command, ``~``, ``$HOME``,
``$HERMES_HOME``, ``/./``, ``//``, ``..`` and symlinks. This checks intent, it is not a sandbox: a command
that builds a path at run time (``$(...)``, string joins in code) is not seen.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional

from . import common

DOWNLOAD = re.compile(
    r"\b(?:pip3?|python3?\s+-m\s+pip|uv\s+pip|uv)\s+(?:install|add|sync)\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add)\b"
    r"|\b(?:npx|uvx|pipx|bunx)\b|\b(?:pnpm|yarn)\s+dlx\b|\bbun\s+x\b"
    r"|\bgit\s+clone\b"
    r"|\b(?:curl|wget)\b[^|;&]*\|\s*(?:ba|z|da)?sh\b")
HERMES_PATH = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+)/\.hermes\b|(?:^|[\s'\"=])\.hermes/")


HERMES_CLI = re.compile(r"\bhermes(?:\s+\S+){0,4}?\s+(?:plugins?|mcp|skills?)\s+"   # {0,4}: global flags like -p work
                        r"(?:install|uninstall|enable|disable|remove|rm|add|update|delete|reset|config)\b")
CARD_MAX = 2000   # longer commands are refused, not clipped: the card must show what "Always allow" covers
TOO_LONG = {"action": "block", "message": f"This command is over {CARD_MAX} characters, too long to show on an "
                                          "approval card. Split it into shorter steps."}

_SPLIT = re.compile(r"[\s'\"`=<>|;&(),\[\]{}]+")
_CD = re.compile(r"(?:^|[;&|(]|\bthen|\bdo)\s*(?:builtin\s+)?(?:cd|pushd)\s+(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)")
_PATCH_FILES = re.compile(r"^(?:\*\*\* (?:Update|Add|Delete) File:|\+\+\+|---)\s*(?:[ab]/)?(\S+)", re.M)


def _expand(s: str) -> str:
    h = str(common.home())
    s = re.sub(r"\$\{HERMES_HOME\}|\$HERMES_HOME\b", lambda _m: h, s.strip("'\""))
    return os.path.expanduser(os.path.expandvars(s))


def _both(p: str) -> set[Path]:
    n = os.path.normpath(p)
    return {Path(n), Path(os.path.realpath(n))}


def touched(tool_name: str, args: dict, guess_home: bool = False) -> set[Path]:
    """Absolute paths a file or shell call may touch, as written (normalised) and with links resolved:
    write_file/patch paths, terminal ``workdir`` and ``cd`` targets, and every path-like word in a command
    or code resolved against them. ``guess_home`` also resolves relative words against the Hermes home."""
    out: set[Path] = set()
    if tool_name in ("write_file", "patch"):
        words = [str(args.get("path") or "")] + _PATCH_FILES.findall(str(args.get("patch") or ""))
        bases: list[str] = []
    else:
        code = str(args.get("command") or args.get("code") or "")
        bases = [_expand(str(args["workdir"]))] if args.get("workdir") else []
        for m in _CD.finditer(code):
            for b in (bases or [""]):
                bases.append(os.path.join(b, _expand(m.group(1))))
        bases = [b for b in bases if os.path.isabs(b)]
        for b in bases:
            out |= _both(b)
        words = _SPLIT.split(code)
    if guess_home:
        bases = bases + [str(common.home())]
    for w in words:
        w = _expand(w) if w else ""
        if not w or w.startswith("-") or "://" in w:
            continue
        if os.path.isabs(w):
            out |= _both(w)
        else:
            for b in bases:
                out |= _both(os.path.join(b, w))
    return out


def _under(p: Path, base: Path) -> bool:
    try:
        return p == base or p.is_relative_to(base)
    except ValueError:
        return False


def install_card(tool_name: str, args: dict) -> Optional[dict]:
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    command = str(args.get("command") or "")
    cli = HERMES_CLI.search(command)
    if not cli:
        if not DOWNLOAD.search(command):
            return None
        where = f"{command} {args.get('workdir') or ''}"
        hermes = _both(str(common.home()))
        if not HERMES_PATH.search(where) and not any(_under(p, h) for p in touched(tool_name, args) for h in hermes):
            return None
    if len(command) > CARD_MAX:
        return TOO_LONG
    digest = hashlib.sha256(command.encode()).hexdigest()[:10]
    if cli:
        return {"action": "approve", "rule_key": f"hermes-manager:cli:{digest}",
                "message": "Run a Hermes command that changes plugins, MCP servers or skills for every chat? "
                           f"It skips hermes-manager's own checks. Command: {command}"}
    return {"action": "approve", "rule_key": f"hermes-manager:install:{digest}",
            "message": f"Install code from the internet into Hermes? Command: {command}"}


# ---- the trust setting ------------------------------------------------------------------------------
# trust_own_items lifts the card on Hermes' own plugins, so Hermes writing it by hand would undo the card
# it is meant to keep. Writing it through a file or shell tool asks. A speed bump, not a sandbox.

def setting_card(tool_name: str, args: dict) -> Optional[dict]:
    if tool_name not in ("write_file", "patch", "terminal", "execute_code") or not isinstance(args, dict):
        return None
    try:
        blob = json.dumps(args)
    except (TypeError, ValueError):
        return None
    if "trust_own_items" not in blob:
        return None
    code = str(args.get("command") or args.get("code") or "")
    if tool_name in ("terminal", "execute_code") and not (writes(code) or CONFIG_SET.search(code)):
        return None   # reading it is fine
    shown = code or str(args.get("path") or "")
    if len(shown) > CARD_MAX:
        return TOO_LONG
    digest = hashlib.sha256(blob.encode()).hexdigest()[:10]
    return {"action": "approve", "rule_key": f"hermes-manager:setting:{digest}",
            "message": "Change hermes-manager's trust_own_items setting? When it is true, Hermes turns on and "
                       f"edits plugins it made with no card. {tool_name}: {shown}"}


# ---- edits to plugin files --------------------------------------------------------------------
# Editing a plugin the user made (anything not in the ledger) needs their approval. So does editing a plugin
# Hermes made while it is turned on (the edit runs at the next reload), unless trust_own_items is set.
WRITE_OPS = re.compile(r">|\btee\b|\bsed\s+-i|\bperl\s+-p?i|\bmv\b|\bcp\b|\brm\b|\brsync\b|\btruncate\b|\bpatch\b"
                       r"|\bgit\s+(?:apply|checkout|restore|reset)\b|\bpython3?\b|\bnode\b|\bln\b|\bchmod\b|\btouch\b"
                       r"|write_text|open\([^)]*['\"][wa]")
CONFIG_SET = re.compile(r"\bconfig\s+set\b")
_HARMLESS = re.compile(r"&?\d*>>?\s*/dev/null\b|\d*>&\d+")   # 2>/dev/null, 2>&1: not a write


def writes(code: str) -> bool:
    return bool(WRITE_OPS.search(_HARMLESS.sub(" ", code)))


def _plugin_keys(text: str, paths: set[Path]) -> set[str]:
    root = common.home() / "plugins"
    pats = {str(root), "~/.hermes/plugins", "$HOME/.hermes/plugins", "${HOME}/.hermes/plugins", ".hermes/plugins"}
    keys: set[str] = set()
    for base in pats:
        for m in re.finditer(re.escape(base) + r"/([A-Za-z0-9_.-]+)", text):
            keys.add(m.group(1))
    # Resolved paths: every way of naming the folder (workdir, cd, /./, symlinks) ends up here.
    bases = {b: None for b in _both(str(root))}
    try:
        for d in root.iterdir():
            if d.is_symlink():   # my-plugin -> ~/code/my-plugin is also edited through its real path
                for real in _both(os.path.abspath(root / d.readlink())) | {d.resolve()}:
                    bases[real] = d.name
    except OSError:
        pass
    for p in paths:
        for base, name in bases.items():
            if not _under(p, base):
                continue
            if name:
                keys.add(name)
            elif p == base:
                keys.add("*")   # the whole plugins folder
            else:
                keys.add(p.relative_to(base).parts[0])
    if "*" in keys:
        keys |= {d.name for d in root.iterdir() if d.is_dir()} if root.is_dir() else set()
    return {k for k in keys if k not in (".", "..", "*") and (root / k).is_dir()}


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
        if not writes(str(args.get("command") or args.get("code") or "")):
            return None
    keys = _plugin_keys(text, touched(tool_name, args, guess_home=True))
    theirs = sorted(k for k in keys if not ledger.owned("plugin", k))
    live_own = [] if common.trust_own_items() else sorted(
        k for k in keys if ledger.owned("plugin", k) and k in common.enabled_plugins())
    if not theirs and not live_own:
        return None
    what = str(args.get("command") or args.get("code") or args.get("path") or "")
    if len(what) > CARD_MAX:
        return TOO_LONG
    # "Always allow" covers one file, not the whole plugin.
    if args.get("path") and tool_name in ("write_file", "patch"):
        grain, scope = str(Path(str(args["path"])).expanduser().resolve()), "this file"
    else:   # terminal / execute_code / multi-file patch: no single file, so pin the exact text
        grain, scope = str(args.get("command") or args.get("code") or args.get("patch") or ""), "this exact command"
    sha8 = hashlib.sha256(grain.encode()).hexdigest()[:8]
    if theirs:
        ask = f"Edit your plugin {', '.join(theirs)}? Hermes didn't make it."
    else:
        ask = (f"Edit {', '.join(live_own)}, a plugin Hermes made that is turned on? The change runs at the next "
               "reload.")
    return {"action": "approve", "rule_key": f"hermes-manager:edit:{','.join(theirs or live_own)}:{sha8}",
            "message": f"{ask} {tool_name}: {what}. Always allow covers only {scope}."}
