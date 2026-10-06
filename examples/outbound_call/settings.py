from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit

from dialt_recipes.twilio import TwilioBridgeSettings


_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
DEFAULT_BUSINESS_NAME = "Northstar Home Services"
DEFAULT_DIALT_URL = "wss://api.dialt.com/v1/realtime"


def _clock(value: str, name: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} must be HH:MM in 24-hour time") from None


@dataclass(frozen=True)
class Settings(TwilioBridgeSettings):
    """Configuration shared by the outbound launcher and authenticated callback host."""

    dialt_url: str = DEFAULT_DIALT_URL
    twilio_account_sid: str = ""
    twilio_from_number: str = ""
    business_name: str = DEFAULT_BUSINESS_NAME
    state_db: Path = Path("outbound_calls.sqlite3")
    voice: str | None = None
    machine_detection: str = "human-only"
    ring_timeout_s: int = 30
    call_time_limit_s: int = 180
    allowed_countries: tuple[str, ...] = ("US",)
    calling_window_start: time = time(9, 0)
    calling_window_end: time = time(20, 0)
    max_active_calls: int = 1

    @classmethod
    def from_env(cls) -> Settings:
        required = (
            "DIALT_API_KEY",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
            "PUBLIC_BASE_URL",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        public_base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")
        parsed = urlsplit(public_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise RuntimeError("PUBLIC_BASE_URL must be an absolute https origin without a path query")
        if parsed.path not in {"", "/"}:
            raise RuntimeError("PUBLIC_BASE_URL must be an origin without a path")

        account_sid = os.environ["TWILIO_ACCOUNT_SID"].strip()
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account_sid):
            raise RuntimeError("TWILIO_ACCOUNT_SID must be an AC-prefixed Twilio Account SID")
        from_number = os.environ["TWILIO_FROM_NUMBER"].strip()
        if not _E164.fullmatch(from_number):
            raise RuntimeError("TWILIO_FROM_NUMBER must be in E.164 format")

        configured_business_name = os.environ.get("OUTBOUND_BUSINESS_NAME")
        if configured_business_name is None:
            business_name = DEFAULT_BUSINESS_NAME
        else:
            business_name = configured_business_name.strip()
            if not business_name:
                raise RuntimeError("OUTBOUND_BUSINESS_NAME must be non-empty when supplied")

        machine_detection = os.environ.get("OUTBOUND_MACHINE_DETECTION", "human-only").strip().lower()
        if machine_detection not in {"human-only", "off"}:
            raise RuntimeError("OUTBOUND_MACHINE_DETECTION must be human-only or off")

        try:
            ring_timeout_s = int(os.environ.get("OUTBOUND_RING_TIMEOUT_S", "30"))
            call_time_limit_s = int(os.environ.get("OUTBOUND_CALL_TIME_LIMIT_S", "180"))
            max_active_calls = int(os.environ.get("OUTBOUND_MAX_ACTIVE_CALLS", "1"))
        except ValueError:
            raise RuntimeError(
                "OUTBOUND_RING_TIMEOUT_S, OUTBOUND_CALL_TIME_LIMIT_S, and "
                "OUTBOUND_MAX_ACTIVE_CALLS must be integers"
            ) from None
        if not 10 <= ring_timeout_s <= 600:
            raise RuntimeError("OUTBOUND_RING_TIMEOUT_S must be between 10 and 600")
        if not 30 <= call_time_limit_s <= 600:
            raise RuntimeError("OUTBOUND_CALL_TIME_LIMIT_S must be between 30 and 600")
        if not 1 <= max_active_calls <= 100:
            raise RuntimeError("OUTBOUND_MAX_ACTIVE_CALLS must be between 1 and 100")

        allowed_countries = tuple(
            part.strip().upper()
            for part in os.environ.get("OUTBOUND_ALLOWED_COUNTRIES", "US").split(",")
            if part.strip()
        )
        if not allowed_countries or any(not re.fullmatch(r"[A-Z]{2}", item)
                                        for item in allowed_countries):
            raise RuntimeError("OUTBOUND_ALLOWED_COUNTRIES must contain ISO alpha-2 codes")

        calling_window_start = _clock(
            os.environ.get("OUTBOUND_CALLING_WINDOW_START", "09:00"),
            "OUTBOUND_CALLING_WINDOW_START",
        )
        calling_window_end = _clock(
            os.environ.get("OUTBOUND_CALLING_WINDOW_END", "20:00"),
            "OUTBOUND_CALLING_WINDOW_END",
        )
        if calling_window_start >= calling_window_end:
            raise RuntimeError("the outbound calling window must start before it ends")

        return cls(
            dialt_api_key=os.environ["DIALT_API_KEY"].strip(),
            twilio_auth_token=os.environ["TWILIO_AUTH_TOKEN"].strip(),
            public_base_url=public_base_url,
            dialt_url=os.environ.get("DIALT_URL", DEFAULT_DIALT_URL).strip(),
            twilio_account_sid=account_sid,
            twilio_from_number=from_number,
            business_name=business_name,
            state_db=Path(os.environ.get("OUTBOUND_STATE_DB", "outbound_calls.sqlite3")),
            voice=os.environ.get("DIALT_VOICE") or None,
            machine_detection=machine_detection,
            ring_timeout_s=ring_timeout_s,
            call_time_limit_s=call_time_limit_s,
            allowed_countries=allowed_countries,
            calling_window_start=calling_window_start,
            calling_window_end=calling_window_end,
            max_active_calls=max_active_calls,
        )
