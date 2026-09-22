from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .browser import AuthRequired, BrowserSession
from .config import DEFAULT_OUTPUT_DIR, DEFAULT_PROFILE_DIR, Settings
from .crawler import Crawler
from .normalize import Normalizer
from .render import render_markdown, search
from .storage import StateStore, secure_dir

AUTH_REQUIRED_EXIT = 10


def _settings(args) -> Settings:
    return Settings(
        output_dir=Path(args.output).expanduser(),
        profile_dir=Path(args.profile).expanduser(),
        max_pages=getattr(args, "max_pages", 10_000),
    )


def _print_crawl_result(result) -> int:
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 2 if result.exhausted_errors else 0


def cmd_login(args) -> int:
    settings = _settings(args)
    secure_dir(settings.profile_dir)
    with BrowserSession(settings, headless=False) as browser:
        url = browser.ensure_authenticated(interactive=True)
        print(f"AUTHENTICATED: {url}")
        print(
            "Diagnostic login completed. For a real export use minsp-export export, "
            "which keeps this same browser session alive through the crawl."
        )
    return 0


def cmd_crawl(args) -> int:
    settings = _settings(args)
    store = StateStore(settings.output_dir)
    try:
        result = Crawler(settings, store).run(
            interactive=not args.non_interactive,
            expand_safe=not args.no_expand,
        )
        return _print_crawl_result(result)
    except AuthRequired as exc:
        print(str(exc), file=sys.stderr)
        return AUTH_REQUIRED_EXIT
    finally:
        store.close()


def cmd_normalize(args) -> int:
    settings = _settings(args)
    store = StateStore(settings.output_dir)
    try:
        path = Normalizer(store).build()
        print(path)
        return 0
    finally:
        store.close()


def cmd_render(args) -> int:
    settings = _settings(args)
    path = render_markdown(settings.normalized_db, settings.markdown_path)
    print(path)
    return 0


def cmd_export(args) -> int:
    settings = _settings(args)
    store = StateStore(settings.output_dir)
    try:
        # One process, one Playwright context, one Chrome window from MitID
        # completion through the entire crawl. This avoids relying on Epic
        # session cookies surviving a browser close/reopen.
        with BrowserSession(settings, headless=args.non_interactive) as browser:
            result = Crawler(settings, store).run(
                interactive=not args.non_interactive,
                expand_safe=not args.no_expand,
                browser=browser,
            )
            crawl_rc = _print_crawl_result(result)

        db_path = Normalizer(store).build()
        print(db_path)
        markdown_path = render_markdown(db_path, settings.markdown_path)
        print(markdown_path)
        return crawl_rc
    except AuthRequired as exc:
        print(str(exc), file=sys.stderr)
        return AUTH_REQUIRED_EXIT
    finally:
        store.close()


def cmd_status(args) -> int:
    settings = _settings(args)
    store = StateStore(settings.output_dir)
    try:
        summary = store.summary()
        summary["normalized_db_exists"] = settings.normalized_db.exists()
        summary["markdown_exists"] = settings.markdown_path.exists()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


def cmd_search(args) -> int:
    settings = _settings(args)
    if not settings.normalized_db.exists():
        print("Normalized database missing; run minsp-export normalize first.", file=sys.stderr)
        return 2
    results = search(settings.normalized_db, args.query, args.limit)
    for row in results:
        date = row.get("occurred_at") or "-"
        print(f"{date} | {row['category']} | {row['title']}")
        print(f"  {row.get('snippet', '')}")
        print(f"  id={row['record_id']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minsp-export")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "login",
        help="Diagnostic manual MitID login; use export for the normal one-login workflow",
    )

    for name in ("crawl", "export"):
        p = sub.add_parser(name, help=f"{name.capitalize()} portal data")
        p.add_argument(
            "--non-interactive",
            action="store_true",
            help="Never wait for MitID; exit 10 if login is required",
        )
        p.add_argument(
            "--no-expand",
            action="store_true",
            help="Do not click exact read-only show-more controls",
        )
        p.add_argument("--max-pages", type=int, default=10_000)

    sub.add_parser("normalize", help="Build normalized health.sqlite from captured raw artifacts")
    sub.add_parser("render", help="Build complete-medical-record.md from normalized data")
    sub.add_parser("status", help="Show crawl and artifact counts")

    p_search = sub.add_parser("search", help="Search the normalized FTS5 archive")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=25)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = {
        "login": cmd_login,
        "crawl": cmd_crawl,
        "normalize": cmd_normalize,
        "render": cmd_render,
        "export": cmd_export,
        "status": cmd_status,
        "search": cmd_search,
    }[args.command]
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
