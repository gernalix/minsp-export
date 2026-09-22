from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from minsp_export import cli
from minsp_export.browser import BrowserSession
from minsp_export.crawler import Crawler, CrawlResult, canonical_url, eligible_url, response_kind
from minsp_export.normalize import Normalizer, classify_dict
from minsp_export.render import search
from minsp_export.storage import StateStore, redact_url


class BrowserAuthUrlTests(unittest.TestCase):
    def test_authenticated_portal_page_is_recognized(self):
        self.assertTrue(
            BrowserSession.is_authenticated_portal_url(
                "https://minsundhedsplatform.dk/mychartppr1/app/testresults"
            )
        )

    def test_login_page_is_not_authenticated(self):
        self.assertFalse(
            BrowserSession.is_authenticated_portal_url(
                "https://minsundhedsplatform.dk/mychartppr1/Authentication/Login?"
            )
        )

    def test_external_mitid_page_is_not_authenticated(self):
        self.assertFalse(
            BrowserSession.is_authenticated_portal_url(
                "https://www.mitid.dk/mitid-core-client/"
            )
        )


class UrlPolicyTests(unittest.TestCase):
    def test_same_origin_read_only_route_allowed(self):
        self.assertTrue(eligible_url("https://minsundhedsplatform.dk/mychartppr1/app/testresults"))

    def test_external_and_login_routes_rejected(self):
        self.assertFalse(eligible_url("https://example.com/mychartppr1/app/testresults"))
        self.assertFalse(eligible_url("https://minsundhedsplatform.dk/mychartppr1/Authentication/Login?x=1"))

    def test_destructive_route_rejected(self):
        self.assertFalse(eligible_url("https://minsundhedsplatform.dk/mychartppr1/app/cancelappointment/123"))

    def test_fragment_removed(self):
        self.assertEqual(
            canonical_url("HTTPS://minsundhedsplatform.dk/mychartppr1/a?x=1#tab"),
            "https://minsundhedsplatform.dk/mychartppr1/a?x=1",
        )

    def test_sensitive_query_redaction(self):
        out = redact_url("https://minsundhedsplatform.dk/mychartppr1/a?token=abc&x=1")
        self.assertNotIn("abc", out)
        self.assertIn("x=1", out)

    def test_response_classification(self):
        self.assertEqual(
            response_kind("https://x/a", "application/json; charset=utf-8", ""),
            ("json", ".json"),
        )
        self.assertEqual(
            response_kind("https://x/a.pdf", "application/octet-stream", ""),
            ("pdf", ".pdf"),
        )
        self.assertEqual(
            response_kind("https://x/fragment", "text/html; charset=utf-8", ""),
            ("html", ".html"),
        )

    def test_external_response_is_never_captured(self):
        store = MagicMock()
        crawler = Crawler(MagicMock(), store)
        crawler._run_id = "run"
        response = MagicMock()
        response.status = 200
        response.url = "https://www.mitid.dk/mitid-core-client/state.json"
        crawler._capture_response(response)
        store.observe.assert_not_called()
        store.save_artifact.assert_not_called()

    def test_portal_initiated_cdn_download_is_captured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "source.pdf"
            downloaded.write_bytes(b"%PDF-1.4\nexample")
            store = StateStore(root / "export")
            crawler = Crawler(MagicMock(), store)
            crawler._run_id = "run"
            download = MagicMock()
            download.url = "https://cdn.example.invalid/document/123"
            download.page.url = "https://minsundhedsplatform.dk/mychartppr1/app/documents"
            download.path.return_value = downloaded
            download.suggested_filename = "record.pdf"
            crawler._capture_download(download)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM artifacts WHERE kind='pdf'").fetchone()[0],
                1,
            )
            store.close()


class StateTests(unittest.TestCase):
    def test_resume_and_fresh_incremental_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            run1, resumed = store.begin_run()
            self.assertFalse(resumed)
            store.enqueue("https://minsundhedsplatform.dk/mychartppr1/app/a", 0, run1)
            store.mark_page_done(
                "https://minsundhedsplatform.dk/mychartppr1/app/a",
                title="A",
                local_path="raw/html/a.html",
                digest="a",
            )
            store.close()

            store = StateStore(Path(tmp))
            same, resumed = store.begin_run()
            self.assertEqual(same, run1)
            self.assertTrue(resumed)
            store.finish_run(run1)
            run2, resumed = store.begin_run()
            self.assertNotEqual(run2, run1)
            self.assertFalse(resumed)
            self.assertEqual(store.pending_count(run2, 3), 1)
            store.close()

    def test_manifest_deduplicates_and_verifies_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            payload = b"same clinical payload"
            first_path, first_sha = store.save_artifact(
                kind="json",
                data=payload,
                source_url="https://minsundhedsplatform.dk/mychartppr1/api/results",
                content_type="application/json",
                extension=".json",
            )
            second_path, second_sha = store.save_artifact(
                kind="json",
                data=payload,
                source_url="https://minsundhedsplatform.dk/mychartppr1/api/results?page=2",
                content_type="application/json",
                extension=".json",
            )
            self.assertEqual(first_path, second_path)
            self.assertEqual(first_sha, second_sha)
            verification = store.verify_artifacts()
            self.assertEqual(verification["artifacts"], 1)
            self.assertEqual(verification["manifest_rows"], 1)
            self.assertEqual(verification["errors"], [])
            store.close()

    def test_coverage_observations_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            run_id, _ = store.begin_run()
            values = {
                "run_id": run_id,
                "page_url": "https://minsundhedsplatform.dk/mychartppr1/app/home",
                "kind": "control:button",
                "label": "Vis mere",
                "target_url": "",
            }
            store.observe(**values)
            store.observe(**values)
            report = json.loads(store.write_coverage_report(run_id).read_text(encoding="utf-8"))
            self.assertEqual(len(report["observations"]), 1)
            store.close()


class NormalizationTests(unittest.TestCase):
    def test_lab_classification(self):
        self.assertEqual(
            classify_dict({"testName": "Ferritin", "value": "42", "unit": "µg/L"}),
            "lab_result",
        )

    def test_end_to_end_json_normalization_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StateStore(root)
            payload = json.dumps({
                "results": [{
                    "testName": "Ferritin",
                    "value": "42",
                    "unit": "µg/L",
                    "referenceRange": "15-150",
                    "date": "2026-09-01",
                }]
            }).encode()
            store.save_artifact(
                kind="json",
                data=payload,
                source_url="https://minsundhedsplatform.dk/mychartppr1/app/testresults",
                content_type="application/json",
                extension=".json",
            )
            db = Normalizer(store).build()
            hits = search(db, "Ferritin")
            self.assertTrue(hits)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            conn.close()
            store.close()


class ExportWorkflowTests(unittest.TestCase):
    @patch("minsp_export.cli.render_markdown")
    @patch("minsp_export.cli.Normalizer")
    @patch("minsp_export.cli.Crawler")
    @patch("minsp_export.cli.BrowserSession")
    @patch("minsp_export.cli.StateStore")
    def test_export_reopens_checkpoint_without_reopening_browser(
        self, state_store, browser_session, crawler_cls, normalizer, render_markdown
    ):
        first_store = MagicMock()
        second_store = MagicMock()
        state_store.side_effect = [first_store, second_store]
        browser = MagicMock()
        browser_session.return_value.__enter__.return_value = browser
        first_crawler = MagicMock()
        second_crawler = MagicMock()
        crawler_cls.side_effect = [first_crawler, second_crawler]
        first_crawler.run.return_value = CrawlResult(
            "run", 1, 2, 0, False, checkpoint_interrupted=True
        )
        second_crawler.run.return_value = CrawlResult(
            "run", 2, 0, 0, True, checkpoint_resumed=True
        )
        second_store.write_coverage_report.return_value = Path("coverage.json")
        second_store.verify_artifacts.return_value = {
            "artifacts": 3,
            "manifest_rows": 3,
            "errors": [],
        }
        normalizer.return_value.build.return_value = Path("health.sqlite")
        render_markdown.return_value = Path("complete-medical-record.md")
        args = SimpleNamespace(
            output="/tmp/export",
            profile="/tmp/profile",
            max_pages=100,
            non_interactive=False,
            no_expand=False,
        )

        self.assertEqual(cli.cmd_export(args), 0)
        self.assertEqual(browser_session.call_count, 1)
        first_store.close.assert_called_once()
        self.assertIs(first_crawler.run.call_args.kwargs["browser"], browser)
        self.assertIs(second_crawler.run.call_args.kwargs["browser"], browser)


if __name__ == "__main__":
    unittest.main()
