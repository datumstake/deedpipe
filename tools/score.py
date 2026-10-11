#!/usr/bin/env python3
"""Score the pipeline against ground truth, per field, and separate the two numbers
that actually matter.

Most extraction tools report one accuracy figure. That figure hides the only distinction
an operator cares about:

    AUTOMATION RATE   how much posted without a human  -- how much work was saved
    POSTED PRECISION  of what posted, how much was right -- whether it can be trusted

They pull in opposite directions and the trade is set by one number, the confidence
threshold. Lower it and automation rate goes up and precision goes down. A single
"accuracy: 94%" tells you nothing about which side of that trade you are on, and 94%
posted precision in a title plant means six wrong rows in every hundred searches.

**Posted precision is the one with teeth.** A queued document costs a reviewer thirty
seconds. A wrongly posted document is found years later, possibly at a closing. This
script reports them separately and refuses to average them.

    python tools/score.py                      # default: samples/, text backend
    python tools/score.py --threshold 0.60     # watch the trade move
    python tools/score.py --sweep              # the whole curve at once
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deedpipe.backends import get_backend            # noqa: E402
from deedpipe.pipeline import DEFAULT_THRESHOLD, route  # noqa: E402
from deedpipe.schema import INDEXED_FIELDS, normalize_date  # noqa: E402


def normalise(field: str, value: str | None) -> str:
    """Compare on meaning, not on whitespace.

    Kept deliberately thin. Every normalisation rule here is a way the scorer can be
    talked into accepting a wrong answer, so each one has to be justifiable out loud:
    case and inner whitespace do not change which document a row refers to; a date in two
    formats is the same date. Nothing else is forgiven.
    """
    if value is None:
        return ""
    v = re.sub(r"\s+", " ", str(value)).strip()
    if field == "recording_date":
        return normalize_date(v) or v
    if field == "document_type":
        return v.upper()
    if field in ("book", "page", "instrument_number"):
        return v.upper()
    return v.lower().rstrip(".,")


def score_folder(folder: Path, backend_name: str = "text",
                 threshold: float = DEFAULT_THRESHOLD, quiet: bool = False,
                 naive_linewrap: bool = False) -> int:
    pdfs = sorted(folder.glob("*.pdf"))
    truths = {p.stem: p for p in folder.glob("*.truth.json")}
    if not pdfs:
        print(f"no PDFs in {folder}", file=sys.stderr)
        return 1

    backend = get_backend(backend_name, naive_linewrap=naive_linewrap)

    # per field: [posted_correct, posted_total, present_correct, present_total, truth_total]
    tally = {f: [0, 0, 0, 0, 0] for f in INDEXED_FIELDS}
    posted = queued = 0
    # Precision alone is gameable by posting rows with half their fields null: an absent
    # value cannot be wrong. Completeness is the counterweight -- of the fields that DO
    # exist on the documents we posted, how many actually made it into the row. A title
    # plant is searched by grantor and grantee, so a posted row missing them is not a
    # partial win, it is an unsearchable record taking up a primary key.
    comp_found = comp_want = 0
    wrong_rows: list[tuple[str, str, str, str]] = []

    for pdf in pdfs:
        truth_path = truths.get(pdf.stem + ".truth")
        if truth_path is None:
            continue
        truth = json.loads(truth_path.read_text())
        rec = backend.extract(pdf)
        outcome = route(rec, threshold)
        if outcome.ok:
            posted += 1
        else:
            queued += 1

        for f in INDEXED_FIELDS:
            want = normalise(f, truth.get(f))
            got = normalise(f, rec.field(f).value)
            if want:
                tally[f][4] += 1
                if outcome.ok:
                    comp_want += 1
                    if rec.field(f).present:
                        comp_found += 1
            if rec.field(f).present:
                tally[f][3] += 1
                if got == want:
                    tally[f][2] += 1
            if outcome.ok and rec.field(f).present:
                tally[f][1] += 1
                if got == want:
                    tally[f][0] += 1
                else:
                    wrong_rows.append((pdf.name, f, truth.get(f, ""), rec.field(f).value))

    pc_num = sum(t[0] for t in tally.values())
    pc_den = sum(t[1] for t in tally.values())
    automation = posted / len(pdfs) if pdfs else 0.0
    precision = pc_num / pc_den if pc_den else 1.0
    completeness = comp_found / comp_want if comp_want else 1.0

    if quiet:
        return (automation, precision, completeness, posted, queued, len(wrong_rows))

    print(f"deedpipe score   {len(pdfs)} documents   backend={backend_name}   "
          f"threshold={threshold:.2f}\n")
    print(f"{'field':<20} {'posted ok':>12} {'posted prec':>12} "
          f"{'found':>8} {'in truth':>9}")
    print("-" * 66)
    for f in INDEXED_FIELDS:
        ok, tot, pok, ptot, truth_n = tally[f]
        prec = f"{ok / tot * 100:.0f}%" if tot else "-"
        print(f"{f:<20} {f'{ok}/{tot}':>12} {prec:>12} "
              f"{f'{pok}/{ptot}':>8} {truth_n:>9}")
    print("-" * 66)

    print(f"\n  AUTOMATION RATE   {posted}/{len(pdfs)} = {automation * 100:.0f}%"
          f"   ({queued} sent for review)")
    print(f"  POSTED PRECISION  {pc_num}/{pc_den} = {precision * 100:.1f}%"
          f"   ({len(wrong_rows)} wrong value(s) posted)")
    print(f"  POSTED COMPLETE   {comp_found}/{comp_want} = {completeness * 100:.1f}%"
          f"   (fields present on rows that posted unattended)")

    if wrong_rows:
        print(f"\n  {len(wrong_rows)} WRONG VALUE(S) POSTED UNATTENDED:")
        for doc, f, want, got in wrong_rows[:20]:
            print(f"    {doc:<18} {f:<20} want {want!r}  got {got!r}")
        print("\n  Any row in that list is a defect, not a rounding error: it entered")
        print("  the title plant without a human ever seeing it.")
    else:
        print("\n  Nothing wrong was posted unattended. Everything the pipeline was not")
        print("  sure about went to a person instead of into the database.")
    return 0 if not wrong_rows else 1


def sweep(folder: Path, backend_name: str) -> int:
    """The trade-off curve. This is the picture that should decide the threshold."""
    print(f"threshold sweep   {backend_name} backend   "
          f"{len(list(folder.glob('*.pdf')))} documents\n")
    print(f"{'threshold':>10} {'automation':>12} {'precision':>11} "
          f"{'complete':>10} {'posted':>8} {'queued':>8} {'wrong':>7}")
    print("-" * 76)
    for t in [0.0, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        automation, precision, completeness, posted, queued, wrong = score_folder(
            folder, backend_name, t, quiet=True)
        flag = ""
        if wrong:
            flag = "  <-- posts wrong values"
        elif posted and completeness < 1.0:
            flag = "  <-- posts rows with holes in them"
        print(f"{t:>10.2f} {automation * 100:>11.0f}% {precision * 100:>10.1f}% "
              f"{completeness * 100:>9.0f}% {posted:>8} {queued:>8} {wrong:>7}{flag}")
    print("\nRead all three columns together — which is the whole reason they are")
    print("printed apart. Automation can be driven to 100% by dropping the threshold")
    print("to zero, and the completeness column is what that actually costs: rows")
    print("land with null grantors, and a title plant is searched BY grantor.")
    print("Ship the lowest threshold where `wrong` is 0 and `complete` is 100%.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("folder", nargs="?", default="samples", type=Path)
    p.add_argument("--backend", default="text", choices=["text", "vision"])
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--sweep", action="store_true", help="scan the whole threshold curve")
    p.add_argument("--naive-linewrap", action="store_true",
                   help="reproduce a real defect: take only the first line of a "
                        "wrapped value, which truncates legal descriptions into a "
                        "description of different land and posts them at 0.95")
    a = p.parse_args(argv)
    if a.sweep:
        return sweep(a.folder, a.backend)
    return score_folder(a.folder, a.backend, a.threshold,
                        naive_linewrap=a.naive_linewrap)


if __name__ == "__main__":
    sys.exit(main())
