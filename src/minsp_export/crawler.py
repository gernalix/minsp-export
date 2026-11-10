from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from .browser import AuthRequired, BrowserSession
from .config import BLOCKED_URL_TOKENS, SAFE_REVEAL_LABELS, Settings, is_portal_url
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
    if not is_portal_url(url):
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


def _require_portal_page(page) -> None:
    if not eligible_url(page.url) or not BrowserSession.is_authenticated_portal_url(page.url):
        raise AuthRequired("The portal page is outside the authenticated capture boundary.")


def _extract_hrefs(page) -> list[str]:
    out: list[str] = []
    for frame in (page.main_frame,):
        try:
            values = frame.eval_on_selector_all(
                "a[href],[data-href],[data-url],[data-route]",
                """els => els.filter(e => !e.closest('a[download]'))
                         .map(e => e.href || e.dataset.href || e.dataset.url || e.dataset.route)
                         .filter(Boolean)""",
            )
            out.extend(str(v) for v in values)
        except Exception:
            continue
    return list(dict.fromkeys(out))


def _extract_controls(page) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for frame in (page.main_frame,):
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


def _non_submitting_control(locator) -> bool:
    return locator.evaluate("""element => (
        !element.closest('form,a[href]') &&
        (element.tagName !== 'BUTTON' || element.type === 'button') &&
        element.tagName !== 'INPUT'
    )""")


def _safe_reveal(page, max_clicks: int = 20) -> int:
    clicks = 0
    for _ in range(max_clicks):
        clicked = False
        for label in SAFE_REVEAL_LABELS:
            _require_portal_page(page)
            try:
                control = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
                if (control.count() != 1 or not control.is_visible() or not control.is_enabled()
                        or control.get_attribute("aria-expanded") != "false"
                        or not control.get_attribute("aria-controls")
                        or not _non_submitting_control(control)):
                    continue
                control.click(timeout=1200)
                page.wait_for_timeout(350)
                _require_portal_page(page)
                clicks += 1
                clicked = True
                break
            except AuthRequired:
                raise
            except Exception:
                _require_portal_page(page)
        if not clicked:
            break
    return clicks


READONLY_TABS = {
    "/mychartppr1/app/home": frozenset({"Appointments"}),
    "/mychartppr1/app/results": frozenset({"Laboratory"}),
}


def _allowed_tabs(url: str) -> frozenset[str]:
    return READONLY_TABS.get(urlsplit(url).path.rstrip("/").lower(), frozenset())


def _settle_and_scroll(page, settle_ms: int) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    _require_portal_page(page)
    page.wait_for_timeout(settle_ms)
    _require_portal_page(page)


class Crawler:
    def __init__(self, settings: Settings, store: StateStore):
        self.settings = settings
        self.store = store
        self._run_id = ""
        self._capture_page = None
        self._download_target = None

    def _guard_request(self, route) -> None:
        request = route.request
        if self._capture_page is not None and (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            or (request.is_navigation_request() and not eligible_url(request.url)
                and request.url != self._download_target)
        ):
            route.abort()
            return
        route.continue_()

    def _capture_response(self, response) -> None:
        try:
            if response.status < 200 or response.status >= 400:
                return
            page = self._capture_page
            if page is None or response.frame is not page.main_frame:
                return
            _require_portal_page(page)
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
            _require_portal_page(page)
            self.store.save_artifact(
                kind=kind,
                data=data,
                source_url=response.url,
                content_type=content_type,
                extension=ext,
            )
        except Exception:
            return

    def _capture_download(self, download, *, page, page_url: str, expected: str) -> None:
        if download.page is not page or download.url != expected or page.url != page_url:
            return
        _require_portal_page(page)
        path = download.path()
        _require_portal_page(page)
        if path is None or page.url != page_url:
            return
        filename = download.suggested_filename or Path(urlsplit(download.url).path).name
        extension = Path(filename).suffix[:12] or ".bin"
        data = Path(path).read_bytes()
        _require_portal_page(page)
        if page.url != page_url:
            return
        kind = "pdf" if extension.lower() == ".pdf" else "attachments"
        self.store.save_artifact(
            kind=kind, data=data, source_url=download.url,
            content_type="application/pdf" if kind == "pdf" else "application/octet-stream",
            extension=extension,
        )
        if self._run_id:
            self.store.observe(
                run_id=self._run_id, page_url=page_url, kind="download",
                label=filename, target_url=download.url,
            )

    def _trigger_explicit_downloads(self, page, max_clicks: int = 100) -> None:
        _require_portal_page(page)
        page_url = page.url
        links = page.locator("a[download][href]")
        seen: set[str] = set()
        for index in range(min(links.count(), max_clicks)):
            _require_portal_page(page)
            if page.url != page_url:
                return
            try:
                link = links.nth(index)
                href = link.get_attribute("href") or ""
                target = link.evaluate("element => element.href")
                parsed = urlsplit(target)
                if (not href or parsed.scheme != "https" or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None
                        or parsed.port not in (None, 443) or target in seen
                        or any(token in target.lower() for token in BLOCKED_URL_TOKENS)):
                    continue
                if not link.is_visible() or not link.is_enabled():
                    continue
                seen.add(target)
                self._download_target = target
                try:
                    with page.expect_download(
                        predicate=lambda download: download.page is page and download.url == target,
                        timeout=2500,
                    ) as pending:
                        _require_portal_page(page)
                        link.click(timeout=2000)
                    self._capture_download(pending.value, page=page, page_url=page_url, expected=target)
                finally:
                    self._download_target = None
            except AuthRequired:
                raise
            except Exception:
                _require_portal_page(page)

    def _observe_page(self, page, run_id: str) -> None:
        _require_portal_page(page)
        for row in _extract_controls(page):
            _require_portal_page(page)
            if row["kind"] == "tab" and row["label"] not in _allowed_tabs(page.url):
                self.store.observe(
                    run_id=run_id, page_url=page.url, kind="tab_unavailable", label=row["label"],
                )
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
        _require_portal_page(page)
        for frame in (page.main_frame,):
            try:
                tabs = frame.locator("[role=tab]")
                initial_count = min(tabs.count(), max_tabs - clicked)
                labels = []
                for index in range(initial_count):
                    locator = tabs.nth(index)
                    label = " ".join(
                        (locator.get_attribute("aria-label") or locator.inner_text() or "").split()
                    )
                    if label in _allowed_tabs(page.url) and label not in labels:
                        labels.append(label)
            except Exception:
                continue
            for label in labels:
                try:
                    identity = (frame.url, label)
                    if identity in seen:
                        continue
                    seen.add(identity)

                    # Resolve again after tablist rerender and require a unique approved tab.
                    _require_portal_page(page)
                    if label not in _allowed_tabs(page.url):
                        continue
                    locator = frame.get_by_role("tab", name=label, exact=True)
                    if (locator.count() != 1 or not locator.is_visible() or not locator.is_enabled()
                            or not locator.get_attribute("aria-controls")
                            or not _non_submitting_control(locator)):
                        locator = None
                    if locator is None:
                        self.store.observe(
                            run_id=run_id,
                            page_url=page.url,
                            kind="tab_unavailable",
                            label=label,
                        )
                        continue

                    _require_portal_page(page)
                    page_url = page.url
                    locator.click(timeout=2500)
                    page.wait_for_timeout(450)
                    _require_portal_page(page)
                    if page.url != page_url:
                        continue
                    if not locator.evaluate("""element => {
                        const panel = element.ownerDocument.getElementById(element.getAttribute('aria-controls'));
                        return element.getAttribute('aria-selected') === 'true' &&
                            !!panel && panel.getClientRects().length > 0 && !panel.hidden;
                    }"""):
                        self.store.observe(run_id=run_id, page_url=page_url, kind="tab_error", label=label)
                        continue
                    _settle_and_scroll(page, self.settings.settle_ms)
                    _safe_reveal(page)
                    self._trigger_explicit_downloads(page)
                    self._observe_page(page, run_id)

                    digest = hashlib.sha256(f"{page.url}|{label}".encode()).hexdigest()[:16]
                    parts = urlsplit(page.url)
                    query = f"{parts.query}&" if parts.query else ""
                    source_url = urlunsplit(
                        (parts.scheme, parts.netloc, parts.path, f"{query}__minsp_view=tab-{digest}", "")
                    )
                    html = frame.content().encode("utf-8")
                    _require_portal_page(page)
                    self.store.save_artifact(
                        kind="html",
                        data=html,
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
                except AuthRequired:
                    raise
                except Exception:
                    _require_portal_page(page)
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
        checkpoint_resumed = False
        page = browser.page
        assert page is not None
        self._run_id = run_id
        context = browser.context
        assert context is not None

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
        _require_portal_page(page)
        self._capture_page = page
        callback = self._capture_response
        guard = self._guard_request
        context.route("**/*", guard)
        context.on("response", callback)
        try:
            return self._crawl_authenticated(
                browser, page, start_url=start_url, run_id=run_id, resumed=resumed,
                interactive=interactive, expand_safe=expand_safe,
                checkpoint_probe=checkpoint_probe, checkpoint_resumed=checkpoint_resumed,
            )
        finally:
            self._capture_page = None
            context.remove_listener("response", callback)
            context.unroute("**/*", guard)

    def _crawl_authenticated(self, browser, page, *, start_url: str, run_id: str,
                             resumed: bool, interactive: bool, expand_safe: bool,
                             checkpoint_probe: bool, checkpoint_resumed: bool) -> CrawlResult:
        visited = 0
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
            if not eligible_url(url):
                self.store.mark_page_error(url, "Navigation outside the allowed portal origin or path")
                continue
            try:
                page.goto(url, wait_until="domcontentloaded")
                if not browser.is_authenticated_portal_url(page.url):
                    # If the portal expired the session, recover inside this
                    # same browser context. A fresh MitID login is requested
                    # only when the portal actually requires it.
                    self._capture_page = None
                    browser.ensure_authenticated(interactive=interactive)
                    page.goto(url, wait_until="domcontentloaded")
                    _require_portal_page(page)
                    self._capture_page = page

                _require_portal_page(page)

                _settle_and_scroll(page, self.settings.settle_ms)
                if expand_safe and _safe_reveal(page):
                    _settle_and_scroll(page, self.settings.settle_ms)

                self._trigger_explicit_downloads(page)

                self._observe_page(page, run_id)
                self._crawl_readonly_tabs(page, run_id, depth)

                _require_portal_page(page)
                html = page.content().encode("utf-8")
                _require_portal_page(page)
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
                self.store.mark_page_error(url, type(exc).__name__)

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
