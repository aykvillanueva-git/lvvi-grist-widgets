---
name: "lvvi-collectibles"
description: "Generate a firm-wide LVVI collectibles list (retainer arrears / AR snapshot) for Pozorrubio and Dagupan; also handles overcollected clients and prior-year carryover balances."
---

# LVVI Collectibles List (Firm-wide AR Snapshot)

## Why this exists, and how it differs from `lvvi-billing`

`lvvi-billing`/`client_lookup.py` answer "what does **this one client** owe, **this quarter**" — scoped to a single fee_key, meant to feed an actual bill. This skill answers "who on the **entire roster** owes money **right now**" — a portfolio-wide, YTD-scoped snapshot meant to feed a collections conversation with Ayk, not to produce a client-facing document. Different question, different output shape: one row per client, both offices, sorted by amount owed, no letterhead, no per-client TIN/VAT/WT lookups.

Run `scripts/build_collectibles.py` rather than re-deriving this by hand — it implements everything below.

## Source files (always highest version / freshest upload — never hardcode)

| Purpose | File | Notes |
|---|---|---|
| Collections ledger (primary driver) | `lvviDailyReports2026_v{N}.xlsx` sheets `dColln` (Dagupan), `pCollns` (Pozorrubio) | **If Ayk has just uploaded a YTD/daily-reports workbook in this conversation, that upload supersedes whatever version sits in `/mnt/project`, even if the project's version number is nominally higher** — compare actual data recency (e.g. which months have non-zero collections) if the naming convention differs, don't just trust the number in the filename. Pass its path via `--daily-reports`. |
| Fee schedule (authoritative fee amounts) | `client_fees_v{N}.xlsx`, sheets `poz`/`dag` | **Exclusive source of truth for Monthly/Qtr/Annual fee — never trust `dColln`/`pCollns`' own embedded FEE column when a client_fees entry exists.** That ledger FEE column goes stale (e.g. it still showed Ramos Money Changer at the old ₱11,500 figure weeks after the fee was corrected to ₱5,000 in `client_fees`). Only fall back to the ledger's own FEE column when the client genuinely isn't in `client_fees` at all — and flag that fallback in the Notes column as unconfirmed. |

Discover both dynamically (highest `_v{N}` in `/mnt/project`) exactly like `client_lookup.py` does — a newer version can appear mid-session without anyone announcing it.

## Sheet layout gotchas (both are already encoded in the script — reference only)

- Row 1 = section headers, row 2 = column headers, **row 3 is a workbook-wide TOTALS row (not a client)** — real client data starts at row 4 in both `dColln` and `pCollns`. Skipping this wrong (starting at row 3) silently treats the firm's own grand total as if it were one giant client.
- `dColln` (Dagupan): name col 2, monthly FEE col 6, month columns start col 7 (jan..dec), Qtr Fee col 22, Annual Fee col 23, qtr/annual monthly block starts col 24.
- `pCollns` (Pozorrubio): name col 2, monthly FEE col 3, month columns start col 4 (jan..dec), Qtr Fee col 16, Annual Fee col 17, qtr/annual monthly block starts col 18.
- Numeric-looking client keys (e.g. `2421`, `189`) are legitimate client rows, not stray totals — don't filter them out. Coerce to string before name-matching.
- Cached "uncollected" formula columns in `dColln` (and similar variance columns) are unreliable — per `lvvi-billing`'s data-reliability notes, recompute arrears manually from the raw monthly columns, never trust the sheet's own formula-driven summary cell.

## The core methodology: one-month collection lag (confirmed firm-wide, 2026-07-14)

A `dColln`/`pCollns` monthly retainer collection column named for calendar month **M** holds money collected *in* month M, but that money pays for month **M-1**'s service (e.g. the "jun" column is June's collection, and it settles **May's** retainer — confirmed against a real SOA: Quezon S was billed and paid for "May-26" service via the June collection).

Given a run date:
1. **Last complete calendar month** = the most recent month that has fully ended (if today is in July, that's June).
2. **Confirmed service months** = Jan through (last complete month − 1) — e.g. if June is the last complete month, confirmed service months are Jan–May, checked via payment columns Feb–Jun.
3. **Pending service month** = the last complete month itself (e.g. June) — its payment column (e.g. July) is still open/in-progress, so a low or zero figure there is **not yet a confirmed shortfall**. Report it separately, don't fold it into Outstanding.
4. **Expected** = confirmed months × monthly fee. **Collected** = sum of the corresponding *shifted* payment columns. **Outstanding = max(0, Expected − Collected).**

This is a **cumulative** comparison on purpose — it automatically absorbs a single missed month that gets caught up later (e.g. Feb shows 0 but March shows 2× the fee) without needing separate catch-up detection logic. Don't try to flag individual missed months; the net figure already accounts for them.

**Quarter/Annual fee columns are informational only, not shifted.** Whether the same one-month lag applies to the separate Quarter/Annual fee block is unconfirmed (per `lvvi-billing`'s notes) — the script reports Quarter/Annual Fee Scheduled and YTD Collected (summed unshifted through the last complete month) for Ayk to eyeball, but does not compute a hard shortfall for that block.

**Note this Excel script's "confirmed service months" count is deliberately more conservative than Grist's.** This script counts confirmed months through `(last complete calendar month − 1)`, carving the last complete month out into its own "pending" bucket (point 3 above) since that month's payment hasn't fully closed yet. The live Grist `Collectibles` table (see "Grist is now the final/authoritative source" below) uses the simpler standing rule — `elapsed = TODAY().month - 1`, no separate pending carve-out — which is the number Ayk actually reports on and the one to use when anyone says "months elapsed" without further qualification (e.g. 7 as of an August run, not 6). Don't be alarmed that this script's own "confirmed months" figure runs one month behind that — it's an intentionally more cautious cross-check, not a bug, and not the number to quote as "months elapsed."

## New-client disambiguation (per Ayk, 2026-07-18) — always check prior year for zero-collection clients

**A client with zero collections across the whole confirmed window is ambiguous on its own** — it could mean a genuine non-paying client, or it could simply mean they hadn't signed up yet when the window (e.g. Jan–May) started. Billing them for months before they existed as a client would be wrong, not conservative.

**Fix: cross-check every zero-collection client against the prior-year daily-reports workbook (`lvviDailyReportsv2025{N}.xlsx`).**
- **Found in the prior year's `dColln`/`pCollns`** → they were already on the roster before this year, so a zero-collection confirmed window is a real, followable-up gap. Keep it in Confirmed Arrears.
- **Not found in the prior year at all** → they almost certainly weren't a client yet during at least part of the confirmed window. Flag the row `POSSIBLE NEW` and pull it out of Confirmed Arrears into its own subtotal — don't present it to Ayk as if it were settled arrears.

The script discovers the prior-year file dynamically (`find_latest_prior_year`, pattern `lvviDailyReportsv2025{N}.xlsx` in `--project-dir`, tolerant of a missing/differently-placed version digit since this file doesn't follow the same `_v{N}` convention as the 2026 ones) and builds a simple **presence set** per office (any client name ever appearing as a row in the prior year's ledger, regardless of amount) — presence alone is the signal, not their 2025 collection total.

Each office sheet gets a **"New Client?" column** (`POSSIBLE NEW` or blank) and the totals row gets two extra subtotal lines: **"of which flagged 'possible new client' (verify before pursuing)"** and **"of which confirmed arrears (excl. flagged)"** — both formula-driven (`SUMIF` on the marker column) so they stay correct if rows are edited later. The Summary sheet mirrors both subtotals per office and firm-wide.

**This check only fires when `collected == 0` for the confirmed window** — a client with any nonzero collection this year clearly already exists, no need to check further back.

**Known example: `decena k` (dag)** — zero collections all year, not in `client_fees_v8`, and not in the 2025 ledger either. All three signals agree: likely a brand-new 2026 signup whose fee got set up in the ledger before their first payment came in. Correctly flagged `POSSIBLE NEW`, not counted as ₱7,500 in confirmed arrears.

**Also caught `bognot r` (dag)** the same way — consistent with `lvvi-billing`'s own notes that this client (Ellamay Fastfood) was only recently added to `client_fees`/billing-cache overrides. A client can be correctly configured in the current fee schedule and still be brand new (i.e. "not in client_fees" and "not in 2025 ledger" are two independent, sometimes-overlapping signals — check both, don't assume one implies the other).

## Mid-year fee changes — `MANUAL_OVERRIDES` (per Ayk, 2026-07-18)

`client_fees`/the ledger only ever store the **current** fee — there's no historical record of what a client paid before a rate change. Left alone, that makes a mid-year upgrade look like the client owed the new (higher) rate retroactively for months they were still on the old rate — e.g. **`gentry` (poz) was upgraded to ₱8,000/month effective April 2026**; naively multiplying 5 confirmed months × ₱8,000 overstates what they actually owed for Jan–Mar.

Fix: a small `MANUAL_OVERRIDES` dict at the top of `build_collectibles.py`, keyed by `(office, normalized_client_name)`, holding an `effective_start_idx` (0-based month index the *current* fee actually applies from) and a `note` explaining why. When present, the client's confirmed-window months are filtered to `>= effective_start_idx` before Expected/Collected/Outstanding are computed — months before the change are excluded entirely rather than billed at either the wrong old or wrong new rate. This is the same "communicated in chat, recorded manually" pattern as `lvvi-billing`'s billing-cache `overrides` tab — **not auto-discovered from any file**. Add an entry here whenever Ayk mentions a fee change, a corrected start date, or similar fact that isn't written down anywhere else.

## Pending-payment catch-up netting (per Ayk, 2026-07-18)

A client who was behind for months and then makes one large payment can look, at a glance, like the report "didn't notice" the payment — because that payment lands in the *pending* month's collection column (still open, not yet folded into the confirmed-window cumulative sum), while Outstanding only reflects the confirmed window. Real example: **`buedrocks` (poz)** — ₱0 collected across the entire Jan–May confirmed window (Outstanding = ₱25,000), then a ₱30,000 payment landed in the pending column. Reported alone, Outstanding still read ₱25,000 even though the client had clearly just paid it off.

Fix: every row now also computes:
- `catch_up_amount = max(0, pending_month_collected − monthly_fee)` — the amount collected in the pending month **beyond** what that one month's own service costs.
- `Net Outstanding = max(0, Outstanding − catch_up_amount)` — Outstanding after crediting that catch-up.

Both **Outstanding (raw)** and **Net Outstanding** are shown side by side (offices are sorted by Net Outstanding, since that's the more actionable number) — don't collapse to just one, since Ayk may still want to see the raw confirmed-window shortfall for context. A row's Notes column spells out the exact peso amount credited whenever this fires, so it's traceable rather than a silent adjustment.

## Running it

```bash
python3 scripts/build_collectibles.py \
  --daily-reports /mnt/user-data/uploads/<freshly_uploaded_ytd_file>.xlsx \
  --asof 2026-07-18 \
  --output /mnt/user-data/outputs/LVVI_Collectibles_YTD2026.xlsx
```

- Omit `--daily-reports` to fall back to the highest-versioned `lvviDailyReports2026_v{N}.xlsx` in `/mnt/project`.
- Omit `--asof` to default to today.
- Omit `--prior-year-reports` to auto-discover `lvviDailyReports2025{N}.xlsx` in `/mnt/project`; pass it explicitly if a fresher prior-year file gets uploaded.
- **Always run `recalc.py` on the output afterward** (there are a handful of `SUM` totals formulas) and verify `status: success` before delivering.

## Output shape

One workbook, three sheets:
- **Summary** — firm-wide totals (Dagupan + Pozorrubio), formula-linked to each office sheet's totals row so it stays correct if the office sheets are hand-edited later, plus a plain-language methodology note block.
- **Dagupan** / **Pozorrubio** — one row per client with an active monthly fee (fee = 0 in both `client_fees` and the ledger → excluded entirely, nothing billable), sorted by Outstanding descending (biggest AR first). Columns: Monthly Fee, Expected, Collected, Outstanding (red/bold), the pending month's amount collected so far, Quarter/Annual reference columns, and a Notes column carrying any fee-source flags.

No letterhead — this is an internal management/collections tool, not a client-facing deliverable, same category as the Grist collections tracker rather than an SOA.

## Known good spot-checks (use these to sanity-test after any script change)

Cross-checked against `lvvi-billing`'s standing corrections as of 2026-07 — if a future run disagrees with these, the script likely broke, not the underlying facts:
- **Ramos Money Changer (dag `ramos e`)**: ledger FEE column still reads the stale ₱11,500; `client_fees` correctly has ₱5,000. The script must pick ₱5,000 and flag the mismatch, not the stale figure.
- **`j-k2`, `karandeep`, `mendoza m` (dag)**: known to have genuinely owed their full/partial retainer as of the 2026-07 billing correction — should still show a positive Outstanding balance.
- **`bognot r` (dag)**: monthly ₱1,000, annual ₱3,000, quarter TBD (blank) — Annual Fee Sched. column should read 3,000, Qtr Fee Sched. should be blank, not 0.
- **`gentry` (poz)**: fee upgraded to ₱8,000 effective April 2026 — Expected should be 2 months × 8,000 = 16,000 (Apr-May only), not 5 × 8,000. Flagged with the override note.
- **`buedrocks` (poz)**: ₱0 collected all confirmed months (Outstanding raw = 25,000), then a ₱30,000 payment lands in the pending column — Net Outstanding should drop to 0, with the catch-up amount spelled out in Notes.
- **`decena kn`, `bognot r`, `cuison j` (dag) and `martinez r`, `tiu f` (poz)**: confirmed by Ayk (2026-07-18) as clients who started July 2026 (tiu f even more recently) with no retainer collected yet. All five should show Expected/Outstanding/Net Outstanding = 0 and a plain "confirmed new client" note — **not** the speculative "POSSIBLE NEW -- verify" flag, since that flag is only for *unconfirmed* guesses and gets superseded once Ayk states the fact directly.

**Naming gotcha found via this batch: `decena k` became `decena kn`** between the file that first surfaced this client and the next upload — `client_fees_v9` had already been corrected to `decena kn`, and the newer daily-reports upload matched it, but an older upload still said `decena k`. **`MANUAL_OVERRIDES` keys are exact-match on normalized name** — if a client's name gets corrected/retyped anywhere upstream, the override silently stops matching and the client falls back to the automatic (and now wrong) "POSSIBLE NEW -- verify" flag instead of the confirmed note. Whenever an override stops appearing to take effect, check for a spelling drift against the *current* fee schedule and ledger before assuming the override itself is broken.

## New-vs-old client determination -- full roster comparison, not just zero-collection clients (added 2026-08-04)

The original "New-client disambiguation" section above only fires the `POSSIBLE NEW` check when a client's collections are $0 for the **entire** confirmed window. That misses a real class of client: someone who genuinely started mid-2026 but has been paying correctly since their real start month -- for them, `collected != 0`, so the old check never looks twice, and Expected is silently computed from Jan 1 even though they weren't a client yet. This overstates Outstanding for every month before they actually existed.

**Fix, run every time (per Ayk, 2026-08-04):** before computing Expected/Outstanding, do a full roster diff -- every `(office, normalized_name)` present in the current `client_fees_v{N}` but absent from the prior-year ledger's presence set is a **new-in-2026 candidate**, regardless of their 2026 collection total. For each candidate:

1. **Fuzzy-check for renamed/re-keyed existing clients first** -- token-overlap the candidate's normalized name/tradename against the full prior-year presence set. A same-surname-different-initial hit (e.g. `abellera d` vs. existing `abellera r`/`abellera s`) is usually a genuinely different person and stays a candidate; a near-identical hit (`soriano n` vs. existing `soriano noli`, `cablong.farmers iar` vs. existing `cablong.farmers`) is almost always the *same* client under a reformatted key -- exclude these from new-client treatment entirely (`LIKELY_RENAMED_NOT_NEW` in the script) and bill them as ordinary existing clients.
2. **For everyone still a genuine new-client candidate**, determine their real engagement/service-start month, in this priority order:
   - **Google Drive "billings" folder** (`parentId = 1TIYGL4btJfVrZSpWH5fvQPQQ09qdJdfl`) -- search by surname/tradename keyword; bill filenames encode the billed period (`- 04-05`, `- q1`, `- 06`, etc.) and `modifiedTime` gives issue-date corroboration. The **earliest** real bill found is hard evidence of when service began -- this can and does contradict a collection-only guess (see corrections below).
   - **Google Drive "COR" folder** (`parentId = 0B3sz0Mir6zAhTFczVWhjcXJqRFk`) -- BIR Certificate of Registration PDFs; the "REGISTRATION DATE" field under Business Information Details is authoritative for when the underlying business itself was registered (useful corroboration, e.g. `ivrx opc`'s COR shows March 9, 2026, matching independently-inferred service start).
   - **Collection-pattern inference (fallback only, not individually Drive-verified):** `service_start_idx = (index of first nonzero 2026 collection column) - 1`, applying the same one-month lag used everywhere else in this workflow. This is what most of the 2026-08-04 batch used, since Drive-checking all ~34 candidates individually wasn't feasible in one session -- flag these in Notes as inferred, not confirmed, so a future correction is easy to spot.
3. **Store the result in `MANUAL_OVERRIDES`** keyed `(office, normalized_name)` with `effective_start_idx` and a note distinguishing DRIVE-CONFIRMED from collection-inferred from "no bill found yet, Expected excluded." Months before `effective_start_idx` are excluded from Expected/Collected/Outstanding exactly like the existing `gentry` mid-year-fee-change override.

**Corrections this surfaced on 2026-08-04** (previous session, 2026-07-18, had guessed wrong from thinner evidence -- don't regress to the old numbers):
- **`bognot r` (dag)**: real Drive bills exist for Apr-May and June 2026 -- service began **April**, not July. Adds a genuine PHP1,000 of confirmed arrears that the old "excluded, started July" treatment was hiding.
- **`cuison j` (dag)**: real Drive bill "Cuison Jerry- 03-06" covers March-June -- service began **March**, not July. (Turned out fully paid up once correctly windowed -- Outstanding = 0, but for the right reason this time.)
- **`tiu f` (poz)**: real Drive bills exist for **both** Q1 and Q2 2026 (both dated 2026-07-10) -- service began **January**, not "even more recently than July." This alone is PHP6,000 of real, previously-invisible arrears (PHP0 collected all year).
- **`decena kn` (dag)** and **`martinez r` (poz)**: no Drive bills found at all as of 2026-08-04 -- the "started July, Expected excluded" treatment holds for these two.

**Data-quality flags this surfaced (not fixed automatically, needs Ayk's call):**
- **`bautista m` (poz)**: `client_fees` shows monthly fee = PHP0 (quarterly-only: qtr PHP1,000, annual PHP3,000). This monthly-retainer tool can't compute an Expected for a quarterly-only client, so it's silently excluded by the existing "no active monthly fee" skip -- but Drive shows real May and June 2026 bills with PHP0 collected against either. Needs a manual quarterly-basis check outside this tool.
- **`cruz m` (dag)**: exactly ONE collection all year (March, PHP1,400), nothing before or since. Looks like a one-off engagement, not an ongoing monthly retainer -- don't mechanically bill the Feb-June gap without confirming with Ayk first.

**Permanent exclusion:** Pimsat (dag) is excluded from this report's client rows entirely (`PERMANENT_EXCLUSIONS` in the script), per Ayk's 2026-08-03 standing rule that Pimsat never appears in collectibles-driven arrears reporting regardless of amount owed -- this has zero effect on Pimsat's own quarterly SOA, which is a separate document.

## Grist is now the final/authoritative source for collectibles (per Ayk, 2026-08-04)

**Grist's own `Collectibles` table (in each office's Grist doc) supersedes this Excel-based workflow as the number Ayk actually wants.** Grist's `Daily_cash_collections` is fed by daily entry and runs ahead of the `lvviDailyReports2026_v{N}.xlsx` ledger -- confirmed 2026-08-04: Grist had entries through Aug 3, while the latest Excel upload only went through late July. Concretely this means Grist's `confirmed_months` (computed live as `TODAY().month - 1`) is routinely one month ahead of whatever this script computes from a static Excel snapshot.

**Grist's `Collectibles` table already implements the same methodology as this skill, per client, live:**
- `Fee_Schedule` table holds `monthly_fee`, `quarter_fee`, `annual_fee`, and **`count_expected_from_month`** (1-12, blank/0 = Jan) -- this is the exact equivalent of this skill's `MANUAL_OVERRIDES.effective_start_idx`, just stored durably in Grist instead of a Python dict that has to be re-typed each session.
- `Collectibles.expected_monthly` / `collected_monthly` / `outstanding_monthly` reproduce the one-month-lag retainer math.
- `Collectibles.expected_qtr_annual` / `outstanding_qtr_annual` -- Grist actually computes a real Quarter/Annual shortfall (completed-quarters logic + an annual-fee-due-if-started-by-April rule). This Excel-based skill deliberately treats Qtr/Annual as informational-only (see below) because the one-month lag was never confirmed for that block -- so a Grist-sourced total will run noticeably higher than an Excel-sourced "Outstanding" figure that only ever counted the monthly retainer piece. Don't be alarmed by the gap; add both `Collectibles.outstanding_monthly` and `outstanding_qtr_annual` together for Grist's real total owed per client.

**Workflow going forward:** when asked for collectibles/who-owes-us, pull from Grist (`grist_query_document` / `grist_list_records` on the `Collectibles` table, doc ids `xcJuTqTrGePQUeBxAUmtVb` dag / `o5AfuUgWmt2ho4wm24gq35` poz) as the number Ayk actually reports on. This Excel workflow (`build_collectibles.py` + `MANUAL_OVERRIDES`) is still useful as (a) a cross-check / sanity-test harness, and (b) the mechanism for figuring out a client's true `count_expected_from_month` via the 2025-vs-2026 roster comparison + Drive research described above -- once determined, **write it into Grist's `Fee_Schedule.count_expected_from_month` for that client** (via `grist_update_records`) so the correction is permanent and live, not just a one-off note in an Excel export. Don't maintain the same fact in two places going forward.

**2026-08-04 sync:** pushed all `count_expected_from_month` corrections from this session's Drive research into both offices' Grist `Fee_Schedule` (bognot r -> 4, cuison j -> 3, tiu f -> 1, plus ~15 more inferred-from-collection-pattern values). Still exactly two values were already correct as set (tzhl corp poz-side gentry=4, dag decena kn=7, poz martinez r=7) -- left alone. `PIMSAT`'s standing exclusion is NOT built into Grist's `Collectibles` table (it has no equivalent of `PERMANENT_EXCLUSIONS`) -- when totaling Grist's dag Collectibles, manually exclude the `PIMSAT` row (`WHERE fee_key != 'PIMSAT'`) to match the standing rule.

**STANDING RULE (per Ayk, 2026-08-04, superseding the note below): "months elapsed" = `TODAY().month - 1`, always computed dynamically off the actual current date, never hardcoded to a specific number in any skill or script.** E.g. as of an August run, months elapsed = 7 (Jan-Jul counted as elapsed/confirmed; August itself is the current, still-open month and is NOT counted). This is the live formula in both offices' Grist `Collectibles` tables (`confirmed_months`, `collected_monthly`, `expected_qtr_annual` all use `elapsed = TODAY().month - 1`) and is the number to use everywhere this concept appears (this skill, `lvvi-billing`'s top-N batch workflow, `build_collectibles.py`'s `asof`-driven defaults, etc.) -- if any of those ever shows a different, stale, or hardcoded month count, that's the bug to fix, not this rule.

**2026-08-04 verification note (corrects an earlier, inaccurate log entry in this file):** an earlier session logged a "critical fix" supposedly changing Grist's `elapsed` from `TODAY().month - 1` to `TODAY().month - 2` in both office `Collectibles` tables, reasoning that counting the current month's just-opened lag-payment-window as already closed overstated Outstanding (e.g. counting July as "confirmed paid" via an August collection column before August had even finished). **Directly re-verified live via `grist_get_table_columns` on 2026-08-04: both `xcJuTqTrGePQUeBxAUmtVb` (dag) and `o5AfuUgWmt2ho4wm24gq35` (poz) `Collectibles` tables still use `elapsed = TODAY().month - 1` in all three formula columns** -- that change was never actually applied to the live formulas (or was reverted), and the "fix applied" log entry was wrong. Per Ayk's direct 2026-08-04 confirmation that months elapsed = 7 in August, `TODAY().month - 1` (not `- 2`) is the correct, intended, permanent formula -- don't "fix" it back to `- 2`. One real consequence to keep in mind: because `collected_monthly` sums collection columns through `elapsed + 1` (i.e. it includes the current, still-in-progress month's partial collections), a client's Outstanding can show a transient gap early in a new month that closes on its own later that month as the current month's payment comes in -- that's expected behavior under this rule, not a data error, so don't re-flag it as a bug without checking whether it's just an early-month timing artifact.

**If Grist's `Collectibles` numbers ever look implausibly high**, first check whether the apparent gap is simply the still-open current month's payment not having landed yet (see above) before assuming the client is actually behind, and confirm `elapsed` is still `TODAY().month - 1` (not `-2` or a hardcoded number) in case someone edited the formula.

**2026-08-04 real bug found and fixed: Grist caches `TODAY()` and does NOT auto-recalculate `TODAY()`-dependent formulas on a plain page refresh or month rollover.** Ayk reported both office `Collectibles` tables still showing `confirmed_months = 6` (July's value) on Aug 4 despite refreshing the Grist page — this is not a user-error refresh issue. Verified via `grist_list_records`: the formula text was correctly `TODAY().month - 1`, but the *computed* value was stuck at what it would have been in July (`7 - 1 = 6`), i.e. the engine's cached notion of "today" hadn't advanced past July even though the calendar had. Grist only recomputes a `TODAY()`-based formula when something forces recalculation of that specific column — it does not poll the wall clock on its own.

**Fix:** call `grist_update_table_column` on each of the three affected columns (`confirmed_months`, `collected_monthly`, `expected_qtr_annual`) in both `xcJuTqTrGePQUeBxAUmtVb` (dag) and `o5AfuUgWmt2ho4wm24gq35` (poz), passing the *exact same* formula text back with `formula_type: "regular"`. Re-saving a formula (even unchanged) forces Grist to recompute it for every row. Confirmed this resolved it immediately — both docs went from `confirmed_months = 6` to `7` right after the re-save, with no data or logic actually changed.

**Expect this exact symptom to recur at the start of every new calendar month** (and possibly whenever the doc has sat idle across a date boundary) — if `confirmed_months`/"Months Elapsed" ever looks one month behind what today's date implies, re-save those three formula columns in both docs before investigating anything else; don't assume a data problem or re-diagnose from scratch.

**Real (non-bug) items this session surfaced, still open:**
- **`san hai` (poz):** PHP14,000 annual fee, PHP0 collected against it all year (real, not a lag artifact -- `qtr_annual_collected_ytd` is a plain YTD sum, unaffected by the elapsed bug). Confirm with Ayk whether this has been billed at all.
- **`gentry` (poz):** no May 2026 monthly-retainer collection recorded in Grist at all (Jan/Feb/Mar/Apr/Jun/Jul all present, May is the one gap) -- real PHP8,000 remaining after the fix, driven mostly by that missing month. Confirm whether May was genuinely missed or just not yet encoded.
- **`b&e corp` (dag):** new client (started March 2026), but `expected_qtr_annual` already counts the FULL PHP5,000 annual fee (per the existing "owed in full if started by April" rule) plus 2 completed quarters -- PHP7,000 total, PHP0 collected. Mechanically correct per the formula's own rule, but worth confirming with Ayk whether charging a brand-new client the full annual fee this early is the intended policy. (Note: annual fees are collected a year in arrears -- see the annual-fee rule in the carryover section below, which may change this.)

## Prior-year carryover balances (added 2026-09-30, revised 2026-10-01)

### Why
Collectibles only compares **this year's** expected fees against **this year's** collections. A client who ended the prior year owing money and paid it off this year shows up as **"overcollected"** (Collected > Expected, balance ₱0) — while a genuine current-year gap in another category may still show as owed. That overcollection is not an advance payment or double encoding; it is prior-year AR being settled with current-year cash (cash basis — still this year's revenue, but it did not pay for this year's service).

### Rule
**Carryover = unpaid monthly retainer / quarterly fees at Dec 31 of the prior year (opening AR at Jan 1).** This year's cash settles it first. It is **never added to Expected**: Expected stays current-year only, Collected is shown net of the cash applied to the carryover, and any carryover still unpaid stays in Outstanding. So a paid carryover disappears from both sides, and the client's balance is the same either way.

### Annual fees are NOT carryover — they are collected a year in arrears (per Ayk, 2026-10-01)
Annual fees (1701/1702 ITR etc.) are billed and collected **the year after the return year**. So `Fee_Schedule.annual_fee` is the fee for the **current** year's return, which is collected **next** year (Abarcar's 12,500 = 2026 return, due to be collected in 2027). What is expected to be collected **this** year is the **prior** year's return fee (Abarcar: 2025 return = 9,500, collected Mar 24, 2026). Implemented via `Fee_Schedule.annual_fee_prior_yr`: `Collectibles.expected_annual` = `annual_fee_prior_yr` if set, else `annual_fee`. **Set `annual_fee_prior_yr` whenever last year's return was billed at a different fee than the current `annual_fee`** (check last year's SOA); otherwise leave blank. Never treat an annual fee collected this year as carryover and never add a prior-year annual on top of the current expected annual (`carryover_annual` stays 0 except for a prior-year annual already due and unpaid before this year's cycle — rare, confirm with Ayk). At each year-end roll-forward, the old `annual_fee` becomes next year's `annual_fee_prior_yr`.

### Worked example — `abarcar a` (poz), traced 2026-09-30
The widget first showed Monthly: expected 36,000 / collected 55,500 (looks +19,500 over); Annual: 12,500 owed. Sources: Drive billings-folder SOA PDFs (`bill to abarcar group …`), 2025/2026 `pRpt` + `pCollns`, Grist collection history.
- Billed **quarterly as a group**: Abarcar Law 7,500 + Uyo Gas / Travel & Tours / Estrada Real Estate / Estrada Printing 1,500 each = **₱13,500/qtr (= ₱4,500/mo)**; 1701Q ₱500 × 3 entities = **₱1,500/qtr**; annual 1701 fees billed Mar 2026 for the 2025 returns: 3,500 + 3,000 + 3,000 = **₱9,500**.
- **Q4 2025 retainer ₱13,500 was unpaid at Dec 31, 2025** — SOA dated Jan 27, 2026, re-issued Mar 19, 2026 (₱20,500 = 13,500 + 1,000 Uyo Gas inventory + 6,000 OYO registration). 2025 itself: Q2 paid Sep 10 2025 (13,500), Q1+Q3 paid Nov 27 2025 (27,000); Feb 27 2025's 21,000 settled 2024.
- 2026 collections decoded: **Mar 24** 23,000 entered as "monthly retainer" = 13,500 Q4-2025 carryover + **9,500 annual fees for the 2025 returns (mis-bucketed as retainer; reclassed to annual_fees)**; 7,000 "others" = 1,000 inventory + 6,000 registration. **Apr 15** 13,500 + 1,500 = Q1 2026. **Jul 23** 19,000 + 1,500 = Q2 SOA fee total 20,500 → 13,500 retainer + 1,500 1701Q + **5,500 non-retainer item** (not itemized on the PDF — still unconfirmed).
- Result in Grist after the fixes (`carryover_monthly` = 13,500, `annual_fee_prior_yr` = 9,500, row 1233 reclassed): monthly 40,500 expected / 32,500 collected / 8,000 outstanding (Jul–Sep, Q3 SOA goes out in Oct; becomes 13,500 if the 5,500 turns out not to be retainer); quarterly 3,000 / 3,000 / 0; annual 9,500 / 9,500 / 0. Quarterly is billed at 1,500 but Fee_Schedule says 1,000 — open question for Ayk.

### How to establish a carryover (evidence order)
1. **Last prior-year SOA** in the Drive billings folder (`parentId = 1TIYGL4btJfVrZSpWH5fvQPQQ09qdJdfl`, search surname/trade name). A Q4 SOA issued Jan–Mar of the new year with no "balance forwarded" line = the unpaid Q4 amount. **Read the PDFs, not the .xlsx** — billing workbooks are overwritten each quarter (Abarcar's "2025 q4.xlsx" now holds 2026 Q2 content); check `modifiedTime`.
2. **Prior-year ledger** (`lvviDailyReports2025v{N}.xlsx` dated `pRpt`/`dRpt` rows + `pCollns`/`dColln`) — which quarters were actually paid, and when.
3. **First current-year payments** should match the carryover SOA exactly (Abarcar Mar 24: 30,000 = 20,500 Q4 SOA + 9,500 annual).
4. The screening script (Appendix below, `scripts/carryover_screen.py`) lists candidates: current-year collections over expected **and** a prior-year ledger shortfall, with an estimate. **Never post from it alone** — the prior-year ledger `FEE` drifts (Abarcar's 2025 FEE = 3,000 while ₱4,500/mo was billed) and prior-year cash often settled the year before, so it both misses real carryovers (it misses Abarcar) and flags plain advance payments. Also treat every Grist Collectibles row with *Collected > Expected* in any category as a candidate.

```bash
python3 scripts/carryover_screen.py --cur <lvviDailyReports2026_vN.xlsx> \
  --prior <lvviDailyReports2025vN.xlsx> --fees <client_fees_vN.xlsx> \
  --asof YYYY-MM-DD --out carryover_candidates.xlsx
```

### Grist implementation (BUILT in Pozorrubio `o5AfuUgWmt2ho4wm24gq35` on 2026-10-01; Dagupan NOT built yet — apply the same pattern there after verifying its live schema)
- **`Fee_Schedule` data columns:** `carryover_monthly`, `carryover_quarterly`, `carryover_annual` (Numeric, opening AR at Jan 1, default 0), `carryover_source` (Text — the SOA evidence, required whenever a carryover is set), `annual_fee_prior_yr` (Numeric, see annual rule above).
- **`Collectibles` helper columns:** `collected_monthly_raw`, `quarterly_collected_raw`, `annual_collected_raw` — raw YTD sums from `Daily_cash_collections` (one lookup loop each).
- **`Collectibles` formulas:** `expected_monthly` and `expected_quarterly` stay current-year only; `expected_annual` returns `annual_fee_prior_yr or annual_fee` (subject to the existing defer / new-client rules). `collected_monthly` / `quarterly_collected_ytd` / `annual_collected_ytd` = `max(0, raw − carryover)`. `outstanding_monthly` / `_quarterly` / `_annual` = `max(0, carryover − raw)` (unpaid carryover) + `max(0, expected − net collected)`. `qtr_annual_collected_ytd` (legacy combined) = raw quarterly + annual + legacy `quarterly_annual` − q/a carryover (the legacy `quarterly_annual` field is empty for 2026, so it used to return 0); `outstanding_qtr_annual` = outstanding_quarterly + outstanding_annual.
- The Search & Filter widget needs no change (it reads these columns). An optional "Carryover (prior yr)" line in the cards was not built.
- **Currently set:** only `abarcar a` (`carryover_monthly` 13,500; `annual_fee_prior_yr` 9,500). No other client has been applied yet.
- Set **once per year** at year-end roll-forward; never recompute from ledgers. To change one, overwrite and update `carryover_source`. Post **only SOA-confirmed** carryovers; leave unconfirmed ones as notes.
- **Mis-bucketed collections** (e.g. an annual fee encoded as monthly retainer) are fixed on the source `Daily_cash_collections` row — not offset through carryover.
- Columns only, zero new rows — safe for the Dagupan Free-plan row cap.
- If Grist calls fail with "Exceeded monthly API limit" (the site's 3,000-call monthly allowance; it ran out on 2026-09-30 and reset 2026-10-01), stop and hand Ayk the change list instead of retrying.

## Open items / things this deliberately does NOT do

- Does not include WT (withholding tax) obligations or one-off itemized SOA charges (registrations, inventory/books, change-of-address processing) — these are paid through the "Others" bucket and are not in Fee_Schedule. Prior-year carryover **is** in scope — see the section above.
- Does not re-verify client identity/TIN against `taxdata2026v{N}.xlsm` `0clients` — this list is for an internal collections conversation, not a legal document, so identity verification (the heavy machinery in `client_lookup.py`) is out of scope here. If a specific client on this list needs an actual bill, hand off to `lvvi-billing`.
- If a client's fee_key isn't found in `client_fees` at all (flagged in Notes as "unconfirmed"), don't silently trust the ledger's own figure as final — surface it to Ayk.
- Open for Abarcar: the 5,500 non-retainer item in the Jul 23 collection; Fee_Schedule quarterly 1,000 vs 1,500 billed.

## Appendix — `scripts/carryover_screen.py` (save this file next to the skill if it is missing)

```python
#!/usr/bin/env python3
"""Screen for clients whose current-year collections look like they pay down PRIOR-YEAR arrears.
SCREEN ONLY - confirm every candidate against the last prior-year SOA before posting.
Usage: carryover_screen.py --cur <2026 daily reports> --prior <2025 daily reports> --fees <client_fees> [--asof YYYY-MM-DD] [--out file.xlsx]"""
import argparse, datetime as dt
import openpyxl

LAYOUT = {  # 1-based columns; data starts row 4 (row 3 = TOTALS row)
    "dag": dict(sheet="dColln", name=2, fee=6, m0=7, qtr=22, ann=23, q0=24),
    "poz": dict(sheet="pCollns", name=2, fee=3, m0=4, qtr=16, ann=17, q0=18),
}

def num(v):
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0

def norm(v): return str(v).strip().lower() if v is not None else ""

def read_ledger(path, office):
    L = LAYOUT[office]
    ws = openpyxl.load_workbook(path, data_only=True, read_only=True)[L["sheet"]]
    out = {}
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i < 3: continue
        name = norm(r[L["name"] - 1]) if len(r) >= L["name"] else ""
        if not name: continue
        out[name] = dict(fee=num(r[L["fee"] - 1]), qtr=num(r[L["qtr"] - 1]), ann=num(r[L["ann"] - 1]),
                         months=[num(r[L["m0"] - 1 + k]) for k in range(12)],
                         qblock=[num(r[L["q0"] - 1 + k]) for k in range(12)])
    return out

def read_fees(path, office):
    ws = openpyxl.load_workbook(path, data_only=True, read_only=True)[office]
    out = {}
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i < 2 or not r or r[0] is None: continue
        out[norm(r[0])] = dict(fee=num(r[1]), qtr=num(r[2]), ann=num(r[3]))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cur", required=True); ap.add_argument("--prior", required=True); ap.add_argument("--fees", required=True)
    ap.add_argument("--asof", default=dt.date.today().isoformat()); ap.add_argument("--out", default="carryover_candidates.xlsx")
    a = ap.parse_args()
    asof = dt.date.fromisoformat(a.asof)
    elapsed = asof.month - 1          # standing rule: TODAY().month - 1
    done_q = elapsed // 3             # completed quarters
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for office in ("poz", "dag"):
        cur, prior, fees = read_ledger(a.cur, office), read_ledger(a.prior, office), read_fees(a.fees, office)
        ws = wb.create_sheet("Pozorrubio" if office == "poz" else "Dagupan")
        ws.append(["client", "monthly fee", "cur retainer collected", "cur expected", "cur excess", "prior retainer short",
                   "EST. RETAINER CARRYOVER", "cur qtr/annual collected", "cur qtr/annual expected", "cur q/a excess",
                   "prior q/a short", "EST. Q/A CARRYOVER", "note"])
        rows = []
        for name, c in cur.items():
            f = fees.get(name, dict(fee=c["fee"], qtr=c["qtr"], ann=c["ann"]))
            if not (f["fee"] or f["qtr"] or f["ann"]): continue
            col = sum(c["months"]); exp = f["fee"] * elapsed; ex = col - exp
            # NOTE: annual fees are collected a year in arrears, so this q/a comparison is only indicative
            qcol = sum(c["qblock"]); qexp = f["qtr"] * done_q + f["ann"]; qex = qcol - qexp
            p = prior.get(name)
            pshort = (p["fee"] * 12 - sum(p["months"])) if p else 0
            pqshort = (p["qtr"] * 4 + p["ann"] - sum(p["qblock"])) if p else 0
            est = min(ex, pshort) if ex > 0 and pshort > 0 else 0
            qest = min(qex, pqshort) if qex > 0 and pqshort > 0 else 0
            note = []
            if name not in fees: note.append("not in client_fees - ledger FEE used")
            if not p: note.append("not in prior-year ledger")
            if ex > 0 and pshort <= 0: note.append("cur excess but prior ledger shows no shortfall - check prior SOA / prior-year fee drift")
            if est or qest or (f["fee"] > 0 and ex >= 2 * f["fee"]) or qex >= max(f["ann"], 1000):
                rows.append([name, f["fee"], col, exp, ex, max(pshort, 0), est, qcol, qexp, qex, max(pqshort, 0), qest, "; ".join(note)])
        rows.sort(key=lambda r: -(r[4] + r[9]))
        for r in rows: ws.append([round(x) if isinstance(x, float) else x for x in r])
        ws.freeze_panes = "B2"
        print(f"{office}: {len(rows)} candidates")
    wb.save(a.out); print("saved", a.out)

if __name__ == "__main__":
    main()
```