#!/usr/bin/env python3
"""Generate synthetic recorded-document PDFs with exact ground truth beside each one.

Why synthetic: real deeds are public record but they are also somebody's name and
address, and a demo corpus does not need either. These are invented people on invented
streets. More importantly, a generated document knows what it says -- so each PDF gets a
`.truth.json` recording the correct answer, which is the only thing that makes a per-field
accuracy number possible. You cannot score a pipeline against a document whose correct
answer nobody wrote down.

Three county layouts on purpose, because "do all your recorders format the same way" is
the question that decides the whole build:

    marion     clean two-column header block, labels left of values
    calloway   recording stamp top-right, grantor/grantee in a prose paragraph
    deshler    everything in one dense block, no labels, fixed field order

A pipeline that only works on `marion` looks finished and is not.

    python tools/make_deeds.py            # writes samples/

Dev-only dependency: reportlab. The pipeline itself does not need it.
"""

import json
import random
import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

FIRST = ["Harold", "Marguerite", "Dennis", "Loretta", "Vernon", "Estelle",
         "Clifford", "Juanita", "Raymond", "Doris", "Elmer", "Wanda"]
LAST = ["Kessler", "Brandt", "Whitlock", "Ferris", "Oyelaran", "Nakashima",
        "Castellanos", "Petrov", "Abernathy", "Quintero", "Lindqvist", "Odum"]
ENTITY = ["{} Family Trust", "{} Holdings LLC", "{} Properties, Inc.",
          "Estate of {}", "{} Revocable Living Trust"]
STREETS = ["Pennyroyal Road", "Sycamore Bend", "West Oglethorpe Street",
           "County Road 1140", "Shiloh Church Lane", "North Depot Avenue"]
KINDS = ["WARRANTY DEED", "QUITCLAIM DEED", "MORTGAGE",
         "RELEASE OF MORTGAGE", "DEED OF TRUST"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def person(rng):
    if rng.random() < 0.3:
        return rng.choice(ENTITY).format(rng.choice(LAST))
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def legal_description(rng):
    lot = rng.randint(1, 48)
    block = rng.randint(1, 12)
    sub = f"{rng.choice(LAST).upper()} ADDITION"
    sec, twp, rng_ = rng.randint(1, 36), rng.randint(1, 30), rng.randint(1, 20)
    return (f"Lot {lot}, Block {block} of {sub}, a subdivision in Section {sec}, "
            f"Township {twp} North, Range {rng_} West, according to the recorded plat "
            f"thereof")


def make_record(rng, county):
    y = rng.randint(2019, 2026)
    return {
        "county": county,
        "instrument_number": f"{y}-{rng.randint(100000, 999999)}",
        "book": str(rng.randint(100, 4800)),
        "page": str(rng.randint(1, 899)),
        "document_type": rng.choice(KINDS),
        "grantor": person(rng),
        "grantee": person(rng),
        "recording_date": f"{rng.choice(MONTHS)} {rng.randint(1, 28)}, {y}",
        "legal_description": legal_description(rng),
        "property_address": f"{rng.randint(100, 9800)} {rng.choice(STREETS)}",
        "consideration": f"${rng.randint(15, 880) * 1000:,}.00",
    }


# ------------------------------------------------------------------ layouts
def layout_marion(c, r):
    """Labels in a left column, values in a right column. The easy one."""
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(4.25 * inch, 10.2 * inch, r["document_type"])
    c.setFont("Helvetica", 8)
    c.drawCentredString(4.25 * inch, 10.0 * inch, f"{r['county']} County Recorder")

    rows = [
        ("Instrument No.", r["instrument_number"]),
        ("Book", r["book"]),
        ("Page", r["page"]),
        ("Recorded", r["recording_date"]),
        ("Grantor", r["grantor"]),
        ("Grantee", r["grantee"]),
        ("Consideration", r["consideration"]),
        ("Property Address", r["property_address"]),
    ]
    y = 9.4 * inch
    for label, value in rows:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(1.0 * inch, y, label)
        c.setFont("Helvetica", 10)
        c.drawString(2.6 * inch, y, value)
        y -= 0.3 * inch

    c.setFont("Helvetica-Bold", 9)
    c.drawString(1.0 * inch, y - 0.2 * inch, "Legal Description")
    c.setFont("Helvetica", 9)
    wrap(c, r["legal_description"], 1.0 * inch, y - 0.45 * inch, 88)


def layout_calloway(c, r):
    """Recording stamp boxed top-right; the parties appear in a prose sentence."""
    c.rect(5.2 * inch, 9.3 * inch, 2.6 * inch, 1.2 * inch)
    c.setFont("Helvetica", 7)
    c.drawString(5.32 * inch, 10.28 * inch, "RECORDED IN THE OFFICE OF THE")
    c.drawString(5.32 * inch, 10.14 * inch, f"CLERK OF {r['county'].upper()} COUNTY")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(5.32 * inch, 9.92 * inch, f"INSTR # {r['instrument_number']}")
    c.drawString(5.32 * inch, 9.74 * inch, f"BK {r['book']}  PG {r['page']}")
    c.setFont("Helvetica", 8)
    c.drawString(5.32 * inch, 9.52 * inch, r["recording_date"])

    c.setFont("Helvetica-Bold", 13)
    c.drawString(1.0 * inch, 8.9 * inch, r["document_type"])

    c.setFont("Helvetica", 10)
    prose = (f"THIS INDENTURE, made and entered into on {r['recording_date']}, "
             f"by and between {r['grantor']}, party of the first part, hereinafter "
             f"called GRANTOR, and {r['grantee']}, party of the second part, "
             f"hereinafter called GRANTEE, WITNESSETH, that for and in consideration "
             f"of the sum of {r['consideration']}, the receipt whereof is hereby "
             f"acknowledged, the GRANTOR does hereby convey unto the GRANTEE the "
             f"following described real property, to wit:")
    wrap(c, prose, 1.0 * inch, 8.5 * inch, 92)
    wrap(c, r["legal_description"], 1.0 * inch, 7.2 * inch, 92)
    c.drawString(1.0 * inch, 6.5 * inch, f"Commonly known as {r['property_address']}.")


def layout_deshler(c, r):
    """No labels at all. One dense block in a fixed order -- position is the only cue."""
    c.setFont("Courier-Bold", 10)
    c.drawString(0.9 * inch, 10.3 * inch, r["document_type"])
    c.setFont("Courier", 9)
    lines = [
        f"{r['county']} COUNTY",
        r["instrument_number"],
        f"{r['book']}/{r['page']}",
        r["recording_date"],
        "",
        r["grantor"],
        r["grantee"],
        "",
        r["property_address"],
        r["consideration"],
    ]
    y = 10.0 * inch
    for line in lines:
        c.drawString(0.9 * inch, y, line)
        y -= 0.22 * inch
    wrap(c, r["legal_description"], 0.9 * inch, y - 0.1 * inch, 76, font="Courier", size=9)


def wrap(c, text, x, y, width, font="Helvetica", size=9, leading=0.17):
    c.setFont(font, size)
    words, line = text.split(), ""
    for w in words:
        trial = f"{line} {w}".strip()
        if len(trial) > width:
            c.drawString(x, y, line)
            y -= leading * inch
            line = w
        else:
            line = trial
    if line:
        c.drawString(x, y, line)
    return y


LAYOUTS = {"marion": layout_marion, "calloway": layout_calloway, "deshler": layout_deshler}


def main():
    SAMPLES.mkdir(parents=True, exist_ok=True)
    rng = random.Random(90210)          # seeded: the corpus is reproducible
    n = 0
    for county, fn in LAYOUTS.items():
        for i in range(4):
            r = make_record(rng, county.capitalize())
            name = f"{county}_{i:02d}"
            pdf = SAMPLES / f"{name}.pdf"
            c = canvas.Canvas(str(pdf), pagesize=letter)
            fn(c, r)
            c.showPage()
            c.save()
            (SAMPLES / f"{name}.truth.json").write_text(
                json.dumps(r, indent=2) + "\n", newline="\n")
            n += 1
            print(f"  {name}.pdf  {pdf.stat().st_size:>6} bytes  {r['document_type']}")
    print(f"\n{n} documents across {len(LAYOUTS)} county layouts, with ground truth")


if __name__ == "__main__":
    sys.exit(main())
