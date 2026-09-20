# How the Cleaner Reads a Spreadsheet — Business Reference

A plain-language explanation of how the tool decides what it is looking at: where the
column names are, which rows are not data, whether a sheet has been turned on its side,
and how sheets in the same file relate to each other.

Written for reviewers, analysts and anyone who has to explain or defend the output. No
code, no file paths.

---

## The one idea behind everything

**The tool is not given a list of expected column names.** It has never been told that a
file should contain "Premium" or "Policy Number", and it does not look for them.

That is deliberate. Real files from real sources never agree on names. One month a column
is `Producer/AgencyName`, the next it is `Producer`, and a different broker calls it
`Agent`. A tool built around a fixed list of names needs a person to update that list
every time a new source appears — which means it never really finishes.

So instead, the tool works the way an experienced person does when handed an unfamiliar
spreadsheet. You don't read a spreadsheet by recognising the words. You read it by
noticing **patterns**:

- the row of column names *looks different* from the rows underneath it
- a total row is *mostly empty* and its number *adds up*
- a footer is a *lone sentence* sitting under a block of figures
- a table that has been turned sideways *reads consistently* down the page instead of
  across it

Every decision below is one of those observations, made explicit. And every one is
recorded, with its reasoning, in an audit file you can read afterwards.

---

## 1. How does it find the header row?

### What the question really is

A header row is not "the first row". In real exports it is usually buried under a company
name, a logo, a "Report generated on…" line, a confidentiality notice, and a couple of
blank rows. In one of our test files the real header sits on **row 300**.

### How it decides

The tool scores each of the first rows on five things. None of them require knowing what
the columns are called.

| What it looks at | The reasoning |
|---|---|
| **Does this row look unlike the rows below it?** | This is the strongest clue. A header holds words sitting on top of columns full of numbers, dates and reference codes. A data row looks like every other data row. |
| **Is it made of words?** | Column names are text even when the column beneath them is money or dates. |
| **Are the entries all different?** | Column names never repeat. Data repeats constantly — the same branch, the same broker, over and over. |
| **Is the row as full as the rows below?** | A header fills roughly the same number of cells as a record. A title fills one. |
| **Are the entries short?** | Column names are labels, not sentences. |
| **Is it styled differently?** | Bold, shading or a bottom border, where the data rows have none. A bonus only — plenty of files have no formatting at all, and those still work. |

The row with the best overall score wins.

### Two obvious-sounding rules that are wrong

- *"The header is the first row with something in it."* This picks up the company name
  banner instead.
- *"The header is the first row with no gaps."* Headers routinely have a blank cell where
  someone forgot to label a column.

### When it refuses to answer

If no row scores well enough, the tool **does not pick one anyway.** It reports that the
table has no header and names the columns by position (`column_1`, `column_2`…).

This matters commercially. Promoting a genuine data row to be the header silently destroys
a record *and* mislabels every column beneath it. Saying "I could not find a header here"
is a far cheaper outcome — someone spends two minutes looking instead of discovering a
discrepancy three months later.

### Headers that aren't in one piece

- **Split over two rows** — `Profit Center` above `Name`. If the second row also reads like
  labels *and* looks unlike the data below it, the two are joined into `Profit Center Name`.
  The second condition matters: without it, the first genuine record of every text-heavy
  file gets eaten.
- **A category band on top** — a merged `POLICY INFORMATION` stretched across several
  columns. Recognised as decoration because it is one value repeated across the row.
- **Blank or duplicated labels** — a blank label keeps its column and gets a positional
  name; the column is never dropped just because nobody named it. Duplicates are numbered.

### When the data doesn't line up with the header

Some exports put a decorative gap in the *data* rows but not in the header. The header
occupies columns 1–8; every record occupies 1, 2, 3, 5, 6, 7, 9, 10.

Read naively, everything after the first gap lands one column to the left — the insurer's
name arrives in the Premium column, and **nothing announces the error**. This is the most
dangerous failure available, because the file still looks perfectly reasonable.

The tool checks whether the header and the data agree on which columns they use. When they
disagree but hold the **same number of values**, the mismatch is only spacing, so the data
is re-seated under the correct labels and the audit records both layouts. When the counts
differ, nothing is moved — the shapes genuinely differ, and guessing would be worse.

---

## 2. How does it identify footers, totals and other non-data rows?

### The principle

A record is a full row. Everything that isn't a record is, in some way, **thinner** than
the rows around it.

The tool works out the "normal" fullness of a table — how many cells a typical row fills —
and treats anything noticeably below that as a candidate for removal. Not removed yet:
*a candidate*.

### What it finds, and how

| Row type | How it is recognised |
|---|---|
| **Title / banner** | Sits above the header, or is a single cell of text, or one value stretched across the whole row. |
| **Footer** | A lone cell of text with no figures — `*** End of Report ***`, `Page 2 of 3`, `Confidential`, `Prepared by Finance`. |
| **Subtotal / Grand Total** | Thin, has a figure, **and that figure equals the sum of the rows above it.** |
| **Repeated header** | The column names appearing again part-way down, from a page break. |

### The arithmetic test is the important one

A subtotal is not identified because it says "Subtotal". It is identified because **the
number adds up**.

This has two commercial advantages:

1. **It works in any language.** A German report's `Zwischensumme` is removed on exactly
   the same evidence as an English `Subtotal`, with no word list to maintain.
2. **It cannot be fooled by a name.** A profit centre legitimately called `Total Risk PC`
   keeps its row — it is a full record and its figures don't sum to anything.

The tool also checks totals in the right order: individual section subtotals first, then
the grand total tested against both the remaining records *and* the subtotals already
found. Without that ordering, a grand total gets missed because it double-counts rows a
subtotal already covered.

### Repeated headers, in three disguises

Page-break headers don't always reappear identically:

1. **Word for word** — the simple case.
2. **A fragment** — `Policy Summary | | | | Premium | Policy Number`. Recognised because
   its entries match the real header's labels in the same positions.
3. **Reworded** — a second section using `PC Number` where the first used
   `ProfitCenterNumber`. No label matches, so it is caught the same way the original header
   was: it is **words sitting where the column holds numbers and dates**.

### Two safety rules

**A thin row that doesn't add up, and doesn't call itself a total, is kept.** Records with
several empty fields are ordinary in real data. Losing one silently is the worst thing this
tool could do, so the benefit of the doubt always goes to keeping the row — flagged, at
reduced confidence, so a reviewer can look.

**Wording on its own can never remove a row.** A row must be structurally thin *first*. The
words only decide which kind of non-record it is.

### The one judgement call we made deliberately

Real reports contain totals whose figures are simply **wrong** — stale, hand-edited, or
rounded by someone. Our test files include a `TOTAL West Zone PC` of `48,387.01` where the
rows above sum to `48,383.11`, and a `Grand Total` of `999,999.99`.

Keeping those as data corrupts every downstream sum. So a row that is *both* thin *and*
labelled a total is removed — but into its **own separate category**, flagged for review,
and never mixed in with totals the arithmetic actually proved.

This is a trade, and we state it plainly: it is the one place where wording carries weight.
It buys correct figures on files that would otherwise be quietly wrong.

---

## 3. How does it know a table has been turned sideways?

### What this looks like

Occasionally a sheet is built the wrong way round — the field names run **down** the first
column, and each **record runs across** the page:

```
Profit Centre     | South Zone PC | West Zone PC | East Zone PC
Producer          | Pinnacle      | Apex         | Metro
Premium           | 15,109.20     | 35,586.47    | 37,066.89
Policy Number     | POL-200001    | POL-200002   | POL-200003
```

Loaded as-is, this is unusable — every customer becomes a column.

### How it decides

Two observations, both about consistency:

**Consistency down the page.** In a normal table, each column holds one kind of thing all
the way down — Premium is money in every row, Policy Number is a reference in every row. A
single *row*, by contrast, is a mixture: some text, a number, a date, a code. In a sideways
table this is exactly reversed.

So the tool asks: *which direction is consistent?* That direction is the field direction.

**Repetition.** Field names never repeat. Data repeats constantly. In the example above,
the first row contains `South Zone PC` more than once, while the first column — the real
field names — has no repeats at all. That contrast is decisive.

### Doing it in the right order

The tool decides orientation for the **whole sheet before** it starts breaking the sheet
into tables. This matters: in a sideways sheet, a blank row is a blank *field*, not a
break between tables. Deciding orientation afterwards means the table has already been cut
in half before anyone noticed it was sideways.

Titles and banners are also removed **before** this judgement. A merged title stretched
across every column is perfectly "consistent" across the row, and left in place it drags
the whole sheet towards looking sideways.

### When it genuinely can't tell

A small lookup table — four columns, five rows — is nearly square and reads plausibly
either way. There is no clever answer here; both readings are defensible from the content
alone.

The tool falls back on the plainest fact available: **tables are far more often taller than
they are wide.** It takes the reading that yields more records than fields, and marks the
result as *decided by shape* so a reviewer can see the judgement was close.

We tried a cleverer tiebreaker — asking which reading produced a better-looking header —
and **measured it as worse.** On a small lookup, the column of company names scores as well
as the real header, and the table gets stood on its side. We reverted it and kept the
simple rule.

### A special case: the pivot report

Some reports aren't sideways, they are **pivoted** — a grid with one measure spread across
the page:

```
Producer                 | Jan-2026  | Feb-2026  | Mar-2026
Pinnacle Agency Partners | 12,299.66 | 35,117.88 | 32,440.79
Apex Insurance Brokers   | 28,850.98 | 33,239.15 | 11,003.51
```

The month columns are **not fields** — they are values of one field (the month), laid
sideways. Loaded as-is you get a column per month, which no table can use, and next month
you get a different set of columns.

These are recognised by three things together: a run of column headings that are all the
same kind of thing and all different from each other; a grid beneath them containing one
consistent measure; and remaining columns that identify each row uniquely. Any ordinary
table fails at least one of those.

The grid is then **unfolded** into the shape a table can actually take — one row per cell:

| Producer | Period | Value |
|---|---|---|
| Pinnacle Agency Partners | Jan-2026 | 12,299.66 |
| Apex Insurance Brokers | Jan-2026 | 28,850.98 |
| … | … | … |

A pivot is also genuinely ambiguous about orientation — it reads consistently in *both*
directions — so it is recognised before the sideways check runs, which would otherwise flip
a perfectly readable grid.

---

## 4. Multiple sheets or tables — which ones need joining?

### The question

A file may contain a sheet of transactions and a separate sheet of reference details —
producer addresses, states, postcodes. Those need to be **joined**: the reference details
attached to each transaction.

### Step one: what kind of table is each one?

| Kind | How it is recognised | Typical example |
|---|---|---|
| **Transaction table** | Contains genuine amounts — figures that vary, repeat or go negative — alongside a reference number or a date. | Policy premiums by producer |
| **Reference table** | Small. Every row a distinct entity. Descriptive details only, no amounts. | Producer → address, state, postcode |

Two things it deliberately **ignores**:

- **The sheet's name.** A tab called "Producer Lookup" that turns out to contain
  transactions is treated as transactions. The label is recorded as a hint and nothing more.
- **Where it sits.** A reference table placed *above* the transactions is still a reference
  table.

A note on telling amounts from codes: both are numbers and both can be all-distinct — a
premium is as unique as a postcode. What separates them is that amounts have decimals, or
repeat, or go negative. A postcode does none of those. **A negative value settles it
outright**, because reference numbers and postcodes are never negative.

### Step two: finding what to join on

This is where not having a fixed list of names genuinely pays off.

The transaction sheet calls the column `Producer/AgencyName`. The reference sheet calls it
`Producer`. **Those names do not match** — any approach based on matching column names
fails here.

So the tool ignores the names entirely and **compares the contents**. It looks for the pair
of columns, one from each table, whose *values* overlap most. `Pinnacle Agency Partners`
appearing in both is far stronger evidence than any similarity between the two headings.

### Step three: matching values that don't quite agree

Real data is untidy. The transaction sheet says `MJC ` with a trailing space; the reference
sheet says `MJC Agency Group`.

Close matches are scored, and a match is accepted automatically only when it is both
**strong** and **clearly better than the runner-up**.

That second condition is not a technicality — it is the difference between a join you can
trust and one you can't:

| Value being matched | Best match | Runner-up | Outcome |
|---|---|---|---|
| `MJC ` | MJC Agency Group — very strong | Metro Agency Group — weak | **Accepted.** Clear winner. |
| `Agency Group` | Metro Agency Group — very strong | MJC Agency Group — **equally strong** | **Rejected, flagged.** Two equally good answers means there is no answer. |
| `Brown & Brown` | nothing close | — | **Rejected, flagged.** Not in the reference data. |

Without the runner-up test, the middle row would have been matched to whichever candidate
happened to be checked first — a silent, invisible, wrong answer.

### Step four: joining safely

**A transaction row is never dropped because its reference lookup failed.** Every
transaction survives; unmatched ones simply come through with the reference fields empty,
and the unmatched value is listed in the audit for someone to resolve.

The reference table is also written out separately, because it is useful in its own right.

---

## 5. Which sheets get stacked together?

### The question

A file may hold the same report split across sheets — `Jan` and `Feb`, or a second sheet
that simply continues where the first ran out of rows. These need to be **stacked**: laid
end to end into one table.

### How it decides

Two tables are stacked when they are the same shape carrying the same kind of content:

- **Same column names**, or
- **Same number of columns with the same kinds of content in the same order** — the fallback
  for when two months were exported with slightly different headings. When it is this
  second route that decides, the result is flagged as lower confidence, because the names
  did not agree.

### The provenance column — why it exists

When sheets are stacked, a **Source Sheet** column is added, recording which sheet each row
came from.

This is not decoration. In our test data, January and February contain the **same policy
numbers** — the same policies, reported in two periods. Stack them without recording the
source and:

- the rows become indistinguishable
- they look like duplicates
- any de-duplication step silently destroys half the dataset

Nothing in the row data itself says which period it belongs to. The sheet name is the only
record of it, so it is captured before it is lost.

For the same reason, **duplicate checks are run within each sheet, not across the stack.**
Checking across would flag every row in a legitimate two-period file.

### Continuation sheets with no header at all

A common pattern: the first sheet has headers and data, and a second sheet named
`Continued` holds the remaining rows with **no header whatsoever** — every row is data.

Looked at on its own, that sheet honestly has no header, and the tool says so rather than
promoting a record. But at **file level** the answer is visible: another sheet in the same
file has the same number of columns, holding the same kinds of content, and it *does* have
a header.

So the continuation sheet **adopts** those column names. The match is made on shape and
content, never on sheet order — so a continuation is recognised whether it comes before or
after the sheet it belongs to. The adoption is recorded in the audit, naming the sheet the
names came from.

---

## What happens when the tool isn't sure

This is the part worth understanding commercially, because it determines how much review
effort the output needs.

The tool distinguishes between **being wrong** and **being uncertain**, and it never
disguises the second as the first.

| Situation | What it does |
|---|---|
| No header can be found | Names the columns by position and says so |
| Orientation is a close call | Uses the fallback rule and marks it "decided by shape" |
| A thin row doesn't add up | Keeps the row, at reduced confidence |
| A close match has two equally good candidates | Refuses to match, lists the value for review |
| A reference lookup finds nothing | Keeps the transaction, leaves the reference fields empty, lists the value |
| A column is entirely empty | Says whether the source was blank or the values are formulas Excel never saved |

Every removed row is recorded with **the evidence**, not a label. The audit says
*"this figure equals the sum of the three rows above it"* — not *"matched subtotal
pattern"*. A reviewer can check the claim.

**Where to look first.** Every file's audit carries three headline counts:

- tables where no header could be found
- orientations that were a close call
- lookup values that need a human decision

If all three are zero, the file went through cleanly.

---

## CSV files

A `.csv` or `.tsv` goes through exactly the same reasoning. A delimited file is a single
sheet by definition — there is no second tab to look for — and once its cells are read it
is indistinguishable from a worksheet as far as every rule above is concerned.

Two things are worked out rather than assumed: **which character separates the fields**
(comma, semicolon, tab or pipe — European exports commonly use semicolons because the
comma is their decimal separator) and **which text encoding the file uses**, so an
accented name does not stop the file opening.

A CSV cannot carry cell formatting or formulas, so those clues are simply unavailable —
the tool copes, because none of them were ever the deciding vote. One thing a CSV does
better than Excel: an identifier like `08085` keeps its leading zero, which Excel would
have thrown away before the tool ever saw the file.

## What happens when a file cannot be read at all

A folder of five hundred files will contain a few that are not really spreadsheets: a
download that was cut short, a workbook someone password-protected, an old `.xls`, a file
saved in the wrong format.

**A bad file costs you that file and nothing else.** Each workbook is handled on its own,
so one unreadable file no longer stops the run and skip everything after it. The report
names the file and says which of these it was, because each calls for a different response:

| What the report says | What to do about it |
|---|---|
| corrupt | ask for the file again; the copy you have is incomplete |
| password-protected | it must be opened and re-saved without the password |
| unsupported format | re-save it as `.xlsx` |
| too large | it exceeds the configured ceiling and was refused rather than attempted |

Telling these apart matters. A password-protected file and a truly corrupt one produce the
*same* error from the underlying reader, and reporting "corrupt" for a file that is merely
locked sends someone looking in entirely the wrong place.

## Refusing to look trustworthy when it is wrong

The checks described above decide what the data *is*. A separate set of checks asks whether
the result can be believed at all, and these do not merely report — they **fail the run**:

- every row is accounted for, either kept or removed with a stated reason
- no identifying column came out empty
- no column emptied without a cause being named
- a total that was removed still agrees with the rows that were kept

The reasoning is that the dangerous failure of a cleaning tool is not crashing. It is
producing a file that looks entirely reasonable and is quietly wrong, which loads into a
table without complaint and is discovered months later, if ever.

## Speed, and doing several files at once

One million rows in a single sheet takes **about a minute and a half**. Several workbooks
can be processed at the same time with `--workers`.

Both numbers come from measurement rather than expectation, and one of them contradicted
what we predicted: we expected that using separate processes would be faster than using
threads, and measured the opposite by a wide margin. The default follows the measurement.

## What this tool does not do

Stated plainly, because it affects what has to happen next.

**It does not map columns onto your target table.** It will faithfully clean a file whose
columns are called `Producer`, and another whose columns are called `Agent Name`. It will
**not** decide that those two are the same field in your database.

That decision cannot be derived from the files. It requires one of: a defined target
schema, a mapping configuration, an AI suggestion reviewed by a person, or a person. There
is no fifth option, and anything claiming otherwise is guessing.

What the tool gives you is per-file-correct structure with every column's content
identified — which is the right input to that mapping step, and makes it a one-off task per
source rather than a recurring one per file.

**It cannot recover what Excel already destroyed.** A postcode saved as the number `8085`
lost its leading zero before the file was ever written. Where the zero survives in the
file, it is preserved.

**Near-square tables are a genuine judgement call.** A four-column, five-row lookup reads
plausibly either way. The tool picks the more likely reading and flags it. It does not
pretend to certainty it doesn't have.

---

## How we know it works

**The files check themselves.** Every genuine record in the test set carries a policy
number; banners, totals, footers and repeated headers do not. Counting those in the raw
file gives an expected record count that owes nothing to the tool being tested. For pivot
reports, which have no policy numbers, the check is that unfolding the grid produces
exactly one row per value — nothing lost, nothing invented.

**The discarded rows verify the kept ones.** Totals are removed *and* retained in the audit,
so their stated figures can be checked against the sum of the cleaned rows. When a report's
own grand total agrees with our cleaned output to the penny, that is independent
confirmation — no hand-built expected answer required.

**Every rule is tested by switching it off.** Each piece of reasoning is disabled in turn
and the whole test set re-scored, which shows exactly which files depend on it. This stops
any assumption quietly becoming load-bearing without anyone noticing.

That check has caught our own measurement twice — cases where switching a rule off appeared
harmless because the scoring couldn't see the damage. Both gaps are now closed with direct
checks. A test that cannot fail is not measuring anything.
