#!/usr/bin/env python3
"""
carrier-loader worker  —  server-side loader for Helloprint carrier invoices.

Runs on GitHub Actions (network-open runner). For every invoice the intake has
already stored in the Supabase bucket `carrier-invoice-sources` but that is NOT
yet in the Hub, it: downloads the raw file, parses it with the FROZEN parser for
that carrier, builds the two combined CSVs (parcel line + surcharge detail),
TIE-CHECKS against the invoice control total, and loads it through the deployed
`ingest-combined-inline` edge function (idempotent). Never loads a non-tying
invoice. Nothing runs through a Claude session — the runner does the work.

WHY this exists: loading an invoice means writing 1k-6k rows into the Hub. A
Claude session cannot carry that (the drag / inline-CSV / SQL all route the data
through the session). A GitHub Actions runner can POST the whole CSV in one HTTP
call, and reuses the validated Python parsers (no TS re-port, no duplicate logic).

ONE SECRET ONLY. Discovery, download and load all go through cron-secret-gated edge
functions (`loader-feed` for list/download, `ingest-combined-inline` for load), so the
Supabase service role key never leaves Supabase. The only GitHub secret is HUB_CRON_SECRET.

ENV:
  HUB_CRON_SECRET   (GitHub secret, required)  value of public.get_cron_secret()
  SUPABASE_URL      (optional)  defaults to the Hub project URL below
  CARRIERS          (optional)  comma list to restrict (e.g. "dhl_nl,geodis")
  DRY_RUN           (optional)  "1" = parse + tie-check + report, do not load
"""
import os, sys, io, json, re, base64, datetime
import requests

from carrier_registry import REGISTRY, carrier_id_for, invoice_no_from_filename

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "https://djmbiqzwkwlhkxjporww.supabase.co").rstrip("/")
CRON_SECRET  = os.environ["HUB_CRON_SECRET"]
DRY_RUN      = os.environ.get("DRY_RUN") == "1"
ONLY         = {c.strip() for c in os.environ.get("CARRIERS", "").split(",") if c.strip()}

FN           = f"{SUPABASE_URL}/functions/v1"
FEED_URL     = f"{FN}/loader-feed"
INGEST_URL   = f"{FN}/ingest-combined-inline"
CRON_HEADERS = {"Content-Type": "application/json", "x-cron-secret": CRON_SECRET}


_FEED = None
def _feed():
    """Fetch the stored-file list and the already-loaded set in one cron-gated call."""
    global _FEED
    if _FEED is None:
        r = requests.post(FEED_URL, headers=CRON_HEADERS, data=json.dumps({"action": "list"}), timeout=120)
        r.raise_for_status()
        _FEED = r.json()
    return _FEED


def fetch_stored():
    """Intake rows that have a stored file, newest first."""
    return _feed().get("stored", [])


def fetch_loaded():
    """Set of (carrier_id, invoice_number) already in the Hub."""
    return {(cid, str(inv)) for cid, inv in _feed().get("loaded", [])}


def download(storage_path):
    r = requests.post(FEED_URL, headers=CRON_HEADERS,
                      data=json.dumps({"action": "download", "path": storage_path}), timeout=120)
    r.raise_for_status()
    return base64.b64decode(r.json()["b64"])


def ingest(kind, filename, csv_text):
    r = requests.post(INGEST_URL, headers=CRON_HEADERS,
                      data=json.dumps({"kind": kind, "filename": filename, "csv": csv_text}), timeout=300)
    ok = r.status_code < 300
    return ok, (r.json() if ok else r.text[:400])


def main():
    stored = fetch_stored()
    loaded = fetch_loaded()
    results = []
    seen = set()  # (carrier_id, invoice_no) handled this run (one invoice can have pdf+csv rows)
    for row in stored:
        ckey = row["carrier_key"]
        if ONLY and ckey not in ONLY:
            continue
        reg = REGISTRY.get(ckey)
        cid = carrier_id_for(ckey)
        if not reg:
            continue  # carrier not wired yet (see README) — skip quietly
        fname = row.get("filename") or ""
        if not reg["match_file"](fname):
            continue  # not the file type this parser wants (e.g. pdf vs csv)
        inv = invoice_no_from_filename(ckey, fname)
        if not inv:
            results.append({"carrier": cid, "file": fname, "status": "skip_no_invoice_no"})
            continue
        key = (cid, inv)
        if key in loaded or key in seen:
            continue
        seen.add(key)
        try:
            raw = download(row["storage_path"])
            built = reg["build"](raw, fname, inv)  # -> dict(combined, surcharge, control, parsed, lines, ratio)
        except Exception as e:
            results.append({"carrier": cid, "invoice": inv, "status": "parse_error", "error": str(e)[:300]})
            continue
        ratio = built["ratio"]
        if not (0.95 <= ratio <= 1.002):
            results.append({"carrier": cid, "invoice": inv, "status": "tie_fail",
                            "ratio": round(ratio, 4), "control": built["control"], "parsed": built["parsed"]})
            continue
        if DRY_RUN:
            results.append({"carrier": cid, "invoice": inv, "status": "dry_run_ok",
                            "lines": built["lines"], "total_eur": built["parsed"], "ratio": round(ratio, 4)})
            continue
        ok1, m1 = ingest("combined", f"{cid}_{inv}_combined.csv", built["combined"])
        if not ok1:
            results.append({"carrier": cid, "invoice": inv, "status": "load_combined_fail", "error": m1})
            continue
        ok2, m2 = ingest("surcharge", f"{cid}_{inv}_surcharge.csv", built["surcharge"])
        results.append({"carrier": cid, "invoice": inv,
                        "status": "loaded" if ok2 else "loaded_combined_only_surcharge_fail",
                        "lines": built["lines"], "total_eur": built["parsed"], "ratio": round(ratio, 4),
                        **({} if ok2 else {"error": m2})})

    summary = {"ran_at": datetime.datetime.utcnow().isoformat() + "Z", "dry_run": DRY_RUN,
               "stored_seen": len(stored), "handled": len(results),
               "loaded": sum(1 for r in results if r["status"] == "loaded"),
               "tie_fail": sum(1 for r in results if r["status"] == "tie_fail"),
               "errors": sum(1 for r in results if r["status"].endswith("error") or r["status"].endswith("fail")),
               "results": results}
    print(json.dumps(summary, indent=2))
    # non-zero exit if anything failed to tie/parse/load, so the Action surfaces it
    if summary["tie_fail"] or summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
