from __future__ import annotations

import hashlib
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
    checkpoint_interrupted: bool = False
    checkpoint_resumed: bool = False


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
    if ct in ("text/html", "application/xhtml+xml"):
        return "html", ".html"

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
    out: list[str] = []
    for frame in page.frames:
        try:
            values = frame.eval_on_selector_all(
                "a[href],[data-href],[data-url],[data-route]",
                """els => els.map(e => e.href || e.dataset.href || e.dataset.url || e.dataset.route)
                         .filter(Boolean)""",
            )
            out.extend(str(v) for v in values)
        except Exception:
            continue
    return list(dict.fromkeys(out))


def _extract_controls(page) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for frame in page.frames:
        try:
            rows = frame.eval_on_selector_all(
                "a,button,[role=button],[role=tab]",
                """els => els.map(e => ({
                    kind: e.getAttribute('role') || e.tagName.toLowerCase(),
                    label: (e.getAttribute('aria-label') || e.innerText || e.textContent || '').trim(),
                    target: e.href || e.dataset?.href || e.dataset?.url || e.dataset?.route || ''
                })).filter(x => x.label || x.target)""",
            )
            for row in rows:
                out.append({
                    "kind": str(row.get("kind") or "control"),
                    "label": " ".join(str(row.get("label") or "").split())[:500],
                    "target": str(row.get("target") or ""),
                })
        except Exception:
            continue
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in out:
        unique[(row["kind"], row["label"], row["target"])] = row
    return list(unique.values())


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


def _trigger_explicit_downloads(page, max_clicks: int = 100) -> int:
    selectors = (
        "a[download]",
        "button",
        "[role=button]",
    )
    labels = re.compile(r"^\s*(?:download(?: pdf)?|hent(?: dokument| fil| pdf)?)\s*$", re.I)
    clicked = 0
    seen: set[tuple[str, str]] = set()
    for selector in selectors:
        try:
            locators = page.locator(selector)
            count = min(locators.count(), max_clicks)
        except Exception:
            continue
        for index in range(count):
            locator = locators.nth(index)
            try:
                label = " ".join((locator.get_attribute("aria-label") or locator.inner_text() or "").split())
                href = locator.get_attribute("href") or ""
                identity = (label, href)
                if identity in seen:
                    continue
                seen.add(identity)
                if selector != "a[download]" and not labels.match(label):
                    continue
                if not locator.is_visible() or not locator.is_enabled():
                    continue
                with page.expect_download(timeout=2500):
                    locator.click(timeout=2000)
                clicked += 1
                if clicked >= max_clicks:
                    return clicked
            except Exception:
                continue
    return clicked
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
        self._run_id = ""

    def _capture_response(self, response) -> None:
        try:
            if response.status < 200 or response.status >= 400:
                return
            if not eligible_url(response.url):
                return
            if self._run_id:
                self.store.observe(
                    run_id=self._run_id,
                    page_url=response.frame.url if response.frame else response.url,
                    kind="response",
                    target_url=response.url,
                )
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

    def _capture_download(self, download) -> None:
        try:
            # A portal-generated attachment may be served by a separate CDN.
            # Trust the initiating authenticated portal page, never an auth or
            # external page by itself.
            if not eligible_url(download.page.url):
                return
            path = download.path()
            if path is None:
                return
            filename = download.suggested_filename or Path(urlsplit(download.url).path).name
            extension = Path(filename).suffix[:12] or ".bin"
            data = Path(path).read_bytes()
            kind = "pdf" if extension.lower() == ".pdf" else "attachments"
            self.store.save_artifact(
                kind=kind,
                data=data,
                source_url=download.url,
                content_type="application/pdf" if kind == "pdf" else "application/octet-stream",
                extension=extension,
            )
            if self._run_id:
                self.store.observe(
                    run_id=self._run_id,
                    page_url=download.page.url,
                    kind="download",
                    label=filename,
                    target_url=download.url,
                )
        except Exception:
            return

    def _observe_page(self, page, run_id: str) -> None:
        for row in _extract_controls(page):
            target = row["target"]
            if target:
                target = canonical_url(urljoin(page.url, target))
            self.store.observe(
                run_id=run_id,
                page_url=page.url,
                kind=f"control:{row['kind']}",
                label=row["label"],
                target_url=target,
            )

    def _crawl_readonly_tabs(self, page, run_id: str, depth: int, max_tabs: int = 100) -> int:
        clicked = 0
        seen: set[tuple[str, str]] = set()
        for frame in list(page.frames):
            try:
                tabs = frame.locator("[role=tab]")
                initial_count = min(tabs.count(), max_tabs - clicked)
                labels = []
                for index in range(initial_count):
                    locator = tabs.nth(index)
                    label = " ".join(
                        (locator.get_attribute("aria-label") or locator.inner_text() or "").split()
                    )
                    if label and label not in labels:
                        labels.append(label)
            except Exception:
                continue
            for label in labels:
                try:
                    identity = (frame.url, label)
                    if identity in seen:
                        continue
                    seen.add(identity)

                    # Resolve by label after every click. Epic re-renders the
                    # whole tablist, so retaining an index can silently point
                    # at a different or temporarily empty element.
                    locator = None
                    for _ in range(3):
                        current = frame.locator("[role=tab]")
                        for index in range(min(current.count(), max_tabs)):
                            candidate = current.nth(index)
                            candidate_label = " ".join(
                                (
                                    candidate.get_attribute("aria-label")
                                    or candidate.inner_text()
                                    or ""
                                ).split()
                            )
                            if candidate_label == label:
                                try:
                                    candidate.scroll_into_view_if_needed(timeout=1200)
                                except Exception:
                                    pass
                                if candidate.is_visible() and candidate.is_enabled():
                                    locator = candidate
                                break
                        if locator is not None:
                            break
                        page.wait_for_timeout(250)
                    if locator is None:
                        self.store.observe(
                            run_id=run_id,
                            page_url=page.url,
                            kind="tab_unavailable",
                            label=label,
                        )
                        continue

                    locator.click(timeout=2500)
                    page.wait_for_timeout(450)
                    _settle_and_scroll(page, self.settings.settle_ms)
                    _safe_reveal(page)
                    _trigger_explicit_downloads(page)
                    self._observe_page(page, run_id)

                    digest = hashlib.sha256(f"{page.url}|{label}".encode()).hexdigest()[:16]
                    parts = urlsplit(page.url)
                    query = f"{parts.query}&" if parts.query else ""
                    source_url = urlunsplit(
                        (parts.scheme, parts.netloc, parts.path, f"{query}__minsp_view=tab-{digest}", "")
                    )
                    self.store.save_artifact(
                        kind="html",
                        data=frame.content().encode("utf-8"),
                        source_url=source_url,
                        content_type="text/html; charset=utf-8",
                        extension=".html",
                    )
                    self.store.observe(
                        run_id=run_id,
                        page_url=page.url,
                        kind="tab_snapshot",
                        label=label,
                        target_url=source_url,
                    )
                    if depth < self.settings.max_depth:
                        for href in _extract_hrefs(page):
                            absolute = canonical_url(urljoin(page.url, href))
                            if eligible_url(absolute):
                                self.store.enqueue(absolute, depth + 1, run_id)
                    clicked += 1
                    if clicked >= max_tabs:
                        return clicked
                except Exception:
                    self.store.observe(
                        run_id=run_id,
                        page_url=page.url,
                        kind="tab_error",
                        label=label,
                    )
                    continue
        return clicked

    def run(
        self,
        *,
        interactive: bool = True,
        expand_safe: bool = True,
        browser: BrowserSession | None = None,
        checkpoint_probe: bool = False,
    ) -> CrawlResult:
        run_id, resumed = self.store.begin_run()

        if browser is not None:
            return self._run_with_browser(
                browser,
                run_id=run_id,
                resumed=resumed,
                interactive=interactive,
                expand_safe=expand_safe,
                checkpoint_probe=checkpoint_probe,
            )

        with BrowserSession(self.settings, headless=not interactive) as owned_browser:
            return self._run_with_browser(
                owned_browser,
                run_id=run_id,
                resumed=resumed,
                interactive=interactive,
                expand_safe=expand_safe,
                checkpoint_probe=checkpoint_probe,
            )

    def _run_with_browser(
        self,
        browser: BrowserSession,
        *,
        run_id: str,
        resumed: bool,
        interactive: bool,
        expand_safe: bool,
        checkpoint_probe: bool,
    ) -> CrawlResult:
        visited = 0
        checkpoint_resumed = False
        page = browser.page
        assert page is not None
        self._run_id = run_id
        context = browser.context
        assert context is not None
        context.on("response", self._capture_response)
        context.on("page", lambda new_page: new_page.on("download", self._capture_download))
        for open_page in context.pages:
            open_page.on("download", self._capture_download)

        if self.store.get_meta("checkpoint_probe_pending") == run_id:
            checkpoint_resumed = True
            self.store.set_meta("checkpoint_probe_pending", None)
            self.store.set_meta(
                "checkpoint_probe_completed",
                f"run_id={run_id};resumed_at={self.store.get_meta('active_run_id')}",
            )

        # Authentication and the crawl intentionally share the exact same
        # BrowserSession. Do not close/reopen Chrome between MitID and crawling.
        start_url = canonical_url(browser.ensure_authenticated(interactive=interactive))
        if eligible_url(start_url):
            self.store.enqueue(start_url, 0, run_id)

        self._observe_page(page, run_id)

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
                if not browser.is_authenticated_portal_url(page.url):
                    # If the portal expired the session, recover inside this
                    # same browser context. A fresh MitID login is requested
                    # only when the portal actually requires it.
                    browser.ensure_authenticated(interactive=interactive)
                    page.goto(url, wait_until="domcontentloaded")

                _settle_and_scroll(page, self.settings.settle_ms)
                if expand_safe and _safe_reveal(page):
                    _settle_and_scroll(page, self.settings.settle_ms)

                _trigger_explicit_downloads(page)

                self._observe_page(page, run_id)
                self._crawl_readonly_tabs(page, run_id, depth)

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
                if (
                    checkpoint_probe
                    and not checkpoint_resumed
                    and not self.store.get_meta("checkpoint_probe_completed")
                ):
                    self.store.set_meta("checkpoint_probe_pending", run_id)
                    return CrawlResult(
                        run_id=run_id,
                        pages_visited=visited,
                        pending=self.store.pending_count(run_id, self.settings.max_retries),
                        exhausted_errors=self.store.exhausted_error_count(run_id, self.settings.max_retries),
                        resumed=resumed,
                        checkpoint_interrupted=True,
                        checkpoint_resumed=False,
                    )
            except AuthRequired:
                raise
            except Exception as exc:
                downloaded = self.store.artifact_for_source(url)
                if downloaded is not None:
                    self.store.mark_page_done(
                        url,
                        title="Downloaded attachment",
                        local_path=downloaded["local_path"],
                        digest=downloaded["sha256"],
                    )
                    visited += 1
                else:
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
            checkpoint_interrupted=False,
            checkpoint_resumed=checkpoint_resumed,
        )
