from __future__ import annotations

import time
from pathlib import Path

from .config import Settings
from .storage import secure_dir


class AuthRequired(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, settings: Settings, *, headless: bool = False):
        self.settings = settings
        self.headless = headless
        self._playwright = None
        self.context = None
        self.page = None

    def __enter__(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is required. Install it using the Fedora system/user Python policy."
            ) from exc

        secure_dir(Path(self.settings.profile_dir))
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.profile_dir),
            channel=self.settings.browser_channel,
            headless=self.headless,
            accept_downloads=True,
            viewport={"width": 1440, "height": 1000},
        )
        self.context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.context is not None:
            self.context.close()
        if self._playwright is not None:
            self._playwright.stop()

    @staticmethod
    def looks_logged_out(url: str) -> bool:
        u = url.lower()
        return "authentication/login" in u or ("/login" in u and "mychartppr1" in u)

    def ensure_authenticated(self, *, interactive: bool = True) -> str:
        assert self.page is not None
        self.page.goto(self.settings.login_url, wait_until="domcontentloaded")
        if not self.looks_logged_out(self.page.url):
            return self.page.url
        if not interactive:
            raise AuthRequired("AUTH_REQUIRED: Min Sundhedsplatform session is not authenticated")

        print("AUTH_REQUIRED: complete the normal MitID login in the opened Chrome window.")
        print("No MitID credentials are read or stored by minsp-export.")
        deadline = time.monotonic() + self.settings.auth_wait_seconds
        while time.monotonic() < deadline:
            if not self.looks_logged_out(self.page.url):
                return self.page.url
            self.page.wait_for_timeout(1000)
        raise AuthRequired("AUTH_REQUIRED: interactive login window timed out")
