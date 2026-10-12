#!/usr/bin/env python3
"""Drive the demo terminal session for a screen recording. One take, no fumbling.

Types each command out at human speed, runs it for real, and holds on the output for as
long as that shot needs. You start a screen recorder, run this, and do not touch the
keyboard again.

**Every command here actually executes.** Nothing is canned, nothing is replayed from a
transcript. If the pipeline broke, the recording breaks, which is the only honest way to
record a demo about output you can trust.

    python demo/record_session.py --rehearse    # narration cues + timings, no recording
    python demo/record_session.py --countdown 5 # the real thing
    python demo/record_session.py --speed 1.5   # faster typing and shorter holds

Before you record:
  * terminal font 18-20pt minimum - most of YouTube is a phone
  * window about 100x30
  * shorten the prompt; a long path eats the line and dates the video
  * close anything that can raise a notification

Narration script, shot list and YouTube metadata live in the project notes; this file is
only the hands.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

GREEN = "\033[32m"
DIM = "\033[2m"
AMBER = "\033[33m"
OFF = "\033[0m"

# Typing feel. Humans are not metronomes; the jitter is what stops it reading as a paste.
CPS_MEAN = 0.045
CPS_JITTER = 0.035
PAUSE_AFTER_PROMPT = 0.35
PAUSE_BEFORE_RUN = 0.45

rng = random.Random(7)          # seeded, so two takes have the same rhythm


class Shot:
    def __init__(self, cue: str, command: list[str], display: str,
                 hold: float, beat: float = 0.0, note: str = ""):
        self.cue = cue            # narration cue, shown only in rehearsal
        self.command = command    # what actually runs
        self.display = display    # what gets typed on screen
        self.hold = hold          # seconds to sit on the output
        self.beat = beat          # silent pause before the hold, for the reveal
        self.note = note          # direction for the person narrating


SHOTS = [
    Shot(
        cue="00:00  Cold open. Start talking over the typing - no intro, no logo.",
        note="\"This pipeline is about to post a legal description for the wrong piece "
             "of land. It will not crash. The run will succeed. Watch.\"",
        command=[PY, "-m", "deedpipe", "run", "samples/"],
        display="python -m deedpipe run samples/",
        hold=13.0,
    ),
    Shot(
        cue="00:18  What it did. Eight posted, four to a human.",
        note="\"The queue names the FIELD, not just 'rejected'. All four are one county "
             "layout with no field labels - grantor and grantee are only told apart by "
             "which line they sit on. Guess wrong and you invert the conveyance.\"",
        command=[PY, "-m", "deedpipe", "queue"],
        display="python -m deedpipe queue",
        hold=26.0,
    ),
    Shot(
        cue="00:50  The setup. A successful run, scored.",
        note="\"Same twelve documents against hand-written ground truth. 100% precision. "
             "Keep your eye on legal_description - eight out of eight.\"",
        command=[PY, "tools/score.py"],
        display="python tools/score.py",
        hold=31.0,
    ),
    Shot(
        cue="01:30  THE REVEAL. This is the shot the video exists for.",
        note="Say NOTHING for the beat. Then: \"Precision drops to 93.8. It wanted Range "
             "17 West. It got nothing. That is not a truncated string - that is a "
             "different piece of land, posted at 95% confidence. The run succeeded. "
             "Both times.\"",
        command=[PY, "tools/score.py", "--naive-linewrap"],
        display="python tools/score.py --naive-linewrap",
        beat=3.0,
        hold=38.0,
    ),
    Shot(
        cue="02:15  Why one number hides it.",
        note="\"Threshold zero: automation 100%, precision 100%. Which is nonsense, and "
             "it is nonsense my own scorer was reporting - precision only counted fields "
             "that were PRESENT. An absent value cannot be wrong. That is what the "
             "completeness column is. My measurement was wrong before my code was.\"",
        command=[PY, "tools/score.py", "--sweep"],
        display="python tools/score.py --sweep",
        beat=2.0,
        hold=31.0,
    ),
    Shot(
        cue="02:50  Close on the fixed result. No outro, no subscribe plea.",
        note="\"Runs in one command. The corpus generator writes the expected output, so "
             "the parser never grades its own homework. The defect is behind a flag with "
             "a test, so you can reproduce it instead of taking my word for it.\"",
        command=[PY, "tools/score.py"],
        display="python tools/score.py",
        hold=25.0,
    ),
]


def type_out(text: str, speed: float) -> None:
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        delay = (CPS_MEAN + rng.random() * CPS_JITTER) / speed
        if ch == " ":
            delay *= 0.6            # spaces are faster in real typing
        time.sleep(delay)
    sys.stdout.write("\n")
    sys.stdout.flush()


def run_shot(shot: Shot, speed: float) -> int:
    sys.stdout.write(f"{GREEN}${OFF} ")
    sys.stdout.flush()
    time.sleep(PAUSE_AFTER_PROMPT / speed)
    type_out(shot.display, speed)
    time.sleep(PAUSE_BEFORE_RUN / speed)

    # inherit stdio so the real colours and the real exit code come through
    result = subprocess.run(shot.command, cwd=ROOT)

    if shot.beat:
        time.sleep(shot.beat / speed)
    time.sleep(shot.hold / speed)
    print()
    return result.returncode


def rehearse(speed: float) -> int:
    total = 0.0
    print(f"{GREEN}rehearsal{OFF}  narration cues and timings. Nothing is recorded.\n")
    for shot in SHOTS:
        typing = len(shot.display) * (CPS_MEAN + CPS_JITTER / 2) / speed
        length = (PAUSE_AFTER_PROMPT + PAUSE_BEFORE_RUN) / speed + typing \
            + (shot.beat + shot.hold) / speed
        mins, secs = divmod(int(total), 60)
        print(f"{AMBER}{mins}:{secs:02d}{OFF}  {shot.cue}")
        print(f"       {DIM}$ {shot.display}{OFF}")
        for line in _wrap(shot.note, 86):
            print(f"       {line}")
        # 150 wpm is a calm technical read; +2s so the shot does not cut on the last word.
        # The holds were guessed once and came out at 1:14 for a three-minute script, so
        # the tool now checks itself rather than trusting the number in SHOTS.
        needed = len(shot.note.split()) / 2.5 + 2.0
        if shot.hold + shot.beat < needed:
            verdict = (f"{AMBER}TOO SHORT — narration needs ~{needed:.0f}s, "
                       f"this shot gives {shot.hold + shot.beat:.0f}s{OFF}")
        else:
            verdict = (f"{DIM}narration ~{needed:.0f}s, shot gives "
                       f"{shot.hold + shot.beat:.0f}s{OFF}")
        print(f"       {DIM}~{length:.0f}s on screen "
              f"(type {typing:.0f}s + beat {shot.beat:.0f}s + hold {shot.hold:.0f}s){OFF}")
        print(f"       {verdict}\n")
        total += length + 1.0       # +1 for command runtime, which is well under a second
    mins, secs = divmod(int(total), 60)
    print(f"{GREEN}estimated total: {mins}:{secs:02d}{OFF}")
    print(f"{DIM}target is 3:00-3:30. Adjust with --speed, or trim a hold in SHOTS.{OFF}")
    return 0


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rehearse", action="store_true",
                   help="print cues and timings without running anything")
    p.add_argument("--speed", type=float, default=1.0,
                   help="multiplier for typing speed and holds (default 1.0)")
    p.add_argument("--countdown", type=int, default=5,
                   help="seconds before the first keystroke, to start your recorder")
    p.add_argument("--no-clear", action="store_true")
    a = p.parse_args(argv)

    if a.rehearse:
        return rehearse(a.speed)

    samples = ROOT / "samples"
    if not list(samples.glob("*.pdf")):
        print("corpus missing. run: python tools/make_deeds.py", file=sys.stderr)
        return 1

    # cold start, so the run on camera is a real first run
    shutil.rmtree(ROOT / "build", ignore_errors=True)

    if not a.no_clear:
        os.system("cls" if os.name == "nt" else "clear")

    for n in range(a.countdown, 0, -1):
        sys.stdout.write(f"\r{DIM}recording in {n}... {OFF}")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * 30 + "\r")
    sys.stdout.flush()
    if not a.no_clear:
        os.system("cls" if os.name == "nt" else "clear")

    started = time.time()
    for shot in SHOTS:
        run_shot(shot, a.speed)

    # final frame: leave the clean result on screen, then a held blank prompt so the
    # last thing on camera is not a cursor sitting in the middle of output
    sys.stdout.write(f"{GREEN}${OFF} ")
    sys.stdout.flush()
    time.sleep(2.5)
    print()

    elapsed = time.time() - started
    mins, secs = divmod(int(elapsed), 60)
    # printed after the hold, so it is trivial to trim off the end of the take
    print(f"{DIM}[take length {mins}:{secs:02d} - trim this line]{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
