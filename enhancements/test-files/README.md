# Test files: File → Bronze → Silver

Fourteen workbooks (files 1–14; 14 is a pair) that exercise every scenario in the two AHI documents
(`../AHI-File-Bronze-Scenario-Doc.docx`, `../AHI-Bronze-Silver-Scenario-Doc.docx`).

Regenerate them with `python backend/tools/make_silver_test_files.py`. The output is deterministic, so the expected outcomes below never drift. The end-to-end test `backend/tests/test_silver_db.py` runs them in order against Postgres and checks each outcome.

**Before you start:**

1. **Reference tables:** the business's division mapping, LOTL and DRT column mapping (`assets/`) are loaded into the database automatically the first time it is used.
2. **Test offices:** the offices these files use (Dayton, Toledo, Columbus …) are not in the business's LOTL, so add them on top:

   ```bash
   python backend/tools/seed_reference.py --add-lotl enhancements/test-files/lotl_seed.csv
   ```

3. **In Ingest:**
   - **pc_id** comes from the `pc0101`-style suffix (`PC0101`).
   - **File date:** files 2, 3 and 4 name a month (`Jul`, `Aug`, `Sep`). Files 1, 9 and 10 name a month range (`JanJun`, `JanJul`), and its last month is used. The rest name only a year, so enter their file date in the Files step.
   - **Division:** the test profit centers are not in `division_mapping` except PC0606 (Bridge Specialty Group), so their division is left empty.

**Ingest them in number order.** Each one is: Configure → Run → Ingest, then Silver.

| # | File | Scenario | Bronze (Ingest) | Silver |
|---|---|---|---|---|
| 1 | `01_ARR_pc0101_2026_JanJun.xlsx` | Base load: one sheet, Jan–Jun, clean headers | CREATE `ext_pc0101_arr` | Every column gets a recommendation: exact, plus the DRT mapping's saved vote for familiar headers (`Producer Name` → `producer_agency_name`). Approving saves 9 rows for PC0101 in `drt_column_mapping`, under the headers as written (`Premium`, `Producer Name` …), with a DRT label only where the business has one (`Premium`; none for `Profit Center Name`); there are none before approval. The rows go through `ahi_bronze_cleansed.ext_pc0101_arr` into `silver_detail`. |
| 2 | `02_ARR_pc0101_2026_Jul.xlsx` | July, identical columns | APPEND | The whole mapping is pre-filled (Saved), and still needs approval. |
| 3 | `03_ARR_pc0101_2026_Aug_reordered.xlsx` | Same columns, different order | REORDER | Saved mapping reused. |
| 4 | `04_ARR_pc0101_2026_Sep_newcols.xlsx` | `Premium Amt`, `Carrier`, `Writing Agency` renamed; `Notes` added | Columns both added and missing, so NEW_TABLE `ext_pc0101_arr_v2` (confirm) | `premium_amt` gets a fuzzy vote (and word2vec's, when configured). `carrier` gets the DRT mapping's saved vote (`Carrier` → insurance company, as most profit centers approved it). `writing_agency` gets word2vec and AI votes when those are configured, or is chosen by hand. `notes` set to Ignore (not saved: it is asked about again next time). Silver unifies the table back into `silver_detail`. |
| 5 | `05_Prem_pc0202_2026.xlsx` | Second source; month sheets Jan–Jun; its own wording (`PC Name`, `Acct Eff Date`, `Policy #` …) | CREATE `ext_pc0202_data` | `pc_name`, `acct_eff_date` … reuse approved names; `written_premium` fuzzy plus a DRT saved vote; `carrier_name`, `agency`, `policy` get DRT saved votes; `source_sheet` set to Ignore. |
| 6 | `06_PC_pc0303_2026_cases.xlsx` | The four profit-center cases, plus a name the LOTL doesn't know | CREATE | The quality report's profit-center counts: kept ×2, filled_number, corrected (`515` → `0094`), filled_name (by pc_id `PC0303` → Columbus Office), no_match (`0777`). |
| 7 | `07_Dates_pc0404_2026.xlsx` | Every date format in the document, an Excel serial (46030), two invalid dates | CREATE | Dates parsed; the 2 invalid ones become NULL and are counted. |
| 8 | `08_Money_pc0505_2026.xlsx` | `1200.50`, `1,200.50`, `$1,200.50`, `(250.00)`, `abc`; padded and blank producers | CREATE | Decimals parsed, `(250.00)` becomes −250.00, `abc` becomes NULL; strings trimmed; blanks become NULL. |
| 9 | `09_ARR_pc0101_2026_JanJun_revised.xlsx` | Revised Jan–Jun for pc0101 | REPLACE file 1 (confirm) | The next run lists file 1 under "removed"; its 30 Silver rows (found by table, file and processing date) are deleted and the aggregate is rebuilt. |
| 10 | `10_Report_pc0606_2026_JanJul.xlsx` | Dirty report: titles, monthly subtotals, blank rows, a hidden sheet, a producer lookup table beside the data | Main table becomes `ext_pc0606_report` (21 rows); the lookup, a second table on the same sheet, becomes `ext_pc0606_report_v2` (both load) | Select the main table's load. Leave the lookup table's load unselected, or set its columns to Ignore. |
| 11 | `11_ARR_pc0101_2026_MayJul_overlap.xlsx` | May–Jul, after the revised Jan–Jun and July are loaded | Blocked: May–Jul overlaps Jan–Jun only partly (Replace would delete Jan–Apr, Append would load May–Jun twice). The reviewer must choose an action. | — |
| 12 | `12_ARR_pc0101_2026_Oct_reorder_newcol.xlsx` | October: columns reversed *and* a new `Commission %` | EVOLVE `ext_pc0101_arr` (confirm): reordered to the table's order, `commission` added | Not needed for the scenario. |
| 13 | `13_MoneyDates_pc0909_2026.xlsx` | Amounts outside the document's list: `$(250.00)`, `1.005`, `0.125`, `1 200,50`, `USD 100`, `9999999999999999.99` | CREATE `ext_pc0909_edge` | Premiums −250.00, 1.01, 0.13, NULL, NULL and 9999999999999999.99 (exact, half up); the two unreadable ones are named in the cleansed table's `_invalid_columns`. |
| 14 | `14_Clash_pc0707_2026_Jan.xlsx`, then `14_Clash_pc0707_2026_Feb_swapped.xlsx` | `Amount ($)` and `Amount (%)` both clean to `amount` (`amount`, `amount_2`); February lists them the other way round | CREATE `ext_pc0707_clash`, then REORDER: matched by the headers as written, so dollars stay in `amount` | — |

**Test LOTL rows:** `lotl_seed.csv` holds (Lima and Mansfield for files 13–14 included), in the LOTL's own shape (`profit_center_number, legacy_office_name, status`), the offices used above. They are test data, added on top of the business's LOTL.
