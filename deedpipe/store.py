"""Where posted records land: SQLite by default, a CSV/ASCII load file beside it.

The posting discipline, which is the part worth copying into a production ODBC loader:

**Idempotent on instrument number.** The same scan arriving twice — a re-send, a retry
after a network blip, an operator dragging a folder in again — must not create a second
row. `INSERT ... ON CONFLICT DO UPDATE` keyed on the recorder's own identifier makes a
re-run safe, and a loader that is not safe to re-run is a loader nobody dares re-run.

**Exceptions are stored, not just filed away.** A quarantined PDF in a folder is invisible
to reporting. The same row goes into the database with its disposition and reason so
"what is sitting in the queue and why" is a query rather than a directory listing.

**No partial posts.** A record either lands whole or goes to the queue. Half a deed in a
title plant is worse than none, because a search finds it and believes it.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Optional

from .schema import INDEXED_FIELDS, Disposition, Outcome

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    instrument_number  TEXT PRIMARY KEY,
    book               TEXT,
    page               TEXT,
    document_type      TEXT,
    grantor            TEXT,
    grantee            TEXT,
    recording_date     TEXT,     -- ISO-8601, or NULL when it could not be read
    legal_description  TEXT,
    source_file        TEXT NOT NULL,
    backend            TEXT NOT NULL,
    disposition        TEXT NOT NULL,
    reason             TEXT,
    weakest_field      TEXT,
    weakest_confidence REAL,
    confidences        TEXT,     -- JSON, per field, kept for threshold tuning later
    posted_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_documents_bookpage  ON documents(book, page);
CREATE INDEX IF NOT EXISTS idx_documents_grantor   ON documents(grantor);
CREATE INDEX IF NOT EXISTS idx_documents_grantee   ON documents(grantee);
CREATE INDEX IF NOT EXISTS idx_documents_recorded  ON documents(recording_date);
CREATE INDEX IF NOT EXISTS idx_documents_queue     ON documents(disposition);

-- Exceptions keyed by file rather than instrument number, because the reason a document
-- is in the queue is often that the instrument number is exactly what could not be read.
CREATE TABLE IF NOT EXISTS exceptions (
    source_file        TEXT PRIMARY KEY,
    reason             TEXT NOT NULL,
    weakest_field      TEXT,
    weakest_confidence REAL,
    extracted          TEXT,     -- JSON: whatever WAS readable, so review starts partway
    seen_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CSV_COLUMNS = INDEXED_FIELDS + ["source_file"]


class Store:
    def __init__(self, db_path: Path, csv_path: Optional[Path] = None):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

        self.csv_path = csv_path
        self._csv_file = None
        self._csv = None
        if csv_path:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not csv_path.exists() or csv_path.stat().st_size == 0
            self._csv_file = csv_path.open("a", newline="", encoding="utf-8")
            self._csv = csv.writer(self._csv_file)
            if fresh:
                self._csv.writerow(CSV_COLUMNS)

    def write(self, outcome: Outcome) -> None:
        rec = outcome.record
        confidences = {n: rec.field(n).confidence for n in INDEXED_FIELDS}
        values = {n: rec.field(n).value for n in INDEXED_FIELDS}

        if outcome.disposition == Disposition.EXCEPTION:
            self.conn.execute(
                "INSERT INTO exceptions "
                "(source_file, reason, weakest_field, weakest_confidence, extracted) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(source_file) DO UPDATE SET "
                "  reason=excluded.reason, weakest_field=excluded.weakest_field,"
                "  weakest_confidence=excluded.weakest_confidence,"
                "  extracted=excluded.extracted",
                (rec.source_file, outcome.reason, outcome.weakest_field,
                 outcome.weakest_confidence, json.dumps(values)),
            )
            self.conn.commit()
            return

        self.conn.execute(
            "INSERT INTO documents "
            "(instrument_number, book, page, document_type, grantor, grantee,"
            " recording_date, legal_description, source_file, backend, disposition,"
            " reason, weakest_field, weakest_confidence, confidences) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(instrument_number) DO UPDATE SET "
            "  book=excluded.book, page=excluded.page,"
            "  document_type=excluded.document_type, grantor=excluded.grantor,"
            "  grantee=excluded.grantee, recording_date=excluded.recording_date,"
            "  legal_description=excluded.legal_description,"
            "  source_file=excluded.source_file, backend=excluded.backend,"
            "  weakest_field=excluded.weakest_field,"
            "  weakest_confidence=excluded.weakest_confidence,"
            "  confidences=excluded.confidences",
            (values["instrument_number"], values["book"], values["page"],
             values["document_type"], values["grantor"], values["grantee"],
             values["recording_date"], values["legal_description"],
             rec.source_file, rec.backend, outcome.disposition.value,
             outcome.reason, outcome.weakest_field, outcome.weakest_confidence,
             json.dumps(confidences)),
        )
        self.conn.commit()

        if self._csv:
            self._csv.writerow([values.get(c) or "" if c in values else rec.source_file
                                for c in CSV_COLUMNS])
            self._csv_file.flush()

    def counts(self) -> dict:
        cur = self.conn.cursor()
        posted = cur.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        queued = cur.execute("SELECT COUNT(*) FROM exceptions").fetchone()[0]
        return {"posted": posted, "exceptions": queued}

    def queue(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT source_file, weakest_field, weakest_confidence, reason "
            "FROM exceptions ORDER BY weakest_confidence ASC, source_file"
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
        if self._csv_file:
            self._csv_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
