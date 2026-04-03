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
            "tavilyApiKey": "",
        },
        "skills": {
            "tavily_search": {
                "enabled": False,
            },
        },
        "user": {
            "city": "",
            "country": "",
            "timezone": "",
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


def _city_country_to_timezone(city: str, country: str) -> str | None:
    """Auto-derive IANA timezone from city+country using pytz.country_timezones.

    Single-timezone countries (India, Japan, etc.) resolve immediately.
    Multi-timezone countries (US, Australia) try to match city against
    the IANA city component (e.g. "New_York" → "America/New_York").
    Returns the IANA string or None if nothing could be matched.
    """
    try:
        import pytz
    except ImportError:
        return None

    lower_country = (country or "").strip().lower()
    iso_code: str | None = None
    if lower_country:
        for code, name in pytz.country_names.items():
            n = name.lower()
            if n == lower_country or lower_country in n or n in lower_country:
                iso_code = code
                break
    if not iso_code:
        return None

    tzs: list[str] = pytz.country_timezones.get(iso_code, [])
    if not tzs:
        return None
    if len(tzs) == 1:
        return tzs[0]

    # Multiple zones: match the city component of the IANA name
    city_norm = (city or "").strip().lower().replace(" ", "_")
    if city_norm:
        for tz in tzs:
            tz_city = tz.split("/", 1)[-1].lower()
            if city_norm in tz_city or tz_city in city_norm:
                return tz
        city_spaced = city_norm.replace("_", " ")
        for tz in tzs:
            tz_city = tz.split("/", 1)[-1].lower().replace("_", " ")
            if city_spaced in tz_city or tz_city in city_spaced:
                return tz

    return tzs[0]  # best guess: country's primary/first zone


def get_user_timezone(cfg: dict[str, Any]) -> str:
    """Return the user's IANA timezone — fully automatic, no user input needed.

    Lookup order:
    1. cfg.user.timezone (already saved)
    2. Auto-derived from cfg.user.city + cfg.user.country (+ saves back to cfg)
    3. workspace/USER.md Timezone field (legacy onboarding path)
    4. "UTC" fallback
    """
    user = cfg.get("user", {})
    tz = (user.get("timezone") or "").strip()
    if tz:
        return tz

    city = (user.get("city") or "").strip()
    country = (user.get("country") or "").strip()
    if city or country:
        derived = _city_country_to_timezone(city, country)
        if derived:
            cfg.setdefault("user", {})["timezone"] = derived
            save_config(cfg)
            return derived

    # Legacy: try workspace/USER.md for users set up before location tracking
    workspace_user_md = Path(__file__).resolve().parent / "workspace" / "USER.md"
    if workspace_user_md.exists():
        try:
            import re as _re
            content = workspace_user_md.read_text(encoding="utf-8")
            m = _re.search(r"Timezone\s*:\s*`?([A-Za-z][A-Za-z/_+-]+)`?", content)
            if m:
                candidate = m.group(1).strip()
                if candidate.lower() not in ("unset", "none", "(unset)", "utc"):
                    try:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(candidate)
                        cfg.setdefault("user", {})["timezone"] = candidate
                        save_config(cfg)
                        return candidate
                    except Exception:
                        pass
        except Exception:
            pass

    return "UTC"


def update_user_location(cfg: dict[str, Any], city: str, country: str) -> bool:
    """Save city/country to cfg and auto-derive+store timezone.

    Modifies cfg in-place. Returns True if anything changed.
    Caller is responsible for calling save_config(cfg) afterwards.
    """
    user = cfg.setdefault("user", {})
    city = (city or "").strip()
    country = (country or "").strip()
    changed = False
    if city and user.get("city") != city:
        user["city"] = city
        changed = True
    if country and user.get("country") != country:
        user["country"] = country
        changed = True
    # Re-derive timezone whenever city or country changes, or if timezone is missing
    if changed or not user.get("timezone"):
        derived = _city_country_to_timezone(
            city or user.get("city", ""),
            country or user.get("country", ""),
        )
        if derived and user.get("timezone") != derived:
            user["timezone"] = derived
            changed = True
    return changed


def get_skill_config(cfg: dict[str, Any], skill_id: str) -> dict[str, Any]:
    """Return the config block for a skill, e.g. cfg['skills']['tavily_search']."""
    return dict(cfg.get("skills", {}).get(skill_id, {}))


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
