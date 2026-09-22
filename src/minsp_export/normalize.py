from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .storage import StateStore, redact_url

DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b")


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9æøå]+", "", value.lower())


def _scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return None


def _pick(mapping: dict[str, Any], names: Iterable[str]) -> str | None:
    normalized = {_norm_key(str(k)): v for k, v in mapping.items()}
    for name in names:
        value = normalized.get(_norm_key(name))
        s = _scalar(value)
        if s not in (None, ""):
            return s
    return None


def _all_scalar_text(node: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            p = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                out.extend(_all_scalar_text(value, p))
            else:
                s = _scalar(value)
                if s not in (None, ""):
                    out.append(f"{p}: {s}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out.extend(_all_scalar_text(value, f"{prefix}[{i}]"))
    else:
        s = _scalar(node)
        if s is not None:
            out.append(f"{prefix}: {s}" if prefix else s)
    return out


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if not self._skip:
            self.parts.append(text)


CATEGORY_HINTS = {
    "lab_result": ("lab", "laborator", "prøve", "proeve", "testresult", "resultat"),
    "clinical_note": ("note", "journal", "notat", "clinicalnote"),
    "encounter": ("encounter", "visit", "besøg", "besoeg", "admission", "discharge", "indlægg", "indlaegg"),
    "imaging_report": ("radiology", "imaging", "scan", "røntgen", "rontgen", "mri", "ct"),
    "diagnosis": ("diagnos", "icd"),
    "medication": ("medication", "medicine", "medicin", "drug"),
    "allergy": ("allerg", "cave"),
    "appointment": ("appointment", "aftale", "booking"),
    "message": ("message", "besked", "inbox"),
    "procedure": ("procedure", "operation", "intervention"),
    "questionnaire": ("questionnaire", "spørgeskema", "sporgeskema"),
}


def classify_dict(obj: dict[str, Any], source_url: str = "") -> str | None:
    keys = {_norm_key(str(k)) for k in obj}
    joined = " ".join(keys) + " " + source_url.lower()

    def has(*terms: str) -> bool:
        return any(_norm_key(term) in joined for term in terms)

    if has("testname", "componentname", "analyte") and has("value", "result", "unit", "referencerange"):
        return "lab_result"
    if has("allergy", "allergen") and has("reaction", "status", "severity"):
        return "allergy"
    if has("medication", "medicine", "drugname", "medicin") and has("dose", "dosage", "status", "strength"):
        return "medication"
    if has("appointment", "aftale") and has("date", "start", "time", "location", "department"):
        return "appointment"
    if has("subject") and has("sender", "recipient", "message", "body"):
        return "message"
    if has("diagnosis", "diagnose", "icd"):
        return "diagnosis"
    if has("radiology", "imaging", "modality") and has("report", "result", "body", "text"):
        return "imaging_report"
    if has("questionnaire", "spørgeskema", "sporgeskema") or (has("question") and has("answer")):
        return "questionnaire"
    if has("encounter", "visit", "admission", "discharge", "besøg", "besoeg") and has("date", "department", "provider", "status"):
        return "encounter"
    if has("procedure", "operation") and has("date", "status", "name"):
        return "procedure"
    if has("note", "journal", "notat") and has("author", "body", "text", "date"):
        return "clinical_note"

    direct_scalar_count = sum(
        _scalar(value) not in (None, "") for value in obj.values()
    )
    for category, hints in CATEGORY_HINTS.items():
        if any(h in source_url.lower() for h in hints) and direct_scalar_count >= 3:
            return category
    return None


def _walk_dicts(node: Any, pointer: str = "$"):
    if isinstance(node, dict):
        yield pointer, node
        for key, value in node.items():
            yield from _walk_dicts(value, f"{pointer}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_dicts(value, f"{pointer}[{i}]")


def _record_id(source_sha: str, pointer: str, category: str) -> str:
    return hashlib.sha256(f"{source_sha}|{pointer}|{category}".encode()).hexdigest()


def _extract_date(obj: dict[str, Any], text: str) -> str | None:
    direct = _pick(
        obj,
        ("date", "datetime", "occurredAt", "createdAt", "startDate", "startTime",
         "resultDate", "admissionDate", "dischargeDate", "time"),
    )
    if direct:
        return direct
    match = DATE_RE.search(text)
    return match.group(0) if match else None


def _extract_pdf_text(path: Path) -> str:
    if shutil.which("pdftotext") is None:
        return ""
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        return proc.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE source_artifacts(
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    local_path TEXT NOT NULL,
    content_type TEXT,
    bytes INTEGER NOT NULL,
    source_url TEXT
);
CREATE TABLE records(
    record_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    occurred_at TEXT,
    title TEXT,
    body TEXT NOT NULL,
    source_sha256 TEXT NOT NULL REFERENCES source_artifacts(sha256),
    source_path TEXT NOT NULL,
    source_url TEXT
);
CREATE INDEX idx_records_category_date ON records(category, occurred_at);
CREATE TABLE lab_results(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,test_name TEXT,value_text TEXT,value_num REAL,unit TEXT,reference_range TEXT,abnormal_flag TEXT);
CREATE TABLE clinical_notes(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,note_type TEXT,author TEXT);
CREATE TABLE encounters(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,encounter_type TEXT,department TEXT,provider TEXT);
CREATE TABLE imaging_reports(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,modality TEXT,report_text TEXT);
CREATE TABLE diagnoses(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,code TEXT,status TEXT);
CREATE TABLE medications(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,medication_name TEXT,dose TEXT,status TEXT);
CREATE TABLE allergies(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,substance TEXT,reaction TEXT,status TEXT);
CREATE TABLE appointments(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,start_at TEXT,status TEXT,location TEXT,provider TEXT);
CREATE TABLE messages(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,sender TEXT,recipient TEXT,subject TEXT,direction TEXT);
CREATE TABLE procedures(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,procedure_name TEXT,status TEXT);
CREATE TABLE questionnaires(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,questionnaire_name TEXT,status TEXT);
CREATE TABLE documents(record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,document_type TEXT,file_path TEXT,mime_type TEXT);
CREATE TABLE providers(provider_id TEXT PRIMARY KEY,name TEXT NOT NULL,specialty TEXT);
CREATE TABLE departments(department_id TEXT PRIMARY KEY,name TEXT NOT NULL);
"""


class Normalizer:
    def __init__(self, store: StateStore):
        self.store = store
        self.db_path = store.root / "normalized" / "health.sqlite"

    def build(self) -> Path:
        if self.db_path.exists():
            self.db_path.unlink()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE search_index USING fts5(record_id UNINDEXED, category, title, body, tokenize='unicode61 remove_diacritics 2')"
            )
        except sqlite3.OperationalError:
            conn.execute("CREATE TABLE search_index(record_id TEXT, category TEXT, title TEXT, body TEXT)")
            conn.execute("CREATE INDEX idx_search_fallback ON search_index(category,title)")

        rows = self.store.conn.execute(
            """
            SELECT a.*, (
                SELECT source_url FROM artifact_sources s
                WHERE s.sha256=a.sha256 ORDER BY s.id DESC LIMIT 1
            ) AS source_url
            FROM artifacts a
            ORDER BY a.first_seen_at, a.sha256
            """
        ).fetchall()

        for artifact in rows:
            conn.execute(
                "INSERT INTO source_artifacts VALUES(?,?,?,?,?,?)",
                (artifact["sha256"], artifact["kind"], artifact["local_path"],
                 artifact["content_type"], artifact["bytes"], artifact["source_url"]),
            )
            path = self.store.root / artifact["local_path"]
            if artifact["kind"] == "html":
                self._normalize_html(conn, artifact, path)
            elif artifact["kind"] == "json":
                self._normalize_json(conn, artifact, path)
            elif artifact["kind"] in ("pdf", "attachments"):
                self._normalize_document(conn, artifact, path)

        conn.commit()
        conn.execute("PRAGMA optimize")
        conn.close()
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        return self.db_path

    def _insert_record(self, conn, artifact, *, record_id, category, occurred_at, title, body, obj=None) -> None:
        if not body.strip() and not title:
            return
        source_url = redact_url(artifact["source_url"] or "")
        conn.execute(
            """
            INSERT OR IGNORE INTO records
            (record_id,category,occurred_at,title,body,source_sha256,source_path,source_url)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (record_id, category, occurred_at, title or category, body,
             artifact["sha256"], artifact["local_path"], source_url),
        )
        conn.execute(
            "INSERT INTO search_index(record_id,category,title,body) VALUES(?,?,?,?)",
            (record_id, category, title or category, body),
        )
        if obj is not None:
            self._insert_specific(conn, record_id, category, obj, body)

    def _normalize_html(self, conn, artifact, path: Path) -> None:
        parser = VisibleTextParser()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        title = " ".join(parser.title_parts).strip() or Path(artifact["local_path"]).name
        body = "\n".join(parser.parts)
        category = "page"
        low = (artifact["source_url"] or "").lower()
        for name, hints in CATEGORY_HINTS.items():
            if any(h in low for h in hints):
                category = name
                break
        rid = _record_id(artifact["sha256"], "$page", category)
        occurred = DATE_RE.search(body)
        self._insert_record(
            conn, artifact, record_id=rid, category=category,
            occurred_at=occurred.group(0) if occurred else None,
            title=title, body=body,
        )

    def _normalize_json(self, conn, artifact, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return
        source_url = artifact["source_url"] or ""
        for pointer, obj in _walk_dicts(data):
            category = classify_dict(obj, source_url)
            if not category:
                continue
            body = "\n".join(_all_scalar_text(obj))
            title = _pick(
                obj,
                ("title", "name", "testName", "componentName", "subject",
                 "medicationName", "drugName", "diagnosis", "procedureName",
                 "questionnaireName", "noteType"),
            ) or category
            rid = _record_id(artifact["sha256"], pointer, category)
            self._insert_record(
                conn, artifact, record_id=rid, category=category,
                occurred_at=_extract_date(obj, body), title=title, body=body, obj=obj,
            )

        whole = "\n".join(_all_scalar_text(data))
        if whole:
            rid = _record_id(artifact["sha256"], "$raw", "json_payload")
            self._insert_record(
                conn, artifact, record_id=rid, category="json_payload",
                occurred_at=None, title="Captured MyChart JSON payload", body=whole,
            )

    def _normalize_document(self, conn, artifact, path: Path) -> None:
        body = _extract_pdf_text(path) if artifact["kind"] == "pdf" or path.suffix.lower() == ".pdf" else ""
        rid = _record_id(artifact["sha256"], "$document", "document")
        self._insert_record(
            conn, artifact, record_id=rid, category="document",
            occurred_at=None, title=path.name, body=body or f"Binary document: {path.name}",
        )
        conn.execute(
            "INSERT OR IGNORE INTO documents VALUES(?,?,?,?)",
            (rid, artifact["kind"], artifact["local_path"], artifact["content_type"]),
        )

    def _insert_specific(self, conn, rid: str, category: str, obj: dict[str, Any], body: str) -> None:
        p = lambda *keys: _pick(obj, keys)
        if category == "lab_result":
            value = p("value", "result", "resultValue")
            try:
                num = float(str(value).replace(",", ".")) if value is not None else None
            except ValueError:
                num = None
            conn.execute(
                "INSERT OR IGNORE INTO lab_results VALUES(?,?,?,?,?,?,?)",
                (rid, p("testName","componentName","name"), value, num, p("unit"),
                 p("referenceRange","normalRange"), p("abnormalFlag","flag","status")),
            )
        elif category == "clinical_note":
            conn.execute("INSERT OR IGNORE INTO clinical_notes VALUES(?,?,?)", (rid, p("noteType","type"), p("author","provider")))
        elif category == "encounter":
            dep, provider = p("department","departmentName"), p("provider","providerName","clinician")
            conn.execute("INSERT OR IGNORE INTO encounters VALUES(?,?,?,?)", (rid, p("encounterType","visitType","type"), dep, provider))
            self._upsert_entities(conn, dep, provider)
        elif category == "imaging_report":
            conn.execute("INSERT OR IGNORE INTO imaging_reports VALUES(?,?,?)", (rid, p("modality","type"), p("report","reportText","body","text") or body))
        elif category == "diagnosis":
            conn.execute("INSERT OR IGNORE INTO diagnoses VALUES(?,?,?)", (rid, p("code","icd","icdCode"), p("status")))
        elif category == "medication":
            conn.execute("INSERT OR IGNORE INTO medications VALUES(?,?,?,?)", (rid, p("medicationName","drugName","medicine","name"), p("dose","dosage","strength"), p("status")))
        elif category == "allergy":
            conn.execute("INSERT OR IGNORE INTO allergies VALUES(?,?,?,?)", (rid, p("allergen","allergy","substance","name"), p("reaction"), p("status","severity")))
        elif category == "appointment":
            dep, provider = p("department","location"), p("provider","providerName")
            conn.execute("INSERT OR IGNORE INTO appointments VALUES(?,?,?,?,?)", (rid, p("startAt","startTime","date","datetime"), p("status"), dep, provider))
            self._upsert_entities(conn, dep, provider)
        elif category == "message":
            conn.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)", (rid, p("sender","from"), p("recipient","to"), p("subject","title"), p("direction")))
        elif category == "procedure":
            conn.execute("INSERT OR IGNORE INTO procedures VALUES(?,?,?)", (rid, p("procedureName","name","procedure"), p("status")))
        elif category == "questionnaire":
            conn.execute("INSERT OR IGNORE INTO questionnaires VALUES(?,?,?)", (rid, p("questionnaireName","name","title"), p("status")))

    @staticmethod
    def _upsert_entities(conn: sqlite3.Connection, department: str | None, provider: str | None) -> None:
        if department:
            did = hashlib.sha256(department.encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO departments VALUES(?,?)", (did, department))
        if provider:
            pid = hashlib.sha256(provider.encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO providers(provider_id,name,specialty) VALUES(?,?,NULL)", (pid, provider))
