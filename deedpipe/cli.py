"""deedpipe command line.

    python -m deedpipe run samples/                 # one batch, print the result
    python -m deedpipe watch inbox/                 # poll a folder forever
    python -m deedpipe queue                        # what is waiting for review, and why
    python -m deedpipe score samples/               # per-field accuracy vs ground truth
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backends import get_backend
from .pipeline import DEFAULT_THRESHOLD, process, watch
from .schema import INDEXED_FIELDS
from .store import Store

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def _no_colour():
    global GREEN, RED, DIM, BOLD, OFF
    GREEN = RED = DIM = BOLD = OFF = ""


def cmd_run(args) -> int:
    folder = Path(args.folder)
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs in {folder}", file=sys.stderr)
        return 1

    out = Path(args.out)
    backend = get_backend(args.backend, naive_linewrap=getattr(args, 'naive_linewrap', False))

    with Store(out / "titleplant.db", out / "load.csv") as store:
        print(f"{BOLD}deedpipe{OFF}  {len(pdfs)} document(s)  "
              f"backend={args.backend}  threshold={args.threshold:.2f}\n")
        print(f"{'document':<18} {'instrument':<14} {'bk/pg':<10} "
              f"{'recorded':<12} {'weakest field':<20} {'':<4}")
        print("-" * 82)

        def show(outcome):
            r = outcome.record
            mark = f"{GREEN}post{OFF}" if outcome.ok else f"{RED}queue{OFF}"
            bkpg = "/".join(x for x in (r.book.value, r.page.value) if x) or "-"
            print(f"{r.source_file:<18} "
                  f"{(r.instrument_number.value or '-'):<14} "
                  f"{bkpg:<10} "
                  f"{(r.recording_date.value or '-'):<12} "
                  f"{outcome.weakest_field + ' ' + format(outcome.weakest_confidence, '.2f'):<20} "
                  f"{mark}")

        stats = process(pdfs, backend, store, out / "exceptions",
                        args.threshold, on_result=show)

        print("-" * 82)
        print(f"\n{BOLD}{stats.posted} posted{OFF} · "
              f"{stats.excepted} to review · "
              f"{stats.failed} unreadable · "
              f"{stats.elapsed:.2f}s "
              f"({stats.elapsed / max(stats.seen, 1) * 1000:.0f} ms/doc)")
        if stats.by_reason:
            print(f"\n{DIM}queued because of:{OFF}")
            for reason, n in sorted(stats.by_reason.items(), key=lambda kv: -kv[1]):
                print(f"  {n:>3}  {reason}")
        print(f"\n{DIM}db   {out / 'titleplant.db'}")
        print(f"csv  {out / 'load.csv'}")
        print(f"queue {out / 'exceptions'}{OFF}")
    return 0


def cmd_watch(args) -> int:
    out = Path(args.out)
    with Store(out / "titleplant.db", out / "load.csv") as store:
        print(f"watching {args.folder} (ctrl-c to stop)")
        try:
            watch(Path(args.folder), args.backend, store, out / "exceptions",
                  args.threshold, interval=args.interval)
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def cmd_queue(args) -> int:
    out = Path(args.out)
    db = out / "titleplant.db"
    if not db.exists():
        print(f"no database at {db} — run `deedpipe run` first", file=sys.stderr)
        return 1
    with Store(db) as store:
        rows = store.queue()
        counts = store.counts()
        if not rows:
            print(f"queue empty — {counts['posted']} document(s) posted")
            return 0
        print(f"{BOLD}{len(rows)} document(s) waiting for review{OFF}  "
              f"({counts['posted']} posted)\n")
        print(f"{'document':<20} {'field':<20} {'conf':<6} reason")
        print("-" * 92)
        for src, field, conf, reason in rows:
            print(f"{src:<20} {(field or '-'):<20} {conf:<6.2f} {reason}")
        print(f"\n{DIM}weakest first — the top of this list is where a reviewer starts{OFF}")
    return 0


def cmd_score(args) -> int:
    from tools.score import score_folder          # noqa: PLC0415 - optional dev tool
    return score_folder(Path(args.folder), args.backend, args.threshold)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="deedpipe",
        description="Recorded-document extraction with a reviewable exception queue.")
    p.add_argument("--no-colour", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--backend", default="text", choices=["text", "vision"])
        sp.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="minimum per-field confidence to post unattended")
        sp.add_argument("--out", default="build", help="output directory")
        sp.add_argument("--naive-linewrap", action="store_true",
                        help="reproduce a real defect: truncate wrapped values at the "
                             "first line. See TextBackend.__init__.")

    r = sub.add_parser("run", help="process a folder once")
    r.add_argument("folder")
    common(r)
    r.set_defaults(fn=cmd_run)

    w = sub.add_parser("watch", help="poll a folder for new scans")
    w.add_argument("folder")
    w.add_argument("--interval", type=float, default=2.0)
    common(w)
    w.set_defaults(fn=cmd_watch)

    q = sub.add_parser("queue", help="show what is waiting for review")
    q.add_argument("--out", default="build")
    q.set_defaults(fn=cmd_queue)

    s = sub.add_parser("score", help="per-field accuracy against ground truth")
    s.add_argument("folder", nargs="?", default="samples")
    common(s)
    s.set_defaults(fn=cmd_score)

    args = p.parse_args(argv)
    if args.no_colour or not sys.stdout.isatty():
        _no_colour()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
