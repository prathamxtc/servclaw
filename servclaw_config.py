"""Shared configuration loader/saver for Servclaw.

Primary config source is servclaw.json in project root.
Environment variables are used only as fallback/migration input.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent / "servclaw.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_config() -> dict[str, Any]:
    return {
        "meta": {
            "lastTouchedVersion": "2026.3.18",
            "lastTouchedAt": _now_iso(),
        },
        "agents": {
            "defaults": {
                "model": {
                    "primary": "openai/gpt-5-mini",
                },
                "workspace": "/app/workspace",
                "compaction": {
                    "mode": "safeguard",
                },
            }
        },
        "channels": {
            "telegram": {
                "enabled": True,
                "token": "",
                "allowedUserIds": [],
                "streaming": "off",
            },
            "discord": {
                "enabled": False,
                "token": "",
                "allowedUserIds": [],
                "streaming": "off",
            },
        },
        "secrets": {
            "openaiApiKey": "",
        },
    }


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    cfg = default_config()
    # Fallback migration from environment (legacy)
    cfg["secrets"]["openaiApiKey"] = os.getenv("OPENAI_API_KEY", "")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg["channels"]["telegram"]["token"] = tg_token

    raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    ids: list[int] = []
    for part in raw_ids.split(","):
        s = part.strip()
        if s.isdigit():
            ids.append(int(s))
    cfg["channels"]["telegram"]["allowedUserIds"] = ids
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    cfg.setdefault("meta", {})
    cfg["meta"]["lastTouchedAt"] = _now_iso()
    if "lastTouchedVersion" not in cfg["meta"]:
        cfg["meta"]["lastTouchedVersion"] = "2026.3.18"
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def get_openai_api_key(cfg: dict[str, Any]) -> str:
    return str(cfg.get("secrets", {}).get("openaiApiKey", "") or "")


def get_model_name(cfg: dict[str, Any], fallback: str = "gpt-5-mini") -> str:
    """Return the primary model name from config, e.g. agents.defaults.model.primary.

    The stored value may include a provider prefix like 'openai/gpt-5-mini'.
    Strip the prefix before returning since the OpenAI client only wants the bare model ID.
    """
    raw = (
        cfg.get("agents", {})
        .get("defaults", {})
        .get("model", {})
        .get("primary", "")
        or ""
    )
    name = raw.split("/", 1)[-1] if "/" in raw else raw
    return name.strip() or fallback


def get_telegram_token(cfg: dict[str, Any]) -> str:
    return str(cfg.get("channels", {}).get("telegram", {}).get("token", "") or "")


def get_telegram_allowed_user_ids(cfg: dict[str, Any]) -> set[int]:
    raw = cfg.get("channels", {}).get("telegram", {}).get("allowedUserIds", [])
    out: set[int] = set()
    if isinstance(raw, list):
        for item in raw:
            try:
                val = int(item)
                out.add(val)
            except Exception:
                continue
    return out


def get_discord_token(cfg: dict[str, Any]) -> str:
    return str(cfg.get("channels", {}).get("discord", {}).get("token", "") or "")


def get_discord_allowed_user_ids(cfg: dict[str, Any]) -> set[int]:
    raw = cfg.get("channels", {}).get("discord", {}).get("allowedUserIds", [])
    out: set[int] = set()
    if isinstance(raw, list):
        for item in raw:
            try:
                val = int(item)
                out.add(val)
            except Exception:
                continue
    return out
