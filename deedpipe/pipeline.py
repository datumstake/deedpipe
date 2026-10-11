"""Ingest, route, load. The routing rule is the only interesting part of this file.

A document posts unattended only if **every** critical field is present and the weakest
indexed field clears the threshold. Anything else goes to the exception queue carrying
the reason, so a reviewer opens the queue already knowing which field to look at.

The asymmetry is intentional. A false exception costs a human thirty seconds. A false
post writes a wrong row into a title plant, where it is not noticed until a search comes
back wrong — possibly years later, possibly in front of a closing. Those two errors are
not worth trading one-for-one, and a pipeline tuned as though they were will look like it
is performing well right up until it matters.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .backends import Backend, get_backend
from .schema import CRITICAL_FIELDS, INDEXED_FIELDS, Disposition, Outcome

DEFAULT_THRESHOLD = 0.80


@dataclass
class Stats:
    seen: int = 0
    posted: int = 0
    excepted: int = 0
    failed: int = 0
    elapsed: float = 0.0
    by_reason: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1


def route(outcome_record, threshold: float = DEFAULT_THRESHOLD) -> Outcome:
    """Decide whether a record may post unattended."""
    missing = outcome_record.missing_critical()
    weakest_field, weakest_conf = outcome_record.weakest()

    if missing:
        return Outcome(
            record=outcome_record,
            disposition=Disposition.EXCEPTION,
            reason=f"missing required field(s): {', '.join(missing)}",
            weakest_field=missing[0],
            weakest_confidence=0.0,
        )
    if weakest_conf < threshold:
        return Outcome(
            record=outcome_record,
            disposition=Disposition.EXCEPTION,
            reason=(f"{weakest_field} confidence {weakest_conf:.2f} "
                    f"below threshold {threshold:.2f}"),
            weakest_field=weakest_field,
            weakest_confidence=weakest_conf,
        )
    return Outcome(
        record=outcome_record,
        disposition=Disposition.POSTED,
        reason="",
        weakest_field=weakest_field,
        weakest_confidence=weakest_conf,
    )


def process(
    pdfs: Iterable[Path],
    backend: Backend,
    store,
    exception_dir: Optional[Path] = None,
    threshold: float = DEFAULT_THRESHOLD,
    on_result: Optional[Callable[[Outcome], None]] = None,
) -> Stats:
    stats = Stats()
    t0 = time.time()

    for pdf in pdfs:
        stats.seen += 1
        try:
            record = backend.extract(pdf)
        except Exception as exc:                      # noqa: BLE001 - must not halt a batch
            # One unreadable file must not stop the other 499. It becomes an exception
            # with the error as its reason, which is a result rather than a crash.
            stats.failed += 1
            stats.note(f"extraction error: {type(exc).__name__}")
            if exception_dir:
                _quarantine(pdf, exception_dir, f"extraction error: {exc}")
            continue

        outcome = route(record, threshold)
        store.write(outcome)

        if outcome.ok:
            stats.posted += 1
        else:
            stats.excepted += 1
            stats.note(outcome.weakest_field or "unknown")
            if exception_dir:
                _quarantine(pdf, exception_dir, outcome.reason)

        if on_result:
            on_result(outcome)

    stats.elapsed = time.time() - t0
    return stats


def _quarantine(pdf: Path, exception_dir: Path, reason: str) -> None:
    """Copy, never move.

    Moving the operator's input file means a bug in this pipeline loses their document.
    The source folder is somebody else's data and is treated as read-only.
    """
    exception_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, exception_dir / pdf.name)
    (exception_dir / f"{pdf.stem}.reason.txt").write_text(
        f"{pdf.name}\n{reason}\n", encoding="utf-8", newline="\n")


def watch(folder: Path, backend_name: str, store, exception_dir: Path,
          threshold: float = DEFAULT_THRESHOLD, interval: float = 2.0,
          once: bool = False) -> None:
    """Poll a folder for new PDFs.

    Polling rather than filesystem events on purpose: scanner output lands over SMB about
    as often as it lands locally, and inotify/ReadDirectoryChangesW are unreliable across
    network shares. A file is also only picked up once its size has stopped changing —
    otherwise a half-written scan gets read mid-copy, which reads as a corrupt PDF and
    sends a perfectly good document to the exception queue.
    """
    backend = get_backend(backend_name)
    seen: dict[str, int] = {}
    pending: dict[str, int] = {}

    while True:
        ready = []
        for pdf in sorted(folder.glob("*.pdf")):
            key = pdf.name
            if key in seen:
                continue
            size = pdf.stat().st_size
            if pending.get(key) == size and size > 0:
                ready.append(pdf)
                seen[key] = size
                pending.pop(key, None)
            else:
                pending[key] = size          # still landing; look again next tick
        if ready:
            process(ready, backend, store, exception_dir, threshold)
        if once:
            return
        time.sleep(interval)
