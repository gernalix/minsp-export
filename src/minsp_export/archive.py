from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .storage import StateStore, contained_file, public_error, redact_url, sanitize_observation


ARCHIVE_NAME = "minsp-export-complete-2026-09-22.zip"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _archive_unique_url(url: str, suffix: str) -> str:
    redacted = redact_url(url)
    parts = urlsplit(redacted)
    query = list(parse_qsl(parts.query, keep_blank_values=True))
    query.append(("__archive_ref", suffix))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _sanitized_state_copy(store: StateStore, destination: Path) -> None:
    target = sqlite3.connect(destination.with_name("state-working.sqlite"))
    try:
        store.conn.backup(target)
        target.row_factory = sqlite3.Row
        rows = target.execute("SELECT rowid,* FROM pages ORDER BY rowid").fetchall()
        reserved = {redact_url(row["url"]) for row in rows}
        used: set[str] = set()
        target.execute("DELETE FROM pages")
        for row in rows:
            sanitized = redact_url(row["url"])
            if sanitized in used:
                suffix = hashlib.sha256(row["url"].encode()).hexdigest()[:16]
                candidate = _archive_unique_url(sanitized, suffix)
                while candidate in used or candidate in reserved:
                    suffix += "-"
                    candidate = _archive_unique_url(sanitized, suffix)
                sanitized = candidate
            used.add(sanitized)
            target.execute(
                """INSERT INTO pages(rowid,url,status,depth,tries,last_error,title,
                                     local_path,sha256,updated_at,run_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["rowid"], sanitized, row["status"], row["depth"], row["tries"],
                 public_error(row["last_error"]), row["title"], row["local_path"],
                 row["sha256"], row["updated_at"], row["run_id"]),
            )
        sources = [dict(row) for row in target.execute("SELECT * FROM artifact_sources ORDER BY id")]
        target.execute("DELETE FROM artifact_sources")
        used_sources = set()
        for row in sources:
            url = redact_url(row["source_url"] or "")
            while (row["sha256"], url, row["captured_at"]) in used_sources:
                url = _archive_unique_url(url, f"source-{row['id']}")
            used_sources.add((row["sha256"], url, row["captured_at"]))
            target.execute("INSERT INTO artifact_sources(id,sha256,source_url,captured_at) VALUES(?,?,?,?)",
                           (row["id"], row["sha256"], url, row["captured_at"]))

        observations = [sanitize_observation(dict(row)) for row in
                        target.execute("SELECT * FROM observations ORDER BY id")]
        target.execute("DELETE FROM observations")
        used_observations = set()
        for row in observations:
            key = (row["run_id"], row["page_url"], row["kind"], row["label"])
            url = row["target_url"]
            while (*key, url) in used_observations:
                url = _archive_unique_url(url, f"observation-{row['id']}")
            used_observations.add((*key, url))
            target.execute(
                "INSERT INTO observations(id,run_id,page_url,kind,label,target_url,observed_at) VALUES(?,?,?,?,?,?,?)",
                (row["id"], *key, url, row["observed_at"]),
            )
        target.commit()
        target.execute("VACUUM INTO ?", (str(destination),))
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
    tabs_by_kind = {}
    for kind in ("control:tab", "tab_snapshot", "tab_error", "tab_unavailable"):
        tabs_by_kind[kind] = {
            (row["page_url"], row["label"])
            for row in store.conn.execute(
                "SELECT page_url,label FROM observations WHERE run_id=? AND kind=? AND label<>''",
                (run_id, kind),
            )
        }
    missing_pairs = (
        (tabs_by_kind["control:tab"] | tabs_by_kind["tab_error"])
        - tabs_by_kind["tab_snapshot"] - tabs_by_kind["tab_unavailable"]
    )
    observed_tabs = {label for _, label in tabs_by_kind["control:tab"]}
    snapshotted_tabs = {label for _, label in tabs_by_kind["tab_snapshot"]}
    unavailable_tabs = {label for _, label in tabs_by_kind["tab_unavailable"]}
    missing_tabs = sorted({label for _, label in missing_pairs})
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
        "manifests/files.jsonl",
        "manifests/coverage.json",
        "manifests/inventory.json",
        "manifests/final-report.json",
        "normalized/health.sqlite",
        "text/complete-medical-record.md",
    )
    inputs = [(rel, contained_file(root, rel, rel.split("/", 1)[0])) for rel in required]
    artifacts = [(row, contained_file(root, row["local_path"], "raw")) for row in
                 store.conn.execute("SELECT sha256,local_path,bytes FROM artifacts ORDER BY local_path")]

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
            for rel, path in inputs:
                if rel == "manifests/coverage.json":
                    coverage = json.loads(path.read_text(encoding="utf-8"))
                    for page in coverage["pages"]:
                        page["url"] = redact_url(page["url"])
                        page["last_error"] = public_error(page["last_error"])
                    coverage["observations"] = [sanitize_observation(item) for item in coverage["observations"]]
                    zf.writestr(rel, json.dumps(coverage, ensure_ascii=False) + "\n")
                elif rel == "manifests/files.jsonl":
                    with path.open(encoding="utf-8") as source, zf.open(rel, "w") as member:
                        for line in source:
                            item = json.loads(line)
                            item["source_url"] = redact_url(item["source_url"] or "")
                            member.write((json.dumps(item, ensure_ascii=False) + "\n").encode())
                else:
                    zf.write(path, rel)
            for row, path in artifacts:
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as source, zf.open(row["local_path"], "w", force_zip64=True) as member:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        member.write(chunk)
                if size != row["bytes"] or digest.hexdigest() != row["sha256"]:
                    raise ValueError("archive artifact differs from its registered bytes")
            zf.write(sanitized_state, "state.sqlite")
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
        tmp_archive.chmod(0o600)
        os.replace(tmp_archive, destination)
    return destination
