from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from .browser import AuthRequired, BrowserSession
from .config import BLOCKED_URL_TOKENS, PORTAL_HOST, PORTAL_PATH_PREFIX, SAFE_REVEAL_LABELS, Settings
from .storage import StateStore


@dataclass(slots=True)
class CrawlResult:
    run_id: str
    pages_visited: int
    pending: int
    exhausted_errors: int
    resumed: bool


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def eligible_url(url: str) -> bool:
    try:
        p = urlsplit(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if p.hostname is None or p.hostname.lower() != PORTAL_HOST:
        return False
    if not p.path.lower().startswith(PORTAL_PATH_PREFIX):
        return False
    low = canonical_url(url).lower()
    if "authentication/login" in low:
        return False
    return not any(token in low for token in BLOCKED_URL_TOKENS)


def response_kind(url: str, content_type: str, content_disposition: str) -> tuple[str, str] | None:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    cd = (content_disposition or "").lower()
    low_url = url.lower()

    if "application/json" in ct or ct.endswith("+json"):
        return "json", ".json"
    if "application/pdf" in ct or low_url.endswith(".pdf"):
        return "pdf", ".pdf"

    attachment_like = "attachment" in cd or any(
        token in low_url for token in ("/attachment", "/document", "/download", "/file/")
    )
    if attachment_like:
        ext = mimetypes.guess_extension(ct) or Path(urlsplit(url).path).suffix or ".bin"
        return "attachments", ext[:12]

    if ct in ("text/plain", "text/csv", "application/xml", "text/xml"):
        return "attachments", mimetypes.guess_extension(ct) or ".txt"
    return None


def _extract_hrefs(page) -> list[str]:
    try:
        values = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href).filter(Boolean)")
        return [str(v) for v in values]
    except Exception:
        return []


def _safe_reveal(page, max_clicks: int = 20) -> int:
    clicks = 0
    for _ in range(max_clicks):
        clicked = False
        for label in SAFE_REVEAL_LABELS:
            try:
                loc = page.get_by_text(re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)).first
                if loc.count() and loc.is_visible() and loc.is_enabled():
                    loc.click(timeout=1200)
                    page.wait_for_timeout(350)
                    clicks += 1
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            break
    return clicks


def _settle_and_scroll(page, settle_ms: int) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    stable = 0
    last = -1
    for _ in range(8):
        try:
            height = int(page.evaluate("document.documentElement.scrollHeight"))
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            page.wait_for_timeout(settle_ms)
        except Exception:
            break
        if height == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = height
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


class Crawler:
    def __init__(self, settings: Settings, store: StateStore):
        self.settings = settings
        self.store = store

    def _capture_response(self, response) -> None:
        try:
            if response.status < 200 or response.status >= 400:
                return
            headers = response.headers
            content_type = headers.get("content-type", "")
            content_disposition = headers.get("content-disposition", "")
            classified = response_kind(response.url, content_type, content_disposition)
            if not classified:
                return
            kind, ext = classified
            data = response.body()
            if not data:
                return
            self.store.save_artifact(
                kind=kind,
                data=data,
                source_url=response.url,
                content_type=content_type,
                extension=ext,
            )
        except Exception:
            return

    def run(self, *, interactive: bool = True, expand_safe: bool = True) -> CrawlResult:
        run_id, resumed = self.store.begin_run()
        visited = 0

        with BrowserSession(self.settings, headless=not interactive) as browser:
            page = browser.page
            assert page is not None
            page.on("response", self._capture_response)
            start_url = canonical_url(browser.ensure_authenticated(interactive=interactive))
            if eligible_url(start_url):
                self.store.enqueue(start_url, 0, run_id)

            for href in _extract_hrefs(page):
                absolute = canonical_url(urljoin(page.url, href))
                if eligible_url(absolute):
                    self.store.enqueue(absolute, 1, run_id)

            while visited < self.settings.max_pages:
                rows = self.store.pending(run_id, self.settings.max_retries, limit=1)
                if not rows:
                    break
                row = rows[0]
                url = row["url"]
                depth = int(row["depth"])
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    if browser.looks_logged_out(page.url):
                        browser.ensure_authenticated(interactive=interactive)
                        page.goto(url, wait_until="domcontentloaded")

                    _settle_and_scroll(page, self.settings.settle_ms)
                    if expand_safe and _safe_reveal(page):
                        _settle_and_scroll(page, self.settings.settle_ms)

                    html = page.content().encode("utf-8")
                    path, digest = self.store.save_artifact(
                        kind="html",
                        data=html,
                        source_url=url,
                        content_type="text/html; charset=utf-8",
                        extension=".html",
                    )
                    title = page.title()
                    self.store.mark_page_done(
                        url,
                        title=title,
                        local_path=str(path.relative_to(self.store.root)),
                        digest=digest,
                    )

                    if depth < self.settings.max_depth:
                        for href in _extract_hrefs(page):
                            absolute = canonical_url(urljoin(page.url, href))
                            if eligible_url(absolute):
                                self.store.enqueue(absolute, depth + 1, run_id)
                    visited += 1
                except AuthRequired:
                    raise
                except Exception as exc:
                    self.store.mark_page_error(url, f"{type(exc).__name__}: {exc}")

        pending = self.store.pending_count(run_id, self.settings.max_retries)
        exhausted = self.store.exhausted_error_count(run_id, self.settings.max_retries)
        if pending == 0:
            self.store.finish_run(run_id)
        return CrawlResult(
            run_id=run_id,
            pages_visited=visited,
            pending=pending,
            exhausted_errors=exhausted,
            resumed=resumed,
        )
