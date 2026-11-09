"""UPS invoice parser (UPS Billing Center flat file, auto-pulled by the
christianwilke-arch/ups-invoice-poller GitHub Action -> Hub ingest).
Format: no header, 250 columns, ONE ROW PER CHARGE LINE (not per parcel).
col5 invoice no | col4 invoice date | col10 invoice total | col13 tracking |
col15 Reference 1 = <order>-<orderitem> (Cube bridge) | col43 charge class
(FRT/FSC/ACC/TAX/INF/EXM/MIS) | col45 description | col52 NET AMOUNT (sums to total).
"""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line

CLASS = {"FRT": "base", "FSC": "fuel", "ACC": "surcharge", "TAX": "tax",
         "INF": "info", "EXM": "info", "MIS": "misc"}

def f2(s):
    try: return float(s)
    except: return 0.0

def parse(path, carrier="ups"):
    rows = list(csv.reader(open(path, encoding="utf-8", errors="replace")))
    if not rows: return []
    h = rows[0]
    inv_id = h[5] if len(h) > 5 else None
    inv_date = h[4] if len(h) > 4 else None
    total = f2(h[10]) if len(h) > 10 else 0.0
    acct_country = h[3] if len(h) > 3 else None
    out = []
    emitted = 0.0
    for r in rows:
        if len(r) <= 52: continue
        amt = f2(r[52])
        cls = r[43].strip() if len(r) > 43 else ""
        if amt == 0 and cls in ("INF", "EXM"): continue  # zero-net info lines
        ref = (r[15] if len(r) > 15 else "").strip()
        toks = [t for t in ref.split("-") if t.isdigit()]
        oid = toks[0] if toks else None
        oiid = toks[1] if len(toks) > 1 else (toks[0] if toks else None)
        out.append(Line(carrier=carrier, invoice_id=inv_id, invoice_date=inv_date,
            order_id=oid, order_item_id=oiid,
            tracking=(r[13].strip() if len(r) > 13 else None) or None,
            ship_date=(r[11].strip() if len(r) > 11 else None) or None,
            dest_country=(r[81].strip() if len(r) > 81 else None) or None,
            dest_region=acct_country,
            weight_kg=f2(r[28]) if len(r) > 28 else None,
            product=(r[45].strip() if len(r) > 45 else None) or None,
            charge_type=CLASS.get(cls, "other"), charge_label=(r[45].strip() if len(r) > 45 else cls),
            amount=round(amt, 2), currency=(h[9] if len(h) > 9 else "EUR"),
            source_file=os.path.basename(path)))
        emitted += amt
    # rounding residual so the file ties to col10 invoice total
    resid = round(total - emitted, 2)
    if abs(resid) >= 0.01 and out:
        out.append(Line(carrier=carrier, invoice_id=inv_id, invoice_date=inv_date,
            order_id=None, order_item_id=None, tracking=None, ship_date=None,
            dest_country=None, dest_region=acct_country, weight_kg=None, product=None,
            charge_type="rounding", charge_label="invoice rounding", amount=resid,
            currency=(h[9] if len(h) > 9 else "EUR"), source_file=os.path.basename(path)))
    return out

def invoice_total(path):
    r = next(csv.reader(open(path, encoding="utf-8", errors="replace")), None)
    return f2(r[10]) if r and len(r) > 10 else None

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls = parse(p); print(os.path.basename(p), "lines", len(ls), "EUR", round(sum(l.amount for l in ls),2), "ctrl", invoice_total(p))
