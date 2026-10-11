"""Tests for the routing rules and the loader. No framework: run it, read the exit code.

    python tests/test_pipeline.py

These concentrate on the decisions that can silently corrupt a title plant — routing,
validation, idempotency — rather than on extraction, which is covered end to end and
against ground truth by tools/score.py.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deedpipe.backends import TextBackend                       # noqa: E402
from deedpipe.pipeline import route                             # noqa: E402
from deedpipe.schema import (                                   # noqa: E402
    DeedRecord, Disposition, FieldValue, normalize_date,
)
from deedpipe.store import Store                                # noqa: E402

failures = 0
checks = 0


def ok(cond, what):
    global failures, checks
    checks += 1
    if not cond:
        failures += 1
        print(f"  FAIL  {what}")


def eq(got, want, what):
    global failures, checks
    checks += 1
    if got != want:
        failures += 1
        print(f"  FAIL  {what}\n          got  {got!r}\n         want  {want!r}")


def full_record(conf=0.95) -> DeedRecord:
    r = DeedRecord(source_file="x.pdf", backend="text")
    for f in ("instrument_number", "book", "page", "document_type",
              "grantor", "grantee", "recording_date", "legal_description"):
        setattr(r, f, FieldValue(value="v", confidence=conf))
    r.instrument_number = FieldValue(value="2024-123456", confidence=conf)
    r.recording_date = FieldValue(value="2024-01-05", confidence=conf)
    return r


# ------------------------------------------------------------------ routing
def test_routing():
    ok(route(full_record(0.95)).ok, "routing: a complete confident record posts")

    r = full_record(0.95)
    r.page = FieldValue()                       # critical field absent
    out = route(r)
    eq(out.disposition, Disposition.EXCEPTION, "routing: missing critical field queues")
    ok("page" in out.reason, "routing: the reason names the missing field")

    r = full_record(0.95)
    r.grantee = FieldValue(value="someone", confidence=0.40)
    out = route(r, threshold=0.80)
    eq(out.disposition, Disposition.EXCEPTION, "routing: weak field queues")
    eq(out.weakest_field, "grantee", "routing: the weakest field is named")
    ok("0.40" in out.reason and "0.80" in out.reason,
       "routing: the reason carries both the score and the threshold")

    # A document is only as postable as its worst field. One strong field must never
    # carry a weak one across the line -- that is the averaging mistake this avoids.
    r = full_record(1.0)
    r.legal_description = FieldValue(value="lot 1", confidence=0.10)
    ok(not route(r, 0.80).ok, "routing: one weak field blocks an otherwise perfect record")

    ok(route(full_record(0.80), 0.80).ok, "routing: threshold is inclusive")
    ok(not route(full_record(0.79), 0.80).ok, "routing: just below threshold queues")


# ------------------------------------------------------------------ validation
def test_dates():
    eq(normalize_date("October 4, 2019"), "2019-10-04", "date: long form")
    eq(normalize_date("10/04/2019"), "2019-10-04", "date: slashed")
    eq(normalize_date("2019-10-04"), "2019-10-04", "date: already ISO")
    eq(normalize_date("Octobr 4, 2019"), None, "date: misspelled month is not guessed")
    eq(normalize_date("February 30, 2019"), None, "date: impossible date rejected")
    eq(normalize_date(""), None, "date: empty")
    # The point of returning None: a date that cannot be read must never become a date
    # that sorts. 1970-01-01 in a recording-date column is a bug found years later.


def test_validator_drops_rather_than_weakens():
    b = TextBackend()
    r = DeedRecord()
    r.instrument_number = FieldValue(value="NOT-AN-INSTRUMENT!!", confidence=0.95)
    r.book = FieldValue(value="ABC", confidence=0.95)
    r.recording_date = FieldValue(value="whenever", confidence=0.95)
    r.grantor = FieldValue(value="x" * 200, confidence=0.95)
    out = b._validate(r)
    ok(not out.instrument_number.present, "validate: malformed instrument is dropped")
    ok(not out.book.present, "validate: non-numeric book is dropped")
    ok(not out.recording_date.present, "validate: unparseable date is dropped")
    ok(not out.grantor.present, "validate: runaway capture is dropped")
    # dropped, not down-weighted: a wrong value is not a low-confidence value
    eq(out.instrument_number.confidence, 0.0, "validate: dropped means zero confidence")


def test_leading_zeros_survive():
    r = DeedRecord()
    r.book = FieldValue(value="00412", confidence=0.95)
    eq(r.book.value, "00412", "book 00412 is not silently cast to 412")


# ------------------------------------------------------------------ loader
def test_store_idempotent():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        with Store(out / "t.db", out / "load.csv") as store:
            o = route(full_record(0.95))
            store.write(o)
            store.write(o)                       # same document delivered twice
            eq(store.counts()["posted"], 1,
               "store: re-posting the same instrument updates, never duplicates")


def test_store_records_exceptions():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        with Store(out / "t.db") as store:
            r = full_record(0.95)
            r.grantor = FieldValue(value="?", confidence=0.2)
            store.write(route(r, 0.80))
            eq(store.counts()["exceptions"], 1, "store: the queue is queryable, not a folder")
            rows = store.queue()
            eq(rows[0][1], "grantor", "store: the queue carries which field failed")


# ------------------------------------------------------------------ corpus
def test_corpus_end_to_end():
    samples = Path(__file__).resolve().parent.parent / "samples"
    if not samples.exists() or not list(samples.glob("*.pdf")):
        print("  SKIP  corpus not generated (python tools/make_deeds.py)")
        return
    sys.path.insert(0, str(samples.parent))
    from tools.score import score_folder          # noqa: PLC0415

    automation, precision, completeness, posted, queued, wrong = score_folder(
        samples, "text", 0.80, quiet=True)
    eq(wrong, 0, "corpus: nothing wrong posted unattended")
    eq(precision, 1.0, "corpus: posted precision is 100%")
    eq(completeness, 1.0, "corpus: posted rows are complete")
    ok(posted > 0, "corpus: something actually posted")
    ok(queued > 0, "corpus: the unlabelled layout is routed for review, not guessed")


if __name__ == "__main__":
    print("deedpipe tests\n")
    for fn in (test_routing, test_dates, test_validator_drops_rather_than_weakens,
               test_leading_zeros_survive, test_store_idempotent,
               test_store_records_exceptions, test_corpus_end_to_end):
        fn()
    print(f"\n{checks} checks, {failures} failure(s)")
    sys.exit(1 if failures else 0)
