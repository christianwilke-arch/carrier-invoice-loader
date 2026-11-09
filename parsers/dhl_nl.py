"""DHL eCommerce NL invoice parser (ecommercebilling-nl@dhl.com).

Input: the eCommerce CSV attachment ("Document <invoice>.csv"), ';'-delimited,
latin-1. Line Type 'I' = invoice summary, 'S' = per-shipment detail.
This CSV carries the full Europlus volume (Product Name "DHL Europlus ...") plus
parcel/pallet, and Shipment Reference 1 = our order ref <order>-<orderitem>.
Total Amount = net ex-VAT (Total Charge = Amount + Tax). Bucket by Shipment Date.
"""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line

def _f(s):
    s = (s or "").strip()
    try: return float(s.replace(',', '.')) if s else 0.0
    except: return 0.0

def parse(path, carrier="dhl_nl"):
    rows = list(csv.reader(open(path, encoding="latin-1"), delimiter=';'))
    if not rows: return []
    hdr = {h.strip(): i for i, h in enumerate(rows[0])}
    def c(name): return hdr.get(name)
    inv_id = None
    out = []
    for r in rows[1:]:
        if len(r) <= c("Total Amount"): continue
        lt = r[c("Line Type")].strip()
        if lt == "I":
            inv_id = r[c("Invoice Number")].strip(); continue
        if lt != "S": continue
        sd = r[c("Shipment Date")].strip()          # YYYYMMDD
        ship = f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) == 8 and sd.isdigit() else None
        ref = (r[c("Shipment Reference 1")] or "").strip()
        oid, oiid = (ref.split("-", 1) + [None])[:2] if "-" in ref else (None, ref or None)
        amt = _f(r[c("Total Amount")])
        inv_date = r[c("Invoice Date")].strip()
        inv_date = f"{inv_date[:4]}-{inv_date[4:6]}-{inv_date[6:8]}" if len(inv_date) == 8 and inv_date.isdigit() else None
        out.append(Line(
            carrier=carrier, invoice_id=inv_id or r[c("Invoice Number")].strip(),
            invoice_date=inv_date, order_id=oid, order_item_id=oiid,
            tracking=r[c("Shipment Number")].strip() or None, ship_date=ship,
            dest_country=(r[c("Dest Country Code")] or "").strip() or None,
            dest_region=(r[c("Receivers Postcode")] or "").strip()[:2] or None,
            weight_kg=_f(r[c("Weight (kg)")]) if c("Weight (kg)") else None,
            product=(r[c("Product Name")] or "").strip() or None,
            charge_type="total", charge_label="shipment_net", amount=amt,
            currency=(r[c("Currency")] or "EUR").strip(), source_file=os.path.basename(path)))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls = parse(p); print(p, len(ls), "lines", round(sum(l.amount for l in ls), 2))
