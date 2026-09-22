from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_QUERY_PARTS = ("token", "auth", "session", "code", "ticket", "key", "secret")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        redacted = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if any(part in key.lower() for part in SENSITIVE_QUERY_PARTS):
                value = "<redacted>"
            redacted.append((key, value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), ""))
    except Exception:
        return url


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StateStore:
    def __init__(self, root: Path):
        self.root = secure_dir(root)
        for rel in ("raw/html", "raw/json", "raw/pdf", "raw/attachments", "manifests", "logs", "normalized", "text"):
            secure_dir(self.root / rel)
        self.db_path = self.root / "state.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages(
                url TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                depth INTEGER NOT NULL DEFAULT 0,
                tries INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                title TEXT,
                local_path TEXT,
                sha256 TEXT,
                updated_at TEXT NOT NULL,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pages_run_status
                ON pages(run_id, status, depth);
            CREATE TABLE IF NOT EXISTS artifacts(
                sha256 TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                local_path TEXT NOT NULL,
                content_type TEXT,
                bytes INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_sources(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
                source_url TEXT,
                captured_at TEXT NOT NULL,
                UNIQUE(sha256, source_url, captured_at)
            );
            CREATE TABLE IF NOT EXISTS runs(
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                pages_done INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS observations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                page_url TEXT NOT NULL,
                kind TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                target_url TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                UNIQUE(run_id,page_url,kind,label,target_url)
            );
            CREATE INDEX IF NOT EXISTS idx_observations_run_kind
                ON observations(run_id,kind);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self.conn.commit()

    def begin_run(self) -> tuple[str, bool]:
        active = self.get_meta("active_run_id")
        if active:
            return active, True
        run_id = uuid.uuid4().hex
        now = utc_now()
        self.conn.execute("INSERT INTO runs(run_id,started_at) VALUES(?,?)", (run_id, now))
        self.conn.execute(
            "UPDATE pages SET status='queued', tries=0, last_error=NULL, run_id=?, updated_at=?",
            (run_id, now),
        )
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('active_run_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (run_id,),
        )
        self.conn.commit()
        return run_id, False

    def finish_run(self, run_id: str) -> None:
        counts = self.conn.execute(
            "SELECT SUM(status='done') AS done, SUM(status='error') AS errors FROM pages WHERE run_id=?",
            (run_id,),
        ).fetchone()
        self.conn.execute(
            "UPDATE runs SET finished_at=?, pages_done=?, errors=? WHERE run_id=?",
            (utc_now(), int(counts["done"] or 0), int(counts["errors"] or 0), run_id),
        )
        self.conn.execute("DELETE FROM meta WHERE key='active_run_id' AND value=?", (run_id,))
        self.conn.commit()

    def prepare_tab_repair(self, run_id: str, labels: list[str]) -> int:
        if not labels:
            return 0
        placeholders = ",".join("?" for _ in labels)
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT page_url
            FROM observations
            WHERE run_id=? AND kind IN ('control:tab','tab_error')
              AND label IN ({placeholders})
            """,
            (run_id, *labels),
        ).fetchall()
        urls = [row["page_url"] for row in rows]
        if not urls:
            return 0
        now = utc_now()
        url_placeholders = ",".join("?" for _ in urls)
        updated = self.conn.execute(
            f"""
            UPDATE pages
            SET status='queued', tries=0, last_error=NULL, updated_at=?, run_id=?
            WHERE url IN ({url_placeholders})
            """,
            (now, run_id, *urls),
        ).rowcount
        if updated:
            self.conn.execute(
                "UPDATE runs SET finished_at=NULL, errors=0 WHERE run_id=?",
                (run_id,),
            )
            self.conn.execute(
                """
                INSERT INTO meta(key,value) VALUES('active_run_id',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (run_id,),
            )
        self.conn.commit()
        return int(updated)

    def enqueue(self, url: str, depth: int, run_id: str) -> bool:
        now = utc_now()
        row = self.conn.execute("SELECT run_id,status FROM pages WHERE url=?", (url,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO pages(url,status,depth,tries,updated_at,run_id) VALUES(?, 'queued', ?, 0, ?, ?)",
                (url, depth, now, run_id),
            )
            self.conn.commit()
            return True
        if row["run_id"] != run_id:
            self.conn.execute(
                "UPDATE pages SET status='queued', depth=MIN(depth,?), tries=0, last_error=NULL, updated_at=?, run_id=? WHERE url=?",
                (depth, now, run_id, url),
            )
            self.conn.commit()
            return True
        return row["status"] in ("queued", "error")

    def pending(self, run_id: str, max_retries: int, limit: int = 1) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM pages
            WHERE run_id=? AND status IN ('queued','error') AND tries < ?
            ORDER BY depth ASC, updated_at ASC
            LIMIT ?
            """,
            (run_id, max_retries, limit),
        ).fetchall()

    def pending_count(self, run_id: str, max_retries: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pages WHERE run_id=? AND status IN ('queued','error') AND tries < ?",
            (run_id, max_retries),
        ).fetchone()
        return int(row["n"])

    def exhausted_error_count(self, run_id: str, max_retries: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pages WHERE run_id=? AND status='error' AND tries >= ?",
            (run_id, max_retries),
        ).fetchone()
        return int(row["n"])

    def mark_page_done(self, url: str, *, title: str, local_path: str, digest: str) -> None:
        self.conn.execute(
            """
            UPDATE pages
            SET status='done', title=?, local_path=?, sha256=?, last_error=NULL, updated_at=?
            WHERE url=?
            """,
            (title, local_path, digest, utc_now(), url),
        )
        self.conn.commit()

    def mark_page_error(self, url: str, error: str) -> None:
        self.conn.execute(
            "UPDATE pages SET status='error', tries=tries+1, last_error=?, updated_at=? WHERE url=?",
            (error[:1000], utc_now(), url),
        )
        self.conn.commit()

    def observe(
        self,
        *,
        run_id: str,
        page_url: str,
        kind: str,
        label: str = "",
        target_url: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO observations
            (run_id,page_url,kind,label,target_url,observed_at)
            VALUES(?,?,?,?,?,?)
            """,
            (run_id, redact_url(page_url), kind[:80], label[:500], redact_url(target_url), utc_now()),
        )
        self.conn.commit()

    def artifact_for_source(self, source_url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT a.local_path,a.sha256,a.kind
            FROM artifact_sources s
            JOIN artifacts a ON a.sha256=s.sha256
            WHERE s.source_url=?
            ORDER BY s.id DESC LIMIT 1
            """,
            (redact_url(source_url),),
        ).fetchone()

    def save_artifact(
        self,
        *,
        kind: str,
        data: bytes,
        source_url: str | None,
        content_type: str | None = None,
        extension: str | None = None,
    ) -> tuple[Path, str]:
        digest = sha256_bytes(data)
        if kind not in {"html", "json", "pdf", "attachments"}:
            kind = "attachments"
        ext = extension or {"html": ".html", "json": ".json", "pdf": ".pdf"}.get(kind, ".bin")
        if ext and not ext.startswith("."):
            ext = "." + ext
        path = self.root / "raw" / kind / f"{digest}{ext}"
        if not path.exists():
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        rel = str(path.relative_to(self.root))
        now = utc_now()
        artifact_insert = self.conn.execute(
            """
            INSERT OR IGNORE INTO artifacts(sha256,kind,local_path,content_type,bytes,first_seen_at)
            VALUES(?,?,?,?,?,?)
            """,
            (digest, kind, rel, content_type, len(data), now),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO artifact_sources(sha256,source_url,captured_at) VALUES(?,?,?)",
            (digest, redact_url(source_url or ""), now),
        )
        self.conn.commit()
        if artifact_insert.rowcount:
            manifest = {
                "bytes": len(data),
                "captured_at": now,
                "content_type": content_type,
                "kind": kind,
                "path": rel,
                "sha256": digest,
                "source_url": redact_url(source_url or ""),
            }
            with (self.root / "manifests" / "files.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n")
        return path, digest

    def rebuild_manifest(self) -> Path:
        path = self.root / "manifests" / "files.jsonl"
        tmp = path.with_name(path.name + ".tmp")
        rows = self.conn.execute(
            """
            SELECT a.sha256,a.kind,a.local_path,a.content_type,a.bytes,a.first_seen_at,
                   COALESCE((SELECT s.source_url FROM artifact_sources s
                             WHERE s.sha256=a.sha256 ORDER BY s.id LIMIT 1),'') AS source_url
            FROM artifacts a ORDER BY a.sha256
            """
        ).fetchall()
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                item = {
                    "bytes": row["bytes"],
                    "captured_at": row["first_seen_at"],
                    "content_type": row["content_type"],
                    "kind": row["kind"],
                    "path": row["local_path"],
                    "sha256": row["sha256"],
                    "source_url": row["source_url"],
                }
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def verify_artifacts(self) -> dict[str, object]:
        errors: list[str] = []
        rows = self.conn.execute(
            "SELECT sha256,local_path,bytes FROM artifacts ORDER BY sha256"
        ).fetchall()
        for row in rows:
            path = self.root / row["local_path"]
            if not path.is_file():
                errors.append(f"missing:{row['local_path']}")
                continue
            data = path.read_bytes()
            if len(data) != row["bytes"]:
                errors.append(f"size:{row['local_path']}")
            if sha256_bytes(data) != row["sha256"]:
                errors.append(f"sha256:{row['local_path']}")
        self.rebuild_manifest()
        manifest = self.root / "manifests" / "files.jsonl"
        manifest_rows = sum(1 for line in manifest.read_text(encoding="utf-8").splitlines() if line)
        if manifest_rows != len(rows):
            errors.append(f"manifest_count:{manifest_rows}!={len(rows)}")
        return {"artifacts": len(rows), "manifest_rows": manifest_rows, "errors": errors}

    def write_coverage_report(self, run_id: str) -> Path:
        path = self.root / "manifests" / "coverage.json"
        page_rows = self.conn.execute(
            "SELECT url,status,title,last_error FROM pages WHERE run_id=? ORDER BY url",
            (run_id,),
        ).fetchall()
        observation_rows = self.conn.execute(
            """
            SELECT page_url,kind,label,target_url FROM observations
            WHERE run_id=? ORDER BY page_url,kind,label,target_url
            """,
            (run_id,),
        ).fetchall()
        pages = []
        for row in page_rows:
            item = dict(row)
            item["url"] = redact_url(item["url"])
            pages.append(item)
        payload = {
            "run_id": run_id,
            "pages": pages,
            "observations": [dict(row) for row in observation_rows],
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def summary(self) -> dict[str, object]:
        page_counts = {
            row["status"]: row["n"]
            for row in self.conn.execute("SELECT status,COUNT(*) AS n FROM pages GROUP BY status")
        }
        artifacts = {
            row["kind"]: row["n"]
            for row in self.conn.execute("SELECT kind,COUNT(*) AS n FROM artifacts GROUP BY kind")
        }
        return {
            "active_run_id": self.get_meta("active_run_id"),
            "pages": page_counts,
            "artifacts": artifacts,
            "state_db": str(self.db_path),
        }
