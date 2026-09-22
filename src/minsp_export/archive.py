from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .storage import StateStore, redact_url


ARCHIVE_NAME = "minsp-export-complete-2026-09-22.zip"
URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _archive_unique_url(url: str, suffix: str) -> str:
    redacted = redact_url(url)
    parts = urlsplit(redacted)
    query = list(parse_qsl(parts.query, keep_blank_values=True))
    query.append(("__archive_ref", suffix))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _sanitize_error(value: str | None) -> str | None:
    if not value:
        return value
    return URL_RE.sub(lambda match: redact_url(match.group(0)), value)


def _sanitized_state_copy(store: StateStore, destination: Path) -> None:
    target = sqlite3.connect(destination)
    try:
        store.conn.backup(target)
        target.row_factory = sqlite3.Row
        rows = target.execute("SELECT rowid,url,last_error FROM pages ORDER BY rowid").fetchall()
        used: set[str] = set()
        for row in rows:
            sanitized = redact_url(row["url"])
            if sanitized in used:
                suffix = hashlib.sha256(row["url"].encode()).hexdigest()[:16]
                sanitized = _archive_unique_url(sanitized, suffix)
            used.add(sanitized)
            target.execute(
                "UPDATE pages SET url=?,last_error=? WHERE rowid=?",
                (sanitized, _sanitize_error(row["last_error"]), row["rowid"]),
            )
        target.commit()
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        target.close()


def _write_private_json(path: Path, value: object) -> Path:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    path.chmod(0o600)
    return path


def write_final_report(store: StateStore, db_path: Path, run_id: str) -> Path:
    state_counts = {
        row["status"]: row["n"]
        for row in store.conn.execute(
            "SELECT status,COUNT(*) AS n FROM pages WHERE run_id=? GROUP BY status",
            (run_id,),
        )
    }
    artifact_counts = {
        row["kind"]: row["n"]
        for row in store.conn.execute("SELECT kind,COUNT(*) AS n FROM artifacts GROUP BY kind")
    }
    artifact_bytes = {
        row["kind"]: row["n"]
        for row in store.conn.execute("SELECT kind,SUM(bytes) AS n FROM artifacts GROUP BY kind")
    }
    observation_counts = {
        row["kind"]: row["n"]
        for row in store.conn.execute(
            "SELECT kind,COUNT(*) AS n FROM observations WHERE run_id=? GROUP BY kind",
            (run_id,),
        )
    }
    observed_tabs = {
        row["label"]
        for row in store.conn.execute(
            "SELECT DISTINCT label FROM observations WHERE run_id=? AND kind='control:tab' AND label<>''",
            (run_id,),
        )
    }
    snapshotted_tabs = {
        row["label"]
        for row in store.conn.execute(
            "SELECT DISTINCT label FROM observations WHERE run_id=? AND kind='tab_snapshot' AND label<>''",
            (run_id,),
        )
    }
    errored_tabs = {
        row["label"]
        for row in store.conn.execute(
            "SELECT DISTINCT label FROM observations WHERE run_id=? AND kind='tab_error' AND label<>''",
            (run_id,),
        )
    }
    unavailable_tabs = {
        row["label"]
        for row in store.conn.execute(
            "SELECT DISTINCT label FROM observations WHERE run_id=? AND kind='tab_unavailable' AND label<>''",
            (run_id,),
        )
    }
    missing_tabs = sorted(
        (observed_tabs | errored_tabs) - snapshotted_tabs - unavailable_tabs
    )
    db = sqlite3.connect(db_path)
    try:
        domain_tables = (
            "records", "lab_results", "clinical_notes", "encounters", "imaging_reports",
            "diagnoses", "medications", "allergies", "appointments", "messages",
            "procedures", "questionnaires", "documents", "providers", "departments",
        )
        normalized_counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in domain_tables
        }
        foreign_key_errors = len(db.execute("PRAGMA foreign_key_check").fetchall())
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        fts_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='search_index'"
        ).fetchone()
        fts5 = bool(fts_sql and "FTS5" in (fts_sql[0] or "").upper())
    finally:
        db.close()

    generated_at = _utc_now()
    inventory = {
        "generated_at_utc": generated_at,
        "run_id": run_id,
        "raw": {
            kind: {
                "artifacts": artifact_counts.get(kind, 0),
                "bytes": artifact_bytes.get(kind, 0),
            }
            for kind in ("html", "json", "pdf", "attachments")
        },
        "pages": state_counts,
        "domain_tables": {
            table: {
                "records": count,
                "exposure": "exposed" if count else "not_exposed",
            }
            for table, count in normalized_counts.items()
        },
        "read_only_tabs": {
            "observed": sorted(observed_tabs),
            "snapshotted": sorted(snapshotted_tabs),
            "not_exposed_or_not_actionable": sorted(unavailable_tabs),
            "missing": missing_tabs,
        },
    }
    inventory_path = store.root / "manifests" / "inventory.json"
    _write_private_json(inventory_path, inventory)

    report = {
        "generated_at_utc": generated_at,
        "run_id": run_id,
        "raw_is_authoritative_and_immutable": True,
        "normalized_is_technical_projection": True,
        "pages": state_counts,
        "artifacts": artifact_counts,
        "observations": observation_counts,
        "normalized": normalized_counts,
        "checkpoint_resume": store.get_meta("checkpoint_probe_completed"),
        "read_only_tabs": inventory["read_only_tabs"],
        "inventory": str(inventory_path.relative_to(store.root)),
        "integrity_check": integrity,
        "foreign_key_errors": foreign_key_errors,
        "fts5": fts5,
    }
    path = store.root / "manifests" / "final-report.json"
    _write_private_json(path, report)
    if missing_tabs:
        store.prepare_tab_repair(run_id, missing_tabs)
        raise RuntimeError("read_only_tab_coverage_incomplete:" + ",".join(missing_tabs))
    return path


def build_complete_archive(store: StateStore) -> Path:
    root = store.root
    destination = root.parent / ARCHIVE_NAME
    required = (
        root / "raw",
        root / "manifests",
        root / "normalized" / "health.sqlite",
        root / "text" / "complete-medical-record.md",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("archive inputs missing: " + ", ".join(missing))

    # Keep the temporary ZIP on the destination filesystem so the final
    # atomic os.replace cannot fail with EXDEV on hosts where /tmp is separate.
    with tempfile.TemporaryDirectory(
        prefix=".minsp-export-archive-",
        dir=root.parent,
    ) as tmp_dir:
        tmp_root = Path(tmp_dir)
        sanitized_state = tmp_root / "state.sqlite"
        _sanitized_state_copy(store, sanitized_state)
        tmp_archive = tmp_root / ARCHIVE_NAME
        with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for base in (root / "raw", root / "manifests"):
                for path in sorted(base.rglob("*")):
                    if path.is_file() and not path.name.endswith(".tmp"):
                        zf.write(path, path.relative_to(root))
            zf.write(sanitized_state, "state.sqlite")
            zf.write(root / "normalized" / "health.sqlite", "normalized/health.sqlite")
            zf.write(
                root / "text" / "complete-medical-record.md",
                "text/complete-medical-record.md",
            )
        with zipfile.ZipFile(tmp_archive) as zf:
            names = set(zf.namelist())
            if not {
                "state.sqlite",
                "normalized/health.sqlite",
                "text/complete-medical-record.md",
                "manifests/files.jsonl",
                "manifests/coverage.json",
                "manifests/inventory.json",
                "manifests/final-report.json",
            }.issubset(names):
                raise RuntimeError("archive_required_entries_missing")
            forbidden = ("chrome-profile", "cookie", "storage-state", "session-state", "logs/")
            if any(any(token in name.lower() for token in forbidden) for name in names):
                raise RuntimeError("archive_forbidden_entry")
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"archive_corrupt_entry:{bad}")
        os.replace(tmp_archive, destination)
    destination.chmod(0o600)
    return destination
