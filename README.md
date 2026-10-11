# deedpipe

**Recorded-document extraction with an exception queue that actually tells you something.**

Watch a folder of scanned deeds, mortgages and releases. Extract the indexed fields into a
typed schema. Post what the pipeline can stand behind, and route everything else to a human
*with the reason attached*.

```
$ python -m deedpipe run samples/

deedpipe  12 document(s)  backend=text  threshold=0.80

document           instrument     bk/pg      recorded     weakest field
----------------------------------------------------------------------------------
calloway_00.pdf    2019-557334    4066/659   2019-09-07   legal_description 0.85 post
calloway_01.pdf    2023-329496    2283/813   2023-02-06   legal_description 0.85 post
deshler_00.pdf     2024-248228    1585/417   2024-09-03   grantor 0.00         queue
deshler_01.pdf     2024-441953    734/259    2024-08-05   grantor 0.00         queue
marion_00.pdf      2019-946854    4511/198   2019-10-04   document_type 0.93   post
...
----------------------------------------------------------------------------------

8 posted · 4 to review · 0 unreadable · 0.31s (26 ms/doc)

queued because of:
    4  grantor
```

Those four are not failures. They are the Deshler county layout, which prints no field
labels at all — the only thing separating grantor from grantee is which line they sit on.
Guessing that wrong silently inverts the conveyance, and a reversed deed reads as perfectly
valid forever. So they go to a person.

---

## The number that matters

Most extraction tools report one accuracy figure. One figure hides the only distinction an
operator cares about, so this reports three and refuses to average them:

```
$ python tools/score.py --sweep

 threshold   automation   precision   complete   posted   queued   wrong
----------------------------------------------------------------------------
      0.00         100%      100.0%        92%       12        0       0  <-- posts rows with holes in them
      0.50          67%      100.0%       100%        8        4       0
      0.80          67%      100.0%       100%        8        4       0
      0.90          33%      100.0%       100%        4        8       0
      0.95           0%      100.0%       100%        0       12       0
```

| | |
|---|---|
| **automation rate** | how much posted without a human — how much work was saved |
| **posted precision** | of what posted, how much was *right* — whether it can be trusted |
| **posted complete** | of the fields that exist on those documents, how many made it into the row |

Automation alone can be driven to 100% by dropping the threshold to zero. The completeness
column is what that costs: four rows land with a null grantor, and **a title plant is
searched by grantor**. A row you cannot find is not a row.

Posted precision is the one with teeth. A queued document costs a reviewer thirty seconds.
A wrongly posted one is found years later, possibly at a closing.

Scored against committed ground truth — `samples/*.truth.json`, written by the generator
that drew each document, so the pipeline never grades its own homework:

```
$ python tools/score.py

field                   posted ok  posted prec    found  in truth
------------------------------------------------------------------
instrument_number             8/8         100%    12/12        12
book                          8/8         100%    12/12        12
page                          8/8         100%    12/12        12
document_type                 8/8         100%    12/12        12
grantor                       8/8         100%      8/8        12
grantee                       8/8         100%      8/8        12
recording_date                8/8         100%    12/12        12
legal_description             8/8         100%    12/12        12

  AUTOMATION RATE   8/12 = 67%     (4 sent for review)
  POSTED PRECISION  64/64 = 100.0% (0 wrong value(s) posted)
  POSTED COMPLETE   64/64 = 100.0% (fields present on rows that posted unattended)
```

## Run it

```sh
pip install pydantic pypdf
python tools/make_deeds.py        # 12 documents, 3 county layouts, with ground truth
python -m deedpipe run samples/   # extract, route, load
python -m deedpipe queue          # what is waiting for review, weakest first
python tools/score.py --sweep     # the trade-off curve
python tests/test_pipeline.py     # 29 checks
```

No API key, no network, no cloud account. The whole demo runs offline in about a second.

## What it does

| | |
|---|---|
| **folder watcher** | polls for new PDFs; a file is only picked up once its size stops changing, so a scan still landing over SMB is not read mid-copy |
| **typed extraction** | pydantic schema, per-field confidence, and the verbatim source text each value came from |
| **exception queue** | routes on the weakest field and records which one and why — the queue is a table you can query, not a folder of rejects |
| **idempotent loader** | SQLite with `ON CONFLICT DO UPDATE` on instrument number, plus a CSV/ASCII load file; the same scan arriving twice updates one row instead of creating two |
| **threshold sweep** | so the automation/precision trade is a decision someone made, not an accident |

### Design decisions worth arguing with

**Confidence is per field, not per document.** A deed where the instrument number is crisp
and the legal description is half-cut-off is not "0.6 confident" — it is certain about one
thing and unsure about another. Collapsing that to one score throws away the only thing a
reviewer needs: *which field to look at*.

**The schema rejects rather than coerces.** `book` is a string, because `00412` is a real
book number and `int("00412")` is `412`, which indexes a different book. An unparseable
recording date returns null rather than something that sorts — a date silently defaulted to
1970-01-01 is a bug nobody finds for years.

**A value that fails validation is dropped, not down-weighted.** A malformed instrument
number is not a low-confidence answer, it is a wrong one.

**Exceptions are copied, never moved.** The input folder is somebody else's data and is
treated read-only. A bug here must not lose their document.

**One unreadable file does not stop the batch.** It becomes an exception carrying the error
as its reason — a result, not a crash.

## Backends

| | |
|---|---|
| `text` | reads the PDF text layer, finds fields by label and shape. No key, no network, no cost. On born-digital recorder output it is faster *and* more accurate than a vision model, because the characters are already there and nothing is inferred. |
| `vision` | renders each page and asks a vision model to fill the same schema. The real prompt is in [`backends.py`](deedpipe/backends.py) — deliberately not wired up here, because a demo that needs the reader's API key and bills per page is a demo most people never run. |

The seam is the point. The schema, the confidence rules, the routing and the loader are all
backend-agnostic, so a vision prompt can be tuned against a scored corpus without
accidentally changing how records are routed.

It also makes the economics tractable. At roughly 1–2¢ a page, 500 documents a day is
$25–100/day to a vendor. Running the text backend first and calling vision only on what it
could not read is not an optimisation — it is the difference between a line item and a
problem. And where a "scanned" PDF already carries an OCR text layer underneath, agreement
between two independent extractors is a far better confidence signal than a model's own
opinion of itself. A model asked how sure it is will tell you what you want to hear.

## Three county layouts, on purpose

`tools/make_deeds.py` generates the corpus with no PDF toolkit beyond reportlab, from a
seeded RNG, and writes the ground truth beside each file because it knows what it drew.

| layout | shape | result |
|---|---|---|
| `marion` | label on one line, value on the next | posts |
| `calloway` | boxed recording stamp, parties named in an indenture sentence | posts |
| `deshler` | no labels at all, fixed field order | **queues on grantor** |

"Do all your recorders format the same way?" is the question that decides the whole build,
which is why the corpus answers it in three different ways. A pipeline that only handles
`marion` looks finished and is not.

## Two defects the scorer caught

Both produced a clean run and output that looked right.

**Legal descriptions were silently truncated.** The label-and-next-line strategy took only
the first line of a value that wraps, cutting *"…Township 11 North, Range 17 West, according
to the recorded plat thereof"* down to *"…Township 11 North"* — which is not a parse error,
it is a legal description of a different piece of land, posted unattended at 95% confidence.

**A prose capture swallowed half a sentence.** An unbounded lazy match starting at the first
"and" in *"made and entered into"* happily ran to *"party of the second part"*, producing a
130-character "grantee" name. It was caught by a length check, which meant the field came
back empty and the document queued — the right outcome by accident, for the wrong reason,
and invisible until the per-field score showed grantee at 0/4.

Neither is findable by running the pipeline and looking at the output. Both took ground
truth and a per-field number.

## What this is not

- **Not an OCR engine.** The `text` backend needs a text layer. Genuinely scanned paper
  needs the vision backend or a dedicated OCR step in front.
- **Not tuned to your recorders.** Three synthetic layouts are a test of the *mechanism*,
  not a promise about your counties. The first thing any real engagement needs is a small
  hand-keyed ground-truth set from the actual documents.
- **Not a production ODBC loader.** SQLite here; the write path is deliberately thin so
  swapping in pyodbc or SQLAlchemy touches one file.
- **Not a review UI.** The queue is a table and a folder. Putting a screen on it is the
  next thing, not a thing that exists.

## License

MIT.

---

Built by [datumstake](https://github.com/datumstake). Related:
[pdftext](https://github.com/datumstake/pdftext) — PDF text extraction in a single
dependency-free C++ header.
