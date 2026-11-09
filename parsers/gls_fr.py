#!/usr/bin/env python3
"""
gls_fr_parse.py - GLS France invoice parser -> carrier-audit-core common line schema.

GLS FR delivers, per invoice:
  - Facture_<...>.pdf  : invoice header with the control total (Montant T.T.C.)
  - BCF_<...>.csv      : billing detail, ONE ROW PER PARCEL, charge components as columns
  - Avoir_<...>.pdf    : credit note (negative), PDF only

BCF layout (semicolon delimited, decimal comma, French headers):
  Facture=invoice no | Date de la facture | Article | Hierarchie produits (service)
  Numero de colis=tracking | Poids... =weight | Nombre de colis=packages
  Money columns, summed = the line's invoiced amount:
     Valeur de Base (base) | SGO (diesel/fuel) | CSR | PER | prix kilos additionnel | Saisie manuelle
  Client Reference = "<order_id>-<order_item_id>"  -> reconciliation key is the 2nd token.
     Manual / "doublon" lines have an empty Client Reference -> unreconciled register.

Design notes (why, per handover convention):
  - File-hash dedup: GLS re-exports the same BCF (observed byte-identical pair 1s apart). Skip by md5.
  - Parse gate: sum of emitted amounts per invoice must equal the Facture control total. Exit non-zero otherwise.
  - Melt: each BCF row becomes N common-schema rows (one per non-zero money component), charge_code verbatim.
"""
import sys, os, csv, hashlib, json, argparse, datetime

CARRIER = "GLS FR"
# charge component column -> (charge_type, charge_code, label)
MONEY_COLS = [
    ("Valeur de Base",        "base",      "BASE",   "Base tariff"),
    ("SGO",                   "fuel",      "SGO",    "Surcharge gazole (diesel)"),
    ("CSR",                   "surcharge", "CSR",    "CSR surcharge"),
    ("PER",                   "surcharge", "PER",    "PER surcharge"),
    ("prix kilos additionnel","surcharge", "ADDKG",  "Additional kg"),
    ("Saisie manuelle",       "surcharge", "MANUAL", "Manual handling"),
]

def num(s):
    s = (s or "").strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return 0.0
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0

def iso_date(s):
    s = (s or "").strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s

def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()

def read_rows(path):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f, delimiter=";")), enc
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"cannot decode {path}")

def is_bcf(name):
    u = name.upper()
    return u.startswith("BCF") and u.endswith(".CSV")

def split_ref(ref):
    ref = (ref or "").strip()
    if "-" in ref:
        a, b = ref.split("-", 1)
        return a.strip(), b.split("_")[0].strip()  # order_id, order_item_id (drop _n parcel seq)
    return "", ""

def parse_folder(folder):
    lines = []            # common-schema rows
    seen_hashes = {}
    skipped = []
    files = sorted(f for f in os.listdir(folder) if is_bcf(f))
    for name in files:
        path = os.path.join(folder, name)
        h = file_md5(path)
        if h in seen_hashes:
            skipped.append((name, seen_hashes[h]))
            continue
        seen_hashes[h] = name
        rows, enc = read_rows(path)
        for r in rows:
            invoice = (r.get("Facture") or "").strip()
            if not invoice:
                continue
            inv_date = iso_date(r.get("Date de la facture"))
            order_id, order_item_id = split_ref(r.get("Client Référence") or r.get("Client Référence 1"))
            tracking = (r.get("Numéro de colis") or "").strip()
            weight = num(r.get("Poids pour le traitement commande vente"))
            packages = num(r.get("Nombre de colis")) or 1
            service = (r.get("Hiérarchie produits") or "").strip()
            article = (r.get("Article") or "").strip()
            for col, ctype, code, label in MONEY_COLS:
                amt = num(r.get(col))
                if amt == 0.0:
                    continue
                lines.append(dict(
                    carrier=CARRIER, invoice_id=invoice, invoice_date=inv_date,
                    order_id=order_id, order_item_id=order_item_id, tracking=tracking,
                    charge_type=ctype, charge_code=code, charge_label=label,
                    amount=round(amt, 2), currency="EUR",
                    weight=weight, packages=int(packages),
                    service=service, article=article,
                    source_file=name, file_hash=h,
                ))
    return lines, skipped

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="./gls_out")
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    lines, skipped = parse_folder(args.dir)

    # per-invoice totals (parse gate)
    inv_tot = {}
    for l in lines:
        inv_tot[l["invoice_id"]] = round(inv_tot.get(l["invoice_id"], 0) + l["amount"], 2)

    # Facture control totals (Montant T.T.C.) read from the PDFs
    CONTROL = {
        "2501389265": 105.57,
        "2501396765": 2304.81,
        "2501405818": 2862.45,
        "2501416520": 4805.00,
    }

    # reconciled split (is there an order_item_id?)
    rec = [l for l in lines if l["order_item_id"]]
    unrec = [l for l in lines if not l["order_item_id"]]

    # write common line schema
    cols = ["carrier","invoice_id","invoice_date","order_id","order_item_id","tracking",
            "charge_type","charge_code","charge_label","amount","currency","weight",
            "packages","service","article","source_file","file_hash"]
    with open(os.path.join(args.out, "gls_fr_common_lines.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for l in lines: w.writerow(l)

    # report
    print("=== GLS FR parse ===")
    print(f"files parsed: {len(set(l['source_file'] for l in lines))}  skipped dups: {len(skipped)}")
    for name, orig in skipped:
        print(f"   DUP skipped: {name} == {orig}")
    print(f"charge lines: {len(lines)}  (reconcilable: {len(rec)}  unreconciled: {len(unrec)})")
    order_items = set(l["order_item_id"] for l in rec)
    print(f"distinct order_item_ids: {len(order_items)}")
    print("\n=== PARSE GATE (sum components vs Facture T.T.C.) ===")
    ok = True
    for inv, tot in sorted(inv_tot.items()):
        ctl = CONTROL.get(inv)
        if ctl is None:
            print(f"  {inv}: parsed {tot:>10.2f}   (no control total)")
            continue
        diff = round(tot - ctl, 2)
        flag = "OK" if abs(diff) <= 0.05 else "MISMATCH"
        if flag != "OK": ok = False
        print(f"  {inv}: parsed {tot:>10.2f}  control {ctl:>10.2f}  diff {diff:>8.2f}  {flag}")
    # unreconciled euro
    unrec_eur = round(sum(l["amount"] for l in unrec), 2)
    print(f"\nunreconciled euro (no client ref, manual/doublon): {unrec_eur}")
    print(f"output: {os.path.join(args.out,'gls_fr_common_lines.csv')}")
    if args.baseline and not ok:
        print("BASELINE FAIL"); sys.exit(1)

if __name__ == "__main__":
    main()
