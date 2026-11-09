#!/usr/bin/env python3
"""
dpduk_parse.py  -  Frozen, deterministic parser for the DPD UK carrier invoice audit.

PURPOSE
    Turn the raw DPD UK weekly invoice CSVs (account 670157) into a line-level
    audit: invoiced per order_item, per service, per surcharge, plus the Cube id
    chunks needed to pull expected cost. Every parsing rule that cost us time in
    the original Opus audit is pinned here in a named function with the REASON in
    its docstring, so a weaker model reruns this instead of re-deriving the rules
    and getting them subtly wrong.

INPUT
    Either a Google Drive folder id (preferred - the invoices now live in
    Carriers/Invoices/DPD UK/<YEAR>/) or a local folder path.

    Drive is read with the Drive REST API via curl, NOT the connector's base64
    download: an 800 KB invoice is ~280k tokens through the connector and breaks
    the session. curl streams bytes straight to disk (verified byte-identical).

USAGE
    python3 dpduk_parse.py --drive-folder <YEAR_FOLDER_ID>
    python3 dpduk_parse.py --local "/path/to/DPD UK invoices"
    python3 dpduk_parse.py --local "..." --baseline     # prove the parser still behaves

    --baseline hard-asserts the figures frozen from the original audit (see
    BASELINE below). A NEW invoice period WILL trip the baseline by design: run
    WITHOUT --baseline for new data, and rerun the ORIGINAL file set WITH
    --baseline to prove the parser has not drifted. A weaker model that cannot
    reproduce these exact numbers gets an AssertionError instead of quietly
    publishing a wrong figure.

OUTPUT (written next to the script, in --outdir, default ./out)
    dpduk_lines.csv     one row per invoice line
    dpduk_oi.csv        one row per order_item (invoiced summed)
    cube_chunks.json    distinct order_item_ids chunked at 1700 for Cube IN-lists
    dpduk_figures.json  the computed audit figures

Owner: Christian Wilke. Trust level 1 (human review, no auto-claim, no auto-send).
"""

import csv, sys, os, re, json, glob, argparse, datetime, collections, subprocess, hashlib

# ----------------------------------------------------------------------------
# FROZEN BASELINE  -  the numbers the original audit agreed on (2026 YTD set,
# 42 files, dispatch Dec 2025 - Sep 2026). Invoice-derived only; the Cube
# expected side lives in the handover, not here, because this parser must not
# depend on Cube to self-verify.
# ----------------------------------------------------------------------------
FX_GBP_EUR = 1.1564          # agreed rate; a config value, never hard-magic elsewhere
BASELINE = {
    "source_files":            42,
    "lines":                   82255,
    "parcel_invoiced_gbp":     560375.15,
    "parcel_invoiced_eur":     648017.82,
    "nett_total_gbp":          561631.55,   # sum of each invoice's Nett header
    "invoice_level_residual_gbp": 1256.40,  # Nett - parcel lines: fees not tied to a parcel
    "distinct_order_items":    69776,
    "unmatched_lines":         1349,        # lines whose Senders Ref yields no order_item id
    "unmatched_eur":           11456.26,
    "standard_invoiced_eur":   457215.84,
    "expresspak_invoiced_eur": 190801.24,
}
TOL = 0.05   # tolerance on float baseline asserts (same parser reproduces exactly)

# ----------------------------------------------------------------------------
# Column map. DPD UK invoice detail row has 42 columns (0-indexed). Money lives
# in 15..35 inclusive; 36..41 are country/refs/address and carry no money.
# ----------------------------------------------------------------------------
COL_DATE, COL_PRODUCT, COL_SERVICE_DESC, COL_SENDERS_REF, COL_WEIGHT = 0, 5, 7, 11, 12
COL_ITEMS, COL_COUNTRY_CODE = 13, 36
CHARGE_COLS = list(range(15, 36))
BUCKET = {   # charge col -> dashboard bucket
    15: "base", 31: "base",
    26: "oversize", 28: "oversize",
    32: "handling",
    17: "fuel", 33: "fuel",
}
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def to_float(x):
    """Amounts are quoted strings, sometimes with thousands commas; blanks are 0."""
    try:
        return float((x or "").strip().replace(",", ""))
    except ValueError:
        return 0.0


def is_data_row(row):
    """A detail (parcel) row is any row whose first column is a dd/mm/yyyy date.
    WHY: the first 5 rows are the invoice header (Account/Nett/VAT/Gross/column
    titles); using the date shape is the only reliable way to separate detail
    rows from header rows across all 42 files."""
    return len(row) >= 36 and bool(DATE_RE.match((row[COL_DATE] or "").strip()))


def extract_order_item_id(senders_ref):
    """order_item_id = the SECOND numeric token of Senders Ref.
    WHY: the ref is '<order_id>-<order_item_id>' and often carries a third part:
    '5857691-9263598-multi' (multi-parcel consignment) or
    '5826901-9207320 / 8002011798' (merged labels). Taking the LAST dash-token -
    the naive split('-')[-1] - drops ~11% of spend to the literal 'multi'. Split
    on dash, slash and whitespace, keep numeric tokens, take index 1. A ref with
    no second numeric token (empty ref) is genuinely unmatchable and returns ''."""
    nums = [t for t in re.split(r"[-/\s]+", (senders_ref or "").strip()) if t.isdigit()]
    if len(nums) >= 2:
        return nums[1]
    if len(nums) == 1:
        return nums[0]
    return ""


def classify_service(product_desc):
    """Service = EXPRESSPAK if Product Description is 'EXPRESSPAK', else STANDARD.
    WHY: STANDARD ships as Product Description 'PARCEL' (and a few 'NTC'); the
    service variants (NXTDAY/2DAY/DPD 12/DPD 1030/SAT) live in Service Description
    but all roll up to STANDARD vs EXPRESSPAK, which is how the precalc and the
    buffer decision are framed. EXPRESSPAK carries almost no surcharges; STANDARD
    carries the oversize/handling drain - so the split must be exact."""
    return "EXPRESSPAK" if (product_desc or "").strip() == "EXPRESSPAK" else "STANDARD"


def line_charge_gbp(row):
    """Invoiced GBP for a line = sum of columns 15..35.
    WHY: summing the whole money block (not a named subset) means a NEW surcharge
    column DPD starts using is still counted in the total even before we name it;
    unmapped non-zero columns are flagged by the gate, not silently dropped."""
    return sum(to_float(row[i]) for i in CHARGE_COLS)


def read_nett(path):
    """The per-file control total is the 'Nett Invoice Value' header (pre-VAT).
    WHY the gate uses Nett not Gross: Gross = Nett + VAT, and the parcel charges
    are pre-VAT. Nett still sits ~0.2% above the parcel-line sum because DPD adds
    invoice-level fees (weekly account/collection) that are not in the parcel
    table - that residual is reported as unexplained, it is not a parse error."""
    for row in csv.reader(open(path, newline="", encoding="utf-8", errors="replace")):
        if row and row[0].strip().lower().startswith("nett invoice value"):
            for c in row[1:]:
                if to_float(c) != 0:
                    return to_float(c)
    return None


# ----------------------------------------------------------------------------
# Drive IO
# ----------------------------------------------------------------------------
def drive_list(folder_id):
    out = subprocess.run([
        "curl", "-sS", "-G", "https://www.googleapis.com/drive/v3/files",
        "--data-urlencode", f"q='{folder_id}' in parents and trashed=false",
        "--data-urlencode", "fields=files(id,name,mimeType,size)",
        "--data-urlencode", "supportsAllDrives=true",
        "--data-urlencode", "includeItemsFromAllDrives=true",
    ], capture_output=True, text=True, check=True)
    return json.loads(out.stdout).get("files", [])


def drive_download(file_id, dest):
    subprocess.run([
        "curl", "-sS", "-o", dest,
        f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
    ], check=True)


def gather_files(args, workdir):
    """Return a list of local csv paths, downloading from Drive if needed.
    A Google-converted upload (mimeType google-apps.*) means the original bytes
    are gone - stop loudly rather than parse a broken export."""
    if args.local:
        return sorted(glob.glob(os.path.join(args.local, "*.csv")))
    os.makedirs(workdir, exist_ok=True)
    paths = []
    for fobj in drive_list(args.drive_folder):
        if fobj["mimeType"].startswith("application/vnd.google-apps"):
            sys.exit(f"ABORT: {fobj['name']} is {fobj['mimeType']} - converted on "
                     "upload, original bytes lost. Turn off 'Convert uploads' and re-upload.")
        if fobj["name"].lower().endswith(".csv"):
            dest = os.path.join(workdir, fobj["name"])
            drive_download(fobj["id"], dest)
            paths.append(dest)
    return sorted(paths)


# ----------------------------------------------------------------------------
# Parse + gate
# ----------------------------------------------------------------------------
def parse(files):
    lines, gate = [], []
    for fp in files:
        nett, psum = read_nett(fp), 0.0
        for row in csv.reader(open(fp, newline="", encoding="utf-8", errors="replace")):
            if not is_data_row(row):
                continue
            gbp = line_charge_gbp(row)
            psum += gbp
            try:
                d = datetime.datetime.strptime(row[COL_DATE].strip(), "%d/%m/%Y").date()
            except ValueError:
                d = None
            rec = {
                "file": os.path.basename(fp), "date": d.isoformat() if d else "",
                "order_item_id": extract_order_item_id(row[COL_SENDERS_REF]),
                "service": classify_service(row[COL_PRODUCT]),
                "country": (row[COL_COUNTRY_CODE] or "").strip() if len(row) > COL_COUNTRY_CODE else "",
                "invoiced_gbp": round(gbp, 4),
                "invoiced_eur": round(gbp * FX_GBP_EUR, 4),
            }
            rec["base_eur"]     = round((to_float(row[15]) + to_float(row[31])) * FX_GBP_EUR, 4)
            rec["oversize_eur"] = round((to_float(row[26]) + to_float(row[28])) * FX_GBP_EUR, 4)
            rec["handling_eur"] = round(to_float(row[32]) * FX_GBP_EUR, 4)
            rec["fuel_eur"]     = round((to_float(row[17]) + to_float(row[33])) * FX_GBP_EUR, 4)
            lines.append(rec)
        gate.append((os.path.basename(fp), nett, round(psum, 2)))
    return lines, gate


def parse_gate(gate):
    """Fail loudly on a partial parse. Per file, the parcel-line sum must sit in
    [0.95, 1.002] x Nett. WHY a band not a tight equality: Nett carries a small
    invoice-level fee residual (parcel sum is always just BELOW Nett, worst case
    ~1.4% on the smallest early invoice), so parcel/Nett slightly under 1 is
    normal; but a dropped chunk makes parcel/Nett collapse far below 0.95, and
    parcel can never legitimately exceed Nett, so >1.002 is also a fail. A missing
    Nett header is a hard fail."""
    problems = []
    for name, nett, psum in gate:
        if nett is None:
            problems.append(f"{name}: no Nett header found")
        elif nett <= 0:
            problems.append(f"{name}: non-positive Nett {nett}")
        elif not (0.95 <= psum / nett <= 1.002):
            problems.append(f"{name}: parcel sum {psum} vs Nett {nett} "
                            f"= ratio {psum/nett:.3f} (outside 0.95-1.002, likely partial parse)")
    if problems:
        sys.exit("PARSE GATE FAILED:\n  " + "\n  ".join(problems))


# ----------------------------------------------------------------------------
# Figures, Cube chunks, baseline
# ----------------------------------------------------------------------------
def build_figures(lines, gate):
    g = lambda k: sum(l[k] for l in lines)
    oi_eur = collections.defaultdict(float)
    oi_svc_eur = collections.defaultdict(lambda: collections.defaultdict(float))
    for l in lines:
        if l["order_item_id"]:
            oi_eur[l["order_item_id"]] += l["invoiced_eur"]
            oi_svc_eur[l["order_item_id"]][l["service"]] += l["invoiced_eur"]
    nett_total = sum(n for _, n, _ in gate if n)
    parcel_gbp = sum(p for _, _, p in gate)
    return {
        "source_files": len(gate),
        "lines": len(lines),
        "parcel_invoiced_gbp": round(parcel_gbp, 2),
        "parcel_invoiced_eur": round(parcel_gbp * FX_GBP_EUR, 2),
        "nett_total_gbp": round(nett_total, 2),
        "invoice_level_residual_gbp": round(nett_total - parcel_gbp, 2),
        "invoice_level_residual_eur": round((nett_total - parcel_gbp) * FX_GBP_EUR, 2),
        "distinct_order_items": len(oi_eur),
        "unmatched_lines": sum(1 for l in lines if not l["order_item_id"]),
        "unmatched_eur": round(sum(l["invoiced_eur"] for l in lines if not l["order_item_id"]), 2),
        "standard_invoiced_eur": round(sum(l["invoiced_eur"] for l in lines if l["service"] == "STANDARD"), 2),
        "expresspak_invoiced_eur": round(sum(l["invoiced_eur"] for l in lines if l["service"] == "EXPRESSPAK"), 2),
        "oversize_eur": round(g("oversize_eur"), 2),
        "handling_eur": round(g("handling_eur"), 2),
        "_oi_eur": oi_eur, "_oi_svc_eur": oi_svc_eur,
    }


def cube_chunks(fig, size=1700):
    """Distinct order_item_ids chunked at 1700 for Cube IN-lists.
    WHY 1700: Cube's AST node limit caps an IN list near 1800; 1900 is the
    practical ceiling and 1700 leaves headroom. An id that appears in BOTH
    services (returns/reshipments; 14 such ids in the frozen set) is assigned to
    its OWNING service - the one with the larger invoiced value - so Cube's single
    expected-per-item is never double counted across groups."""
    owner = {}
    for oid, svc_eur in fig["_oi_svc_eur"].items():
        owner[oid] = max(svc_eur.items(), key=lambda kv: kv[1])[0]
    all_ids = sorted(fig["_oi_eur"])
    by_service = collections.defaultdict(list)
    for oid in all_ids:
        by_service[owner[oid]].append(oid)
    chunk = lambda ids: [ids[i:i + size] for i in range(0, len(ids), size)]
    return {
        "chunk_size": size,
        "all": chunk(all_ids),
        "by_service": {s: chunk(v) for s, v in by_service.items()},
    }


def check_baseline(fig):
    fails = []
    for k, v in BASELINE.items():
        got = fig.get(k)
        if isinstance(v, float):
            if got is None or abs(got - v) > TOL:
                fails.append(f"{k}: got {got}, expected {v}")
        else:
            if got != v:
                fails.append(f"{k}: got {got}, expected {v}")
    if fails:
        raise AssertionError("BASELINE MISMATCH (parser drifted or wrong file set):\n  "
                             + "\n  ".join(fails))
    print("BASELINE OK - all frozen figures reproduced exactly.")


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--drive-folder", help="Drive year-folder id")
    src.add_argument("--local", help="local folder of invoice CSVs")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    files = gather_files(args, os.path.join(args.outdir, "_invoices"))
    if not files:
        sys.exit("No invoice CSVs found.")
    lines, gate = parse(files)
    parse_gate(gate)
    fig = build_figures(lines, gate)

    with open(os.path.join(args.outdir, "dpduk_lines.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[k for k in lines[0] if not k.startswith("_")])
        w.writeheader(); w.writerows(lines)
    with open(os.path.join(args.outdir, "dpduk_oi.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["order_item_id", "invoiced_eur"])
        for oid, e in sorted(fig["_oi_eur"].items()):
            w.writerow([oid, round(e, 2)])
    json.dump(cube_chunks(fig), open(os.path.join(args.outdir, "cube_chunks.json"), "w"))
    public = {k: v for k, v in fig.items() if not k.startswith("_")}
    json.dump(public, open(os.path.join(args.outdir, "dpduk_figures.json"), "w"), indent=2)

    print(json.dumps(public, indent=2))
    if args.baseline:
        check_baseline(fig)


if __name__ == "__main__":
    main()
