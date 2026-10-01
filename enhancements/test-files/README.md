# Test files: File → Bronze → Silver

Ten workbooks that exercise every scenario in the two AHI documents
(`../AHI-File-Bronze-Scenario-Doc.docx`, `../AHI-Bronze-Silver-Scenario-Doc.docx`).

Regenerate them with `python backend/tools/make_silver_test_files.py`. The output is deterministic, so the expected outcomes below never drift. The end-to-end test `backend/tests/test_silver_db.py` runs all ten in order against Postgres and checks each outcome.

**Before you start:** load the LOTL seed so the profit-center rules have something to look up:

```bash
python backend/tools/seed_lotl.py enhancements/test-files/lotl_seed.csv
```

**Ingest them in number order.** Each one is: Configure → Run → Ingest, then Silver.

| # | File | Scenario | Bronze (Ingest) | Silver |
|---|---|---|---|---|
| 1 | `01_ARR_pc0101_2026_JanJun.xlsx` | Base load: one sheet, Jan–Jun, clean headers | CREATE `ext_pc0101_arr` | Every column matches exactly. Approving saves 9 mapping rows. Nothing is saved before approval. |
| 2 | `02_ARR_pc0101_2026_Jul.xlsx` | July, identical columns | APPEND | The whole mapping is pre-filled (Saved), and still needs approval. |
| 3 | `03_ARR_pc0101_2026_Aug_reordered.xlsx` | Same columns, different order | REORDER | Saved mapping reused. |
| 4 | `04_ARR_pc0101_2026_Sep_newcols.xlsx` | `Premium Amt`, `Carrier`, `Writing Agency` renamed; `Notes` added | Columns both added and missing, so NEW_TABLE `ext_pc0101_arr_2026_09` (confirm) | `premium_amt` gets a fuzzy vote (and word2vec's, when configured). `carrier` and `writing_agency` get word2vec and AI votes when those are configured, or are chosen by hand. `notes` set to Ignore. Silver unifies the table back into `detail`. |
| 5 | `05_Prem_pc0202_2026.xlsx` | Second source; month sheets Jan–Jun; its own wording (`PC Name`, `Acct Eff Date`, `Policy #` …) | CREATE `ext_pc0202_data` | `pc_name`, `acct_eff_date` … reuse approved names; `written_premium` fuzzy; `source_sheet` set to Ignore. |
| 6 | `06_PC_pc0303_2026_cases.xlsx` | The four profit-center cases, plus a name the LOTL doesn't know | CREATE | `pc_lookup_status`: kept ×2, filled_number, corrected (`515` → `0094`), filled_name (by pc_id `0303` → Columbus Office), no_match (`0777`). |
| 7 | `07_Dates_pc0404_2026.xlsx` | Every date format in the document, an Excel serial (46030), two invalid dates | CREATE | Dates parsed; the 2 invalid ones become NULL and are counted. |
| 8 | `08_Money_pc0505_2026.xlsx` | `1200.50`, `1,200.50`, `$1,200.50`, `(250.00)`, `abc`; padded and blank producers | CREATE | Decimals parsed, `(250.00)` becomes −250.00, `abc` becomes NULL; strings trimmed; blanks become NULL. |
| 9 | `09_ARR_pc0101_2026_JanJun_revised.xlsx` | Revised Jan–Jun for pc0101 | REPLACE file 1 (confirm) | The next run lists file 1 under "removed"; its 30 Silver rows are deleted and the summary is rebuilt. |
| 10 | `10_Report_pc0606_2026_JanJul.xlsx` | Dirty report: titles, monthly subtotals, blank rows, a hidden sheet, a producer lookup table beside the data | Main table becomes `ext_pc0606_report` (21 rows); the lookup becomes `ext_pc0606_report_2026_01` | Select the main table's load. Leave the lookup table's load unselected, or set its columns to Ignore. |

**Seed data:** `lotl_seed.csv` holds the LOTL rows (`pc_id, legacy_office_name, profit_center_number`) for every office used above. It's test data only; the real LOTL replaces it.
