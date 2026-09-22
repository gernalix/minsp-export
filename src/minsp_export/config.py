from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LOGIN_URL = "https://minsundhedsplatform.dk/mychartppr1/Authentication/Login?"
PORTAL_HOST = "minsundhedsplatform.dk"
PORTAL_PATH_PREFIX = "/mychartppr1/"

DEFAULT_PROFILE_DIR = Path.home() / ".local" / "share" / "minsp-export" / "chrome-profile"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "MinSP" / "export"

BLOCKED_URL_TOKENS = (
    "logout",
    "logoff",
    "signout",
    "delete",
    "removeaccount",
    "cancelappointment",
    "cancelvisit",
    "makepayment",
    "payment/submit",
    "composemessage",
    "sendmessage",
    "scheduleappointment",
    "schedulevisit",
    "refillrequest",
    "proxyaccess/request",
)

SAFE_REVEAL_LABELS = (
    "vis mere",
    "se mere",
    "show more",
    "load more",
)


@dataclass(slots=True)
class Settings:
    output_dir: Path = DEFAULT_OUTPUT_DIR
    profile_dir: Path = DEFAULT_PROFILE_DIR
    login_url: str = LOGIN_URL
    browser_channel: str = "chrome"
    max_pages: int = 10_000
    max_depth: int = 24
    max_retries: int = 3
    navigation_timeout_ms: int = 45_000
    settle_ms: int = 900
    auth_wait_seconds: int = 15 * 60

    @property
    def normalized_db(self) -> Path:
        return self.output_dir / "normalized" / "health.sqlite"

    @property
    def markdown_path(self) -> Path:
        return self.output_dir / "text" / "complete-medical-record.md"
