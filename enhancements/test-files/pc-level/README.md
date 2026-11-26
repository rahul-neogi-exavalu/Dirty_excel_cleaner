# Profit-center-level test files

Ten workbooks, one per requirement, from `python backend/tools/make_pc_level_test_files.py`
(deterministic). Column names are deliberately inconsistent: inside a file the headers mix
styles (`Acct_Eff_Date`, `comm pct`, `MARKET PROVIDER`, `Policy #`), and each profit center
writes the same field its own way. Expect to map some required columns yourself on
Validate (the AI and the saved mapping cover more of them when they are on).

Run them in number order; 03–06 are one profit center's series and depend on each other.

| # | File | What it tests | What should happen |
|---|---|---|---|
| 01 | `01_PC0796_DatePick_…` | Date priority AED → TED → PED; the reviewer's date column | AED (every row, Feb–Jun) decides and is **flagged**. Its TED row shows **Year to date**: pick it → Jan–Jun YTD, `date_detail` **TED**. Typing Jan–May instead → `date_detail` blank. |
| 02 | `02_PC0843_Transactions_with_Producer_Codes_…` | A sheet that is not data | *Producer Codes* (no required columns) is rejected **on its own**; *Transactions* stays ready and is staged. |
| 03 | `03_PC0515_ARR Jan-Jun 2026_…` | The year-to-date file | INSERT, YTD → Ingest creates `ext_pc0515_arr`. |
| 04 | `04_PC0515 July 2026 production_…` | A month after the YTD file, other name and sheet, one column more | APPEND; Against Bronze shows **Columns added or missing: endorsement_notes**. Ingest **evolves `ext_pc0515_arr`** (not a new `ext_pc0515_data`). |
| 05 | `05_PC0515_ARR June resend_…` | A month already loaded | **Flagged**, control table **REJECTED** (“Jun 2026 is already loaded from 03…”). |
| 06 | `06_PC0515_ARR Jan-Jun 2026 revised_…` | A revision | Same dates as 03: **Your call** (revise / companion / reject). Revise, naming `03_PC0515_ARR Jan-Jun 2026_07132026.xlsx` exactly (a wrong name is refused) → Ingest **replaces** 03's rows; July stays. |
| 07 + 08 | `07_PC0901_Transactional_…`, `08_PC0901_BrokerList_…` | A companion list joined in Silver | Validate both together. 07 misses MarketProvider; mark 08 **Companion of** 07 → both ready (checked together). In Silver select 07: 08 comes along; the join suggests **writing_producer = broker_account** (Shared values). Joining on that alone is **blocked** (Lockton listed twice); add **region = territory** → every row matches; MarketProvider comes from the list. |
| 09 | `09_PC2030_Waypoint Premium Summary …` | A summary the profit center aggregated (spaced crosstab like the real PC2030 file) | One table, **Aggregated figures**, dated by its sheets (Jan–Jun YTD, February empty). Staged as `ext_pc2030_data_agg_stg`, loaded as `ext_pc2030_data_agg`. In Silver: TOTALS left out, Casualty / Property / Workers Comp spread as **premium** (pick `product_line_name`) → `silver_aggregate`, `AS_REPORTED`. |
| | | **Known issue** | March's first partner row is all zeros, and the cleaner currently reads it as a second header row: March comes out as its own table with headers like `casualty_treaty_0`. Jan, Apr, May and Jun stack correctly into one table. Untick March on Run to test the rest. |
| 10 | `10_PC0333_Producers and Endorsements_…` | A data sheet rejecting the whole file | *Endorsements* runs Mar–Jun → **flagged**; *Producers* (fit) is **rejected with its file**. Correcting Endorsements' dates makes both ready. |
