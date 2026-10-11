"""The contract. Everything downstream is typed by what is in this file.

Two deliberate choices, and they are the ones that decide whether a pipeline is safe to
point at a database:

**Confidence is per field, not per document.** A deed where the instrument number is
unambiguous and the legal description is half-cut-off is not "0.6 confident" — it is
certain about one thing and unsure about another. Collapsing that to a single document
score throws away the only information a reviewer needs, which is *which field to look
at*. The exception queue routes on the weakest field and names it.

**The schema rejects rather than coerces.** `book` is a string, not an int, because
`00412` is a real book number and `int("00412")` silently becomes `412`, which indexes a
different book. Anywhere a value could be quietly mangled into something plausible, it
stays a string and gets validated by pattern instead.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class DocumentType(str, Enum):
    WARRANTY_DEED = "WARRANTY DEED"
    QUITCLAIM_DEED = "QUITCLAIM DEED"
    MORTGAGE = "MORTGAGE"
    RELEASE = "RELEASE OF MORTGAGE"
    DEED_OF_TRUST = "DEED OF TRUST"
    UNKNOWN = "UNKNOWN"


class FieldValue(BaseModel):
    """One extracted value, with where it came from and how sure we are.

    `source` is not decoration. A value a reviewer cannot locate on the page is a value
    they have to re-key from scratch, which costs more than the extraction saved.
    """

    value: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    source: Optional[str] = Field(
        default=None, description="the text the value was read out of, verbatim")

    @property
    def present(self) -> bool:
        return self.value is not None and self.value.strip() != ""


# The fields the title plant actually indexes on. Order matters: it is the order a
# reviewer reads them in, and the order of the CSV load file.
INDEXED_FIELDS = [
    "instrument_number",
    "book",
    "page",
    "document_type",
    "grantor",
    "grantee",
    "recording_date",
    "legal_description",
]

# Fields that must be present and confident for a record to post unattended. A missing
# legal description is survivable and gets flagged; a missing instrument number means the
# record cannot be found again, which makes the row worse than useless.
CRITICAL_FIELDS = ["instrument_number", "book", "page", "recording_date"]


class DeedRecord(BaseModel):
    """One recorded document, as extracted. Every field carries its own confidence."""

    instrument_number: FieldValue = FieldValue()
    book: FieldValue = FieldValue()
    page: FieldValue = FieldValue()
    document_type: FieldValue = FieldValue()
    grantor: FieldValue = FieldValue()
    grantee: FieldValue = FieldValue()
    recording_date: FieldValue = FieldValue()
    legal_description: FieldValue = FieldValue()

    # provenance, never extracted
    source_file: str = ""
    page_count: int = 0
    backend: str = ""

    @field_validator("book", "page", mode="after")
    @classmethod
    def _keep_leading_zeros(cls, v: FieldValue) -> FieldValue:
        # Deliberately NOT int(). Book 00412 and book 412 are different books, and the
        # cast is the kind of silent mangling that is invisible until a title search
        # comes back empty.
        if v.value is not None:
            v.value = v.value.strip()
        return v

    def field(self, name: str) -> FieldValue:
        return getattr(self, name)

    def weakest(self) -> tuple[str, float]:
        """The field a reviewer should look at first, and its confidence."""
        worst_name, worst = INDEXED_FIELDS[0], 1.0
        for name in INDEXED_FIELDS:
            fv = self.field(name)
            score = fv.confidence if fv.present else 0.0
            if score < worst:
                worst_name, worst = name, score
        return worst_name, worst

    def missing_critical(self) -> list[str]:
        return [n for n in CRITICAL_FIELDS if not self.field(n).present]


class Disposition(str, Enum):
    POSTED = "posted"
    EXCEPTION = "exception"


class Outcome(BaseModel):
    """What happened to one document, and why. The `why` is the point.

    An exception queue that is just a folder of rejected files makes a human re-do the
    triage the pipeline already did. Every routed document carries the field that failed,
    its score, and the threshold it failed against, so the queue is workable rather than
    a pile.
    """

    record: DeedRecord
    disposition: Disposition
    reason: str = ""
    weakest_field: str = ""
    weakest_confidence: float = 0.0

    @property
    def ok(self) -> bool:
        return self.disposition == Disposition.POSTED


# ------------------------------------------------------------------ validation helpers
INSTRUMENT_RE = re.compile(r"^\d{4}-\d{4,8}$|^\d{6,12}$")
BOOKPAGE_RE = re.compile(r"^\d{1,6}$")


def looks_like_instrument(s: str) -> bool:
    return bool(INSTRUMENT_RE.match(s.strip()))


def looks_like_book_or_page(s: str) -> bool:
    return bool(BOOKPAGE_RE.match(s.strip()))


def normalize_date(s: str) -> Optional[str]:
    """ISO-8601 or nothing.

    Returning None on an unparseable date is the whole point: a recording date that
    silently became today's date, or 1970-01-01, is a title-search bug that surfaces
    years later. Unparseable is a fact worth reporting.
    """
    s = s.strip().rstrip(".")
    months = {m.lower(): i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)}
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", s)
    if m and m.group(1).lower() in months:
        try:
            return date(int(m.group(3)), months[m.group(1).lower()],
                        int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        try:
            return date(*map(int, m.groups())).isoformat()
        except ValueError:
            return None
    return None
