"""Extraction backends. One interface, two implementations, and the seam is the point.

`TextBackend` reads the PDF's text layer and finds fields by label and shape. It needs no
API key, no network and no GPU, so the demo runs anywhere and costs nothing — and on
born-digital recorder output it is both faster and *more* accurate than a vision model,
because the characters are already there and nothing has to be inferred.

`VisionBackend` renders each page to an image and asks a vision model to fill the same
pydantic schema. This is the one that matters for genuinely scanned input.

Two things fall out of making this a seam rather than a hardcoded call:

1. **The schema, the confidence rules, the exception queue and the loader are all
   backend-agnostic.** Swapping extraction does not touch any of them, which is what lets
   a vision prompt be tuned against a scored corpus without also, invisibly, changing how
   records get routed.
2. **A document can be run through both and the answers compared.** Many "scanned" PDFs
   have already been through OCR and carry a text layer underneath. Agreement between two
   independent extractors is a far better confidence signal than a model's own opinion of
   itself — a model asked how sure it is will tell you what you want to hear.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Protocol

from .schema import (
    DeedRecord,
    FieldValue,
    looks_like_book_or_page,
    looks_like_instrument,
    normalize_date,
)


class Backend(Protocol):
    name: str

    def extract(self, pdf: Path) -> DeedRecord: ...


# ------------------------------------------------------------------ text layer
#
# Recorder output falls into three shapes and they need three strategies. Confidence is
# attached to the STRATEGY, not to the field: a value read off its own printed label is
# genuinely more trustworthy than one recognised by shape alone, and that difference is
# exactly what the routing threshold is for.

# 1. label on one line, value on the next -- the common recorder header block
STACKED_LABELS = {
    "instrument_number": ["instrument no.", "instrument no", "instrument number",
                          "instrument #", "document no.", "document number"],
    "book": ["book", "bk"],
    "page": ["page", "pg"],
    "recording_date": ["recorded", "recording date", "date recorded", "filed"],
    "grantor": ["grantor", "grantor(s)", "grantors"],
    "grantee": ["grantee", "grantee(s)", "grantees"],
    "legal_description": ["legal description", "legal"],
}
STACKED_CONFIDENCE = 0.95

# 2. label and value on the same line
INLINE_PATTERNS = {
    "instrument_number": [r"\binstr(?:ument)?\.?\s*#\s*([A-Z0-9\-]+)",
                          r"\binstrument\s*(?:no\.?|number)\s*[:\-]\s*([A-Z0-9\-]+)"],
    "book": [r"\bbk\.?\s*(\d{1,6})\b", r"\bbook\s*[:\-]\s*(\d{1,6})\b"],
    "page": [r"\bpg\.?\s*(\d{1,6})\b", r"\bpage\s*[:\-]\s*(\d{1,6})\b"],
}
INLINE_CONFIDENCE = 0.93

# 3. prose -- the parties named in an indenture sentence rather than a header
#    The {2,80} bound is load-bearing, not decoration. An unbounded lazy capture starting
#    at the first "and" in "made and entered into" happily swallows the entire first half
#    of the sentence to reach "party of the second part" -- producing a 130-character
#    "name" that looks like a successful extraction. Bounding the span makes that match
#    fail so the engine moves on to the right "and", and the length check in _validate is
#    the second net under it.
PROSE_PATTERNS = {
    "grantor": [
        r"called\s+GRANTOR[,\s]+(.{2,80}?),\s*part(?:y|ies)\s+of\s+the\s+first\s+part",
        r"between\s+(.{2,80}?),\s*part(?:y|ies)\s+of\s+the\s+first\s+part",
    ],
    "grantee": [
        r"called\s+GRANTOR,\s*and\s+(.{2,80}?),\s*part(?:y|ies)\s+of\s+the\s+second\s+part",
        r"\band\s+(.{2,80}?),\s*part(?:y|ies)\s+of\s+the\s+second\s+part",
    ],
    "recording_date": [r"entered\s+into\s+on\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})"],
}
PROSE_CONFIDENCE = 0.90

# 4. shape only -- no label anywhere, recognised purely by what it looks like.
#    Deliberately scored BELOW the default posting threshold: a bare token that merely
#    resembles an instrument number is a lead, not an answer, and it should land in front
#    of a human rather than in the title plant.
SHAPE_CONFIDENCE = 0.70

INSTRUMENT_SHAPE = re.compile(r"^\s*(\d{4}-\d{4,8})\s*$")
BOOKPAGE_SHAPE = re.compile(r"^\s*(\d{1,6})\s*/\s*(\d{1,6})\s*$")
BOOKPAGE_INLINE = re.compile(r"\bbk\.?\s*(\d{1,6})\s+pg\.?\s*(\d{1,6})\b", re.I)
DATE_SHAPE = re.compile(r"^\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\s*$")

DOC_TYPES = ["RELEASE OF MORTGAGE", "DEED OF TRUST", "WARRANTY DEED",
             "QUITCLAIM DEED", "MORTGAGE"]

LEGAL_SHAPE = re.compile(
    r"(Lot\s+\d+.*?(?:according to the recorded plat thereof|thereof))",
    re.I | re.S)


def read_text(pdf: Path) -> tuple[str, int]:
    """Text layer plus page count. pypdf keeps this a pure-Python dependency."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    parts = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(parts), len(reader.pages)


class TextBackend:
    """Label-and-shape extraction over the PDF text layer.

    Confidence is earned rather than asserted, and a value that fails its format check is
    *dropped* rather than passed along weakly — a malformed instrument number is not a
    low-confidence answer, it is a wrong one. That distinction is the rule that moved the
    drawing pipeline from 57% to 100% precision.
    """

    name = "text"

    def extract(self, pdf: Path) -> DeedRecord:
        raw, pages = read_text(pdf)
        lines = [ln.strip() for ln in raw.splitlines()]
        flat = re.sub(r"[ \t]+", " ", raw)
        rec = DeedRecord(source_file=pdf.name, page_count=pages, backend=self.name)

        self._stacked(rec, lines)
        self._inline(rec, flat)
        self._prose(rec, re.sub(r"\s+", " ", raw))
        self._shape(rec, lines)
        self._document_type(rec, flat)
        self._legal(rec, re.sub(r"\s+", " ", raw))

        return self._validate(rec)

    # -- strategies, strongest first; a field already filled is never overwritten
    @staticmethod
    def _set(rec: DeedRecord, field: str, value: str, conf: float, source: str) -> None:
        if rec.field(field).present:
            return
        value = value.strip().rstrip(",.;:")
        if value:
            setattr(rec, field, FieldValue(value=value, confidence=conf,
                                           source=source[:120]))

    def _stacked(self, rec: DeedRecord, lines: list[str]) -> None:
        lowered = [ln.lower().rstrip(":") for ln in lines]
        all_labels = {l for ls in STACKED_LABELS.values() for l in ls}
        for i, low in enumerate(lowered):
            for field, labels in STACKED_LABELS.items():
                if low not in labels:
                    continue
                # A legal description wraps across lines; every other field is one line.
                # Taking only the first line of a wrapped value does not fail loudly --
                # it truncates mid-sentence at a plausible boundary and posts a legal
                # description that describes a different piece of land. Found by scoring
                # the output, not by the run succeeding.
                span = len(lines) if field == "legal_description" else i + 3
                collected: list[str] = []
                for j in range(i + 1, min(span, len(lines))):
                    nxt = lines[j]
                    if not nxt:
                        if collected:
                            break           # blank line ends a wrapped value
                        continue
                    if lowered[j] in all_labels:
                        break
                    collected.append(nxt)
                    if field != "legal_description":
                        break
                if collected:
                    value = " ".join(collected)
                    self._set(rec, field, value, STACKED_CONFIDENCE,
                              f"{lines[i]} / {collected[0]}")

    def _inline(self, rec: DeedRecord, flat: str) -> None:
        for field, patterns in INLINE_PATTERNS.items():
            for pat in patterns:
                m = re.search(pat, flat, re.I)
                if m:
                    self._set(rec, field, m.group(1), INLINE_CONFIDENCE, m.group(0))
                    break
        if not (rec.book.present and rec.page.present):
            m = BOOKPAGE_INLINE.search(flat)
            if m:
                self._set(rec, "book", m.group(1), INLINE_CONFIDENCE, m.group(0))
                self._set(rec, "page", m.group(2), INLINE_CONFIDENCE, m.group(0))

    def _prose(self, rec: DeedRecord, oneline: str) -> None:
        for field, patterns in PROSE_PATTERNS.items():
            for pat in patterns:
                m = re.search(pat, oneline, re.I)
                if m:
                    self._set(rec, field, m.group(1), PROSE_CONFIDENCE, m.group(0))
                    break

    def _shape(self, rec: DeedRecord, lines: list[str]) -> None:
        """No label anywhere. Recognise by form alone, and score it low on purpose."""
        for ln in lines:
            if not rec.instrument_number.present:
                m = INSTRUMENT_SHAPE.match(ln)
                if m:
                    self._set(rec, "instrument_number", m.group(1),
                              SHAPE_CONFIDENCE, ln)
            if not (rec.book.present and rec.page.present):
                m = BOOKPAGE_SHAPE.match(ln)
                if m:
                    self._set(rec, "book", m.group(1), SHAPE_CONFIDENCE, ln)
                    self._set(rec, "page", m.group(2), SHAPE_CONFIDENCE, ln)
            if not rec.recording_date.present:
                m = DATE_SHAPE.match(ln)
                if m:
                    self._set(rec, "recording_date", m.group(1), SHAPE_CONFIDENCE, ln)
        # Grantor and grantee are deliberately NOT inferred by position here. On an
        # unlabelled layout the only thing distinguishing the two names is which line
        # they sit on, and getting them backwards silently inverts the conveyance — the
        # document would say the wrong party owns the property. That is exactly the class
        # of error worth sending to a human, and exactly what the vision backend is for.

    def _document_type(self, rec: DeedRecord, flat: str) -> None:
        upper = flat.upper()
        for t in DOC_TYPES:                 # longest-first, so RELEASE OF MORTGAGE wins
            if t in upper:
                self._set(rec, "document_type", t, 0.93, t)
                return

    def _legal(self, rec: DeedRecord, oneline: str) -> None:
        m = LEGAL_SHAPE.search(oneline)
        if m:
            self._set(rec, "legal_description", re.sub(r"\s+", " ", m.group(1)),
                      0.85, m.group(1)[:120])

    @staticmethod
    def _validate(rec: DeedRecord) -> DeedRecord:
        """Format checks. A value that fails one is removed, not down-weighted."""
        if rec.instrument_number.present and not looks_like_instrument(
                rec.instrument_number.value):
            rec.instrument_number = FieldValue(confidence=0.0,
                                               source=rec.instrument_number.source)
        for f in ("book", "page"):
            fv = rec.field(f)
            if fv.present and not looks_like_book_or_page(fv.value):
                setattr(rec, f, FieldValue(confidence=0.0, source=fv.source))
        if rec.recording_date.present:
            iso = normalize_date(rec.recording_date.value)
            if iso is None:
                # unparseable is reported, never guessed into something that sorts
                rec.recording_date = FieldValue(confidence=0.0,
                                                source=rec.recording_date.source)
            else:
                rec.recording_date.value = iso
        for f in ("grantor", "grantee"):
            fv = rec.field(f)
            if fv.present and len(fv.value) > 120:
                # a runaway capture has swallowed the next clause; it is not a name
                setattr(rec, f, FieldValue(confidence=0.0, source=fv.source))
        return rec


# ------------------------------------------------------------------ vision
VISION_PROMPT = """\
You are reading one page of a recorded legal document from a county recorder's office.

Return ONLY a JSON object with exactly these keys:
  instrument_number, book, page, document_type, grantor, grantee,
  recording_date, legal_description

Rules, and the first one matters more than the rest:

1. If a field is not legibly present on the page, return null for it. Do NOT infer, do
   NOT complete a partially visible value, and do NOT carry a value over from another
   field. A null is a correct answer. A plausible guess is not.
2. Copy values exactly as printed, including leading zeros. Book "00412" is not book 412.
3. recording_date: ISO-8601 (YYYY-MM-DD) only if the full date is legible, else null.
4. document_type must be one of: WARRANTY DEED, QUITCLAIM DEED, MORTGAGE,
   RELEASE OF MORTGAGE, DEED OF TRUST. Anything else or unclear: null.
5. grantor is the party conveying; grantee is the party receiving. On a release, the
   lender is the grantor. If the page does not make the direction unambiguous, return
   null for BOTH rather than guessing which way round they go -- reversing them inverts
   the conveyance, and a reversed deed reads as perfectly valid.

Alongside it, return a second object "confidence" with the same keys, each 0 to 1,
describing how legible that specific value was -- not how plausible it looks. A crisp
value in an otherwise smudged document is still a crisp value.
"""


class VisionBackend:
    """Render each page and ask a vision model to fill the same schema.

    Not exercised in the demo: it needs an API key and bills per page, and a demo the
    reader cannot run themselves is worth very little. The prompt above is the real one,
    the schema it fills is the same `DeedRecord`, and the routing downstream is unchanged
    — which is the part worth showing.

    Cost note, because it decides the architecture rather than being a footnote: at
    roughly 1-2 cents per page, 500 documents a day is $25-100/day to a vendor. Running
    the text backend first and calling vision only on what it could not read is not an
    optimisation, it is the difference between a line item and a problem.
    """

    name = "vision"

    def __init__(self, model: str = "claude-sonnet-5", api_key: Optional[str] = None,
                 dpi: int = 200):
        self.model = model
        self.api_key = api_key
        self.dpi = dpi

    def extract(self, pdf: Path) -> DeedRecord:
        raise NotImplementedError(
            "VisionBackend needs an API key and bills per page, so it is deliberately "
            "not wired up in the public demo. The prompt, the schema it fills and the "
            "routing it feeds are all here unchanged -- see VISION_PROMPT. "
            "Run with --backend text to see the pipeline end to end at zero cost."
        )


def get_backend(name: str) -> Backend:
    if name == "text":
        return TextBackend()
    if name == "vision":
        return VisionBackend()
    raise ValueError(f"unknown backend: {name!r} (text|vision)")
