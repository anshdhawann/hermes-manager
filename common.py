"""Shared helpers for the hermes-manager tools: Hermes home, the main-chat guard and config edits."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

# Chat sources a person types into. The main chat is the newest pinned one among them.
CHAT_SOURCES = ("desktop", "gui", "local", "mobile-web", "tui", "telegram")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

MAIN_ONLY = ("This changes Hermes for every chat, so it runs from the main chat only (the user's pinned chat). "
             "Ask the user to make the change there.")


def home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def main_chat_id() -> Optional[str]:
    """The newest pinned chat in the default profile. None when state.db cannot be read."""
    try:
        con = sqlite3.connect(f"file:{home() / 'state.db'}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        q = ",".join("?" * len(CHAT_SOURCES))
        row = con.execute(f"SELECT id FROM sessions WHERE pinned=1 AND archived=0 AND source IN ({q}) "
                          "AND coalesce(profile_name,'default')='default' ORDER BY started_at DESC LIMIT 1",
                          CHAT_SOURCES).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def not_main(session_id: Optional[str]) -> Optional[str]:
    """An error line when ``session_id`` is not the main chat (fails closed), else None."""
    main = main_chat_id()
    if not session_id or not main or str(session_id) != str(main):
        return MAIN_ONLY
    return None


def read_config() -> dict[str, Any]:
    """config.yaml as written (no defaults merged in), safe to change and save back."""
    from hermes_cli.config import read_raw_config
    return read_raw_config() or {}


def save_config(cfg: dict[str, Any]) -> None:
    from hermes_cli.config import save_config as _save
    _save(cfg)


def clip(text: Any, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"
