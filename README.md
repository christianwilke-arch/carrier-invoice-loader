# carrier-loader

Server-side loader for Helloprint carrier invoices into the Partner Hub. One Python worker for every
carrier (CSV, xlsx and PDF), running on GitHub Actions — same pattern as the UPS invoice poller.

## Why this exists

Loading an invoice means writing 1k–6k rows into the Hub. A Claude session cannot carry that: drag,
inline-CSV and SQL all route the whole invoice through the session, which is slow and fails on large
invoices. A fired background session produced **zero** loads. A GitHub Actions runner POSTs the whole
CSV in one HTTP call and reuses the already-validated Python parsers — no TypeScript re-port, no
duplicate logic. Parsing can live in a session; **loading must not**.

## What it does, each run

1. Read the intake log for invoices with `status='stored'` (file saved to the bucket, not yet parsed in).
2. Skip any already in `carrier_invoice_records` (idempotent) — so re-runs are safe.
3. Download the raw file from the `carrier-invoice-sources` bucket with the service role.
4. Parse with the frozen parser for that carrier → build the two combined CSVs (parcel line + surcharge).
5. **Tie-check** against the invoice control total. Load only when `0.95 ≤ ratio ≤ 1.002`; otherwise hold
   and report `tie_fail` (never loads a non-tying invoice).
6. POST combined then surcharge to the `ingest-combined-inline` edge function.

Exit code is non-zero if anything failed to tie/parse/load, so a bad run is visible in the Actions tab.

## Carriers wired now

| carrier_key | format | tie-check |
|---|---|---|
| dpd_uk | CSV | sum(parcel charges) / Nett control |
| dhl_nl | CSV | 1 − \|residual\|/control |
| geodis | PDF | 1 − \|residual\|/control |
| chronopost | PDF | 1 − \|residual\|/control |

Not yet wired (parser bundled, builder present for dpd_fr, un-registered until proven on the runner):
`dpd_fr, correos, correos_express, dx, dpd_nl, dhl_be, gls_fr, royal_mail`. See `carrier_registry.py`.
Un-wired carriers are reported and skipped — never guessed.

## Setup (Christian — one time)

Claude has no push access to GitHub, so add this package to a repo yourself (same account as
`christianwilke-arch/ups-invoice-poller`).

1. Create a repo, e.g. `carrier-invoice-loader`, and drop in all files from this folder
   (keep the `parsers/` folder, `common.py`, `worker.py`, `carrier_registry.py`, `requirements.txt`,
   and `.github/workflows/carrier-loader.yml`).
2. Repo → Settings → Secrets and variables → Actions → add three secrets:
   - `SUPABASE_URL` = `https://djmbiqzwkwlhkxjporww.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = the service role key (Supabase → Project Settings → API)
   - `HUB_CRON_SECRET` = the value of `public.get_cron_secret()`
3. Actions tab → enable workflows.

## First run — validate before trusting it

1. Actions → carrier-loader → Run workflow → set **dry_run = 1**. Read the JSON summary: every wired
   carrier's stored-not-loaded invoices should show `dry_run_ok` with sane `lines`, `total_eur`, `ratio`.
   Any `tie_fail` is a parser/control mismatch to fix before loading that carrier.
2. Re-run with dry_run = 0 to load for real. After that the hourly schedule keeps it current.

## Adding a carrier

In `carrier_registry.py`, add a `build_<carrier>(raw, fname, inv)` that returns
`{combined, surcharge, control, parsed, lines, ratio}`, then add it to `REGISTRY`. Line-schema parsers
(PDF/CSV that emit `list[Line]`) can reuse `_lines_to_csv`. Validate with dry_run = 1 before wiring.

## Relation to the edge-function loaders

`ingest-dpduk-from-storage` (deployed, hourly) already loads DPD UK server-side inside Supabase. This
worker supersedes it for breadth (it does the PDF/xlsx carriers an edge function cannot, via poppler),
and is idempotent against the same records table, so the two cannot double-load. Pick one path per
carrier: keep DPD UK on whichever you prefer and drop it from the other.
