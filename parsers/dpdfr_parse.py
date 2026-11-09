#!/usr/bin/env python3
"""
dpdfr_parse.py - frozen parser for the DPD France monthly "complement" invoices.

Helloprint account 14011, invoice series 07600 26xx, Presta service level DPDFR-STANDARD,
FR to FR (a handful of BE/LU/ES/NL strays, under 0.05% of value).

WHY THIS FILE EXISTS
--------------------
The DPD FR audit was done once, by hand, with a strong model. This script freezes every
parsing rule that audit depended on, so a weaker model can rerun it and either reproduce
the agreed figures exactly or fail loudly. Do not reimplement the parsing: run this.

WHERE THE INVOICES LIVE
-----------------------
Google Drive, shared folder Carriers / Invoices / DPD FR
  carrier folder  1jO4PgQOxEDCyJaw6fBDxzWETGZBQm1Zo
  2025            1rirxAEoQsd4G1fsHBrXF5EJmuXI8-aEw   (2 files: Nov, Dec)
  2026            1ciWrVpOzEy9kTWdrY0vOu1GhyDbgCyFU   (9 files: Jan to Sep)

File naming in Drive: YYYY-MM_complement_01.MM.YYYY-DD.MM.YYYY.xlsx

NEVER use the Drive connector's download_file_content for these files. An xlsx of 1.5 MB
comes back as base64 in the conversation and costs roughly 500k tokens. Use the Drive REST
API through curl, as download_from_drive() below does - the agent proxy already
authenticates googleapis.com/drive/, so no key is needed and the bytes go straight to disk.

USAGE
-----
  python3 dpdfr_parse.py --drive 1ciWrVpOzEy9kTWdrY0vOu1GhyDbgCyFU --drive 1rirxAEoQsd4G1fsHBrXF5EJmuXI8-aEw
  python3 dpdfr_parse.py --local "/path/to/invoice/folder"
  python3 dpdfr_parse.py --drive <2026 id> --drive <2025 id> --baseline

--baseline re-asserts the figures agreed in the October 2026 audit. A NEW invoice period
WILL trip the baseline - that is by design. Run without --baseline for new data, and rerun
the original eleven files with --baseline to prove the parser still behaves.

OUTPUTS (into --out, default ./dpdfr_out)
  lines.csv            one row per invoice charge line, normalised
  by_order_item.csv    invoiced rolled up to order_item_id, with the cost components
  by_month.csv         per dispatch month
  cube_chunks.json     order_item_id chunks of 1700 for the Cube IN() queries
  parse_report.json    per-file gate result and the headline figures
"""

import argparse, json, os, re, subprocess, sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Frozen reference data
# ---------------------------------------------------------------------------

DRIVE_FOLDERS = {
    "carrier": "1jO4PgQOxEDCyJaw6fBDxzWETGZBQm1Zo",
    "2025": "1rirxAEoQsd4G1fsHBrXF5EJmuXI8-aEw",
    "2026": "1ciWrVpOzEy9kTWdrY0vOu1GhyDbgCyFU",
}

CUBE_CARRIER_NAME = "DPDFR-STANDARD"
CUBE_CHUNK_SIZE = 1700  # Cube's AST node limit caps an IN list near 1800. 3000 and 4000 both fail.

# The 39 columns that carry money. Everything else is a count, a date or an identifier.
MONEY_COLUMNS = [
    "Prix transport", "Supplément île et montagne", "Coût de la VD",
    "Taxe Collection Request", "Taxe fixe", "Taxe Consolidation",
    "Indexation gasoil", "Indexation kérosène", "Supplément prédict",
    "Supplément DPD Sécure", "Supplément DPD Consigne", "Participation Sureté",
    "Contribution Logistique Responsable", "Frais de tenue de compte",
    "Fact. statuts Absent Avisés", "Fact. Avisage par SMS", "Fact. Retour expédition",
    "Fact. statuts livraison PR", "Fac statut colis non retiré PR",
    "Fact. Statuts adresse erronée", "Fact. Colis Pos. Inventaire",
    "Fact. Etiquette client fausse", "Fact. Colis Refusés",
    "Fact. Emballage non conforme", "Supplément DPD Retour", "Supplément DPD inversé",
    "Fact. DPD Relais non retirés", "Supplément DPD Relais", "Fact. statuts Hors norme",
    "Fact. Ramasse programmée sans colis", "Fact.statut Liv.domicile privé",
    "Etiquette manuelle", "Fact. Consignation EDI manquante ou incomplète",
    "Taxe triangulaire", "Fact. Colis non mécanisable", "Fact. étiquette non lisible",
    "Fact. colis code non fourni", "Fact. statuts hors domicile déposé en consigne",
    "Frais de douane",
]

# Named cost components used in every roll-up and in the config delta.
COMPONENTS = {
    "base":      "Prix transport",
    "fuel":      "Indexation gasoil",
    "security":  "Participation Sureté",
    "clr":       "Contribution Logistique Responsable",
    "remote":    "Supplément île et montagne",
    "nonmech":   "Fact. Colis non mécanisable",
    "absent":    "Fact. statuts Absent Avisés",
    "edi":       "Fact. Consignation EDI manquante ou incomplète",
    "horsnorme": "Fact. statuts Hors norme",
    "retour":    "Fact. Retour expédition",
    "relais_nr": "Fact. DPD Relais non retirés",
    "emballage": "Fact. Emballage non conforme",
    "sms":       "Fact. Avisage par SMS",
    "acct":      "Frais de tenue de compte",
    "pr_nr":     "Fac statut colis non retiré PR",
    "badlabel":  "Fact. étiquette non lisible",
    "manlabel":  "Etiquette manuelle",
}

# Footer column positions. The footer header row is the row whose first cell reads
# "Total expéditions"; the row AFTER it carries the values, by position, not by name.
FOOTER_TOTAL_HT_IDX = 12
FOOTER_SHIPMENTS_IDX = 0

# --- frozen baseline, DPD FR audit of 15 October 2026 -----------------------
BASELINE = {
    "files": 11,
    "data_rows": 49557,
    "invoiced_all_files_eur": 334310.22,
    "distinct_order_items_all_files": 47521,
    "no_reference_lines": 87,
    "no_reference_eur": 595.10,
    # 2026 Jan-Sep, the reporting period
    "p2026_lines": 41271,
    "p2026_parcels": 40818,
    "p2026_invoiced_eur": 280633.42,
    "p2026_distinct_order_items": 39621,
    "p2026_no_reference_eur": 494.63,
    # per-file control totals as printed by DPD in the footer
    "file_totals": {
        "2025-11": 27788.18, "2025-12": 25739.37, "2026-01": 25585.47,
        "2026-02": 27724.61, "2026-03": 33540.71, "2026-04": 34032.50,
        "2026-05": 34881.13, "2026-06": 41592.65, "2026-07": 27517.12,
        "2026-08": 22528.92, "2026-09": 33379.59,
    },
    # 2026 Jan-Sep component totals, invoice side
    "p2026_components": {
        "base": 184254.05, "fuel": 35679.50, "security": 16735.38, "clr": 7347.24,
        "remote": 6462.66, "nonmech": 11178.00, "absent": 10536.00,
        "edi": 4050.50, "horsnorme": 1860.00,
    },
    "p2026_component_hits": {
        "remote": 961, "nonmech": 1863, "absent": 2598, "edi": 8101, "horsnorme": 62,
    },
    # Cube side, reconciled 15 Oct 2026. Not reproducible from the invoice alone -
    # reported here so a rerun can compare after the Cube step.
    "cube_matched_order_items": 39522,
    "cube_matched_invoiced_eur": 279499.35,
    "cube_expected_eur": 252953.62,
    "cube_gap_eur": 26545.73,
    "cube_gap_pct": 10.49,
    "unexplained_eur": 1134.07,
}


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------

def download_from_drive(folder_id: str, workdir: Path) -> list:
    """Pull every file of a Drive folder to disk with the Drive REST API.

    WHY curl and not the Drive connector: the connector returns base64 into the
    conversation. A 1.5 MB xlsx is roughly 500k tokens and ends the session. curl writes
    bytes straight to disk and the agent proxy supplies the credentials for
    googleapis.com/drive/, so no key is needed here.

    WHY we check mimeType: if Drive's "Convert uploads to Google Docs editor format" was
    on when the file was uploaded, the stored object is a Google Sheet and the original
    bytes are gone. Parsing the export silently produces different numbers, so we refuse.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    listing = subprocess.run(
        ["curl", "-sS", "-G", "https://www.googleapis.com/drive/v3/files",
         "--data-urlencode", f"q='{folder_id}' in parents and trashed=false",
         "--data-urlencode", "fields=files(id,name,size,mimeType)",
         "--data-urlencode", "supportsAllDrives=true",
         "--data-urlencode", "includeItemsFromAllDrives=true"],
        capture_output=True, text=True, check=True).stdout
    files = json.loads(listing).get("files", [])
    out = []
    for f in files:
        if f["mimeType"].startswith("application/vnd.google-apps."):
            sys.exit(
                f"ABORT: {f['name']} has mimeType {f['mimeType']}. It was converted on "
                f"upload and the original bytes are lost. Turn off 'Convert uploads to "
                f"Google Docs editor format' in Drive Settings and re-upload.")
        if not f["name"].lower().endswith((".xlsx", ".xls")):
            continue
        dest = workdir / f["name"]
        subprocess.run(
            ["curl", "-sS", "-o", str(dest),
             f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media"],
            check=True)
        out.append(dest)
    return sorted(out)


# ---------------------------------------------------------------------------
# Parsing rules - one named function each, with the reason
# ---------------------------------------------------------------------------

def split_data_and_footer(df: pd.DataFrame):
    """Separate the charge lines from DPD's own footer totals.

    WHY: every DPD FR complement file ends with two extra rows. The first has
    "Total expéditions" in column 0 and is a second header; the row after it carries the
    control totals by POSITION, not by column name (the real header names still sit above
    them). Feeding either row into the money sum double counts the whole invoice, and
    parsing the date columns on them raises DateParseError.

    Charge lines are exactly the rows where "Type (Slave Export)" == 1.
    """
    data = df[df["Type (Slave Export)"] == 1].copy()
    hdr = df.index[df["Type (Slave Export)"].astype(str) == "Total expéditions"]
    footer = df.loc[hdr[0] + 1].tolist() if len(hdr) else None
    return data, footer


def coerce_money(data: pd.DataFrame) -> pd.DataFrame:
    """Force the 39 money columns numeric and total each line.

    WHY we list the columns instead of summing everything numeric: the file also holds
    "Nombre ..." counters and, critically, "Prix cumulé". Prix cumulé is NOT additive - it
    repeats the base price for multi-parcel groups. Summing it double counts those
    shipments. It is deliberately absent from MONEY_COLUMNS.
    """
    for c in MONEY_COLUMNS:
        if c not in data.columns:
            data[c] = 0.0
        data[c] = pd.to_numeric(data[c], errors="coerce").fillna(0.0)
    for c in ["Poids", "Poids initial", "Poids volumétrique", "Nombre de colis"]:
        if c in data.columns:
            data[c] = pd.to_numeric(data[c], errors="coerce").fillna(0.0)
    data["total_line"] = data[MONEY_COLUMNS].sum(axis=1)
    return data


def extract_order_item_id(reference: pd.Series) -> pd.DataFrame:
    """Take the order_item_id from "Votre référence 1", which is order_id-order_item_id.

    WHY the SECOND token: both tokens are 7 to 9 digits and BOTH resolve as valid Cube
    order_item_ids on different rows. Keying on the first silently matches the wrong
    order items and manufactures a large fake gap. This is the same trap documented for
    the PostNL stanize.

    Some references carry a "-multi" suffix for a multi-parcel shipment; the regex ignores
    the tail. About 0.2% of invoiced value has a reference in another shape
    (e.g. "6026037.1") or none at all - those are left null and become the unreconciled
    register, never a guess.
    """
    ext = reference.astype(str).str.strip().str.extract(r"^(\d{5,9})-(\d{5,9})")
    return pd.DataFrame({"order_id": ext[0], "order_item_id": ext[1]})


def derive_dispatch_month(data: pd.DataFrame) -> pd.Series:
    """Bucket a line by "Date expédition", not by the invoice period.

    WHY: DPD bills calendar-month on dispatch, but surcharge-only lines for an earlier
    month ride along on a later invoice. Keying the period on the invoice file would move
    those euros into the wrong month and break any month-on-month comparison.
    """
    return pd.to_datetime(data["Date expédition"], errors="coerce").dt.to_period("M").astype(str)


def flag_row_type(data: pd.DataFrame) -> pd.Series:
    """Label each line parcel or surcharge_only.

    WHY this matters more than it looks: a surcharge-only line has Nombre de colis == 0
    and Prix transport == 0, and carries a value in exactly one "Fact. *" column. It is an
    event fee billed later against a parcel that was already charged in an earlier month.
    If such a line is counted as a parcel, every per-parcel figure (avg cost per parcel,
    surcharge incidence, the buffer per parcel) is understated. If it is dropped, roughly
    7% of invoiced value disappears. Keep it, but never count it as a parcel.
    """
    return pd.Series(
        ["parcel" if n > 0 else "surcharge_only" for n in data["Nombre de colis"]],
        index=data.index)


def parse_file(path: Path) -> tuple:
    """Parse one complement xlsx and run its parse gate."""
    raw = pd.read_excel(path, sheet_name=0)
    data, footer = split_data_and_footer(raw)
    data = coerce_money(data)
    ids = extract_order_item_id(data["Votre référence 1"])
    data["order_id"] = ids["order_id"].values
    data["order_item_id"] = ids["order_item_id"].values
    data["dispatch_month"] = derive_dispatch_month(data)
    data["row_type"] = flag_row_type(data)
    data["source_file"] = path.name

    control = float(footer[FOOTER_TOTAL_HT_IDX]) if footer else None
    parsed = float(data["total_line"].sum())
    gate = {
        "file": path.name,
        "rows": int(len(data)),
        "parsed_total": round(parsed, 2),
        "control_total_ht": None if control is None else round(control, 2),
        "shipments_footer": None if footer is None else int(footer[FOOTER_SHIPMENTS_IDX]),
        "ok": control is not None and abs(round(parsed, 2) - round(control, 2)) <= 0.02,
    }
    return data, gate


def build_order_item_rollup(lines: pd.DataFrame) -> pd.DataFrame:
    """Roll invoiced euros up to order_item_id."""
    d = lines[lines["order_item_id"].notna()].copy()
    d["oid"] = d["order_item_id"].astype(int)
    agg = {"invoiced": ("total_line", "sum"),
           "lines": ("total_line", "size"),
           "parcels": ("Nombre de colis", "sum"),
           "billed_kg": ("Poids", "sum"),
           "declared_kg": ("Poids initial", "sum")}
    out = d.groupby("oid").agg(**agg)
    for name, col in COMPONENTS.items():
        out[name] = d.groupby("oid")[col].sum()
    out["first_dispatch_month"] = d.groupby("oid")["dispatch_month"].min()
    return out.reset_index()


def write_cube_chunks(order_items: list, out_dir: Path) -> list:
    """Pre-split the order_item_id list for the Cube IN() queries."""
    ids = sorted(set(int(i) for i in order_items))
    chunks = [ids[i:i + CUBE_CHUNK_SIZE] for i in range(0, len(ids), CUBE_CHUNK_SIZE)]
    (out_dir / "cube_chunks.json").write_text(json.dumps(
        {"carrier_name": CUBE_CARRIER_NAME, "chunk_size": CUBE_CHUNK_SIZE,
         "total_ids": len(ids), "chunks": chunks}, indent=1))
    return chunks


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def assert_baseline(report: dict, lines: pd.DataFrame, rollup: pd.DataFrame):
    """Hard-assert the figures agreed in the 15 October 2026 audit."""
    B = BASELINE
    def eq(actual, expected, label, tol=0.01):
        assert abs(actual - expected) <= tol, f"{label}: got {actual}, baseline {expected}"

    assert report["files"] == B["files"], f"files: {report['files']} vs {B['files']}"
    assert report["data_rows"] == B["data_rows"], f"data rows: {report['data_rows']} vs {B['data_rows']}"
    eq(report["invoiced_all_files_eur"], B["invoiced_all_files_eur"], "invoiced all files")
    assert report["distinct_order_items_all_files"] == B["distinct_order_items_all_files"]

    noref = lines[lines["order_item_id"].isna()]
    assert len(noref) == B["no_reference_lines"], f"no-reference lines: {len(noref)}"
    eq(float(noref["total_line"].sum()), B["no_reference_eur"], "no-reference eur")

    for g in report["gates"]:
        assert g["ok"], f"parse gate failed on {g['file']}"
    for month, total in B["file_totals"].items():
        got = next(g["control_total_ht"] for g in report["gates"] if g["file"].startswith(month))
        eq(got, total, f"control total {month}")

    p = lines[(lines["dispatch_month"] >= "2026-01") & (lines["dispatch_month"] <= "2026-09")]
    assert len(p) == B["p2026_lines"], f"2026 lines: {len(p)}"
    eq(float(p["total_line"].sum()), B["p2026_invoiced_eur"], "2026 invoiced")
    assert int(p["Nombre de colis"].sum()) == B["p2026_parcels"]
    assert int(p["order_item_id"].dropna().nunique()) == B["p2026_distinct_order_items"]
    eq(float(p[p["order_item_id"].isna()]["total_line"].sum()), B["p2026_no_reference_eur"],
       "2026 no-reference eur")

    for name, total in B["p2026_components"].items():
        eq(float(p[COMPONENTS[name]].sum()), total, f"2026 component {name}")
    for name, hits in B["p2026_component_hits"].items():
        got = int((p[COMPONENTS[name]] != 0).sum())
        assert got == hits, f"2026 hits {name}: got {got}, baseline {hits}"

    print("BASELINE OK - all invoice-side figures reproduced exactly.")
    print("  Cube-side baseline for comparison after the Cube step:")
    print(f"    matched order items {B['cube_matched_order_items']}, "
          f"invoiced {B['cube_matched_invoiced_eur']}, expected {B['cube_expected_eur']}, "
          f"gap {B['cube_gap_eur']} = {B['cube_gap_pct']}%, unexplained {B['unexplained_eur']}")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drive", action="append", default=[], help="Drive folder id (repeatable)")
    ap.add_argument("--local", action="append", default=[], help="local folder path (repeatable)")
    ap.add_argument("--out", default="dpdfr_out")
    ap.add_argument("--baseline", action="store_true", help="assert the frozen audit figures")
    a = ap.parse_args()

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    paths = []
    for fid in a.drive:
        paths += download_from_drive(fid, out / "invoices")
    for p in a.local:
        paths += sorted(x for x in Path(p).rglob("*.xls*") if not x.name.startswith("~$"))
    if not paths:
        sys.exit("No invoice files found. Pass --drive <folder id> or --local <path>.")

    frames, gates = [], []
    for p in paths:
        df, g = parse_file(p)
        frames.append(df); gates.append(g)
        flag = "OK " if g["ok"] else "FAIL"
        print(f"[{flag}] {g['file']:50s} rows={g['rows']:5d} "
              f"parsed={g['parsed_total']:>12,.2f} control={g['control_total_ht']:>12,.2f}")

    bad = [g for g in gates if not g["ok"]]
    if bad:
        sys.exit(f"PARSE GATE FAILED on {len(bad)} file(s). A short parse looks exactly like a "
                 f"saving - refusing to continue. Files: {[g['file'] for g in bad]}")

    lines = pd.concat(frames, ignore_index=True)
    rollup = build_order_item_rollup(lines)
    chunks = write_cube_chunks(rollup["oid"].tolist(), out)

    lines.to_csv(out / "lines.csv", index=False)
    rollup.to_csv(out / "by_order_item.csv", index=False)
    bym = lines.groupby("dispatch_month").agg(
        lines=("total_line", "size"), parcels=("Nombre de colis", "sum"),
        invoiced=("total_line", "sum"),
        **{k: (v, "sum") for k, v in list(COMPONENTS.items())[:9]})
    bym.to_csv(out / "by_month.csv")

    report = {
        "files": len(paths),
        "data_rows": int(len(lines)),
        "invoiced_all_files_eur": round(float(lines["total_line"].sum()), 2),
        "distinct_order_items_all_files": int(lines["order_item_id"].dropna().nunique()),
        "cube_chunks": len(chunks),
        "cube_chunk_size": CUBE_CHUNK_SIZE,
        "gates": gates,
    }
    (out / "parse_report.json").write_text(json.dumps(report, indent=1))
    print(f"\n{report['data_rows']:,} lines  {report['invoiced_all_files_eur']:,.2f} EUR  "
          f"{report['distinct_order_items_all_files']:,} order items  "
          f"{len(chunks)} Cube chunks of {CUBE_CHUNK_SIZE}")
    print(f"written to {out.resolve()}")

    if a.baseline:
        assert_baseline(report, lines, rollup)


if __name__ == "__main__":
    main()
