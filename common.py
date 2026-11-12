"""Shared helpers for the hermes-manager tools: Hermes home, the main-chat guard and config edits."""

from __future__ import annotations

import hashlib
import json
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


def plugins_dir() -> Path:
    """~/.hermes/plugins (created if missing), where user plugins live."""
    folder = home() / "plugins"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


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


SETTINGS_ID = "hermes-manager"


def setting(key: str, default: Any = None) -> Any:
    """``plugins.entries.hermes-manager.settings.<key>`` as written in config.yaml (read fresh each call)."""
    try:
        node: Any = read_config()
        for part in ("plugins", "entries", SETTINGS_ID, "settings", key):
            node = node.get(part) if isinstance(node, dict) else None
        return default if node is None else node
    except Exception:
        return default


def trust_own_items() -> bool:
    """True only when the user set ``trust_own_items: true``: then Hermes turns on and edits plugins it made
    with no card. Off by default, and anything but a real ``true`` counts as off (fails toward a card)."""
    return setting("trust_own_items") is True


def enabled_plugins() -> set[str]:
    """Keys in ``plugins.enabled``: the plugins that load on the next reload."""
    try:
        plugins = read_config().get("plugins")
        enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
        return {str(k) for k in enabled} if isinstance(enabled, list) else set()
    except Exception:
        return set()


def clip(text: Any, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:8]
