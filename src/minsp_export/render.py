from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path


def _clean(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\x00", "").strip()


def render_markdown(db_path: Path, output_path: Path) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"Normalized database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT record_id, category, occurred_at, title, body, source_path, source_url
        FROM records
        ORDER BY category COLLATE NOCASE, COALESCE(occurred_at,'') DESC, title COLLATE NOCASE
        """
    ).fetchall()

    grouped = defaultdict(list)
    for row in rows:
        grouped[row["category"]].append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        fh.write("# Complete Min Sundhedsplatform export\n\n")
        fh.write(f"Records: **{len(rows)}**\n\n")
        fh.write("> Generated from locally captured portal data. The original raw artifacts remain authoritative.\n\n")
        for category in sorted(grouped):
            fh.write(f"## {category}\n\n")
            for row in grouped[category]:
                title = _clean(row["title"]) or "(untitled)"
                fh.write(f"### {title}\n\n")
                if row["occurred_at"]:
                    fh.write(f"- Date/time: {row['occurred_at']}\n")
                fh.write(f"- Source: {row['source_path']}\n")
                if row["source_url"]:
                    fh.write(f"- Portal URL: {row['source_url']}\n")
                fh.write(f"- Record ID: {row['record_id']}\n\n")
                body = _clean(row["body"])
                if body:
                    fh.write(body + "\n\n")
    conn.close()
    try:
        output_path.chmod(0o600)
    except OSError:
        pass
    return output_path


def search(db_path: Path, query: str, limit: int = 25) -> list[dict[str, str]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='search_index'").fetchone()
    is_fts = bool(sql and "VIRTUAL TABLE" in (sql["sql"] or "").upper())
    if is_fts:
        rows = conn.execute(
            """
            SELECT r.record_id,r.category,r.occurred_at,r.title,
                   snippet(search_index,3,'[',']',' … ',18) AS snippet
            FROM search_index JOIN records r USING(record_id)
            WHERE search_index MATCH ?
            ORDER BY bm25(search_index)
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    else:
        like = f"%{query}%"
        rows = conn.execute(
            """
            SELECT r.record_id,r.category,r.occurred_at,r.title,
                   substr(r.body,1,300) AS snippet
            FROM records r
            WHERE r.title LIKE ? OR r.body LIKE ?
            LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]
