from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from minsp_export.crawler import canonical_url, eligible_url, response_kind
from minsp_export.normalize import Normalizer, classify_dict
from minsp_export.render import search
from minsp_export.storage import StateStore, redact_url


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


if __name__ == "__main__":
    unittest.main()
