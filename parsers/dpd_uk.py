"""DPD UK invoice parser (andy.porch@dpd.co.uk) - the 42-col CSV attachment
(670157.<inv>.csv; a PDF is attached too but the CSV is authoritative).
Header rows carry "Nett Invoice Value" (GBP control). Parcel rows: Senders Ref
= <order>-<orderitem> reconciliation key; line nett = sum of charge columns from
"Revenue" to the end. GBP->EUR at 1.1564 (frozen rate, matches dpduk audit baseline).
"""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line

GBP_EUR = 1.1564

def _f(s):
    try: return float(str(s).replace(',', '').strip())
    except: return 0.0

def parse(path, carrier="dpd_uk", fx=GBP_EUR):
    rows = list(csv.reader(open(path, encoding="utf-8", errors="replace")))
    inv_id = None; nett_ctrl = None; hdr_i = None
    for i, r in enumerate(rows):
        if len(r) >= 4 and r[0] == "Account No": inv_id = r[3]
        if r and r[0] == "Nett Invoice Value":
            nett_ctrl = _f(r[2]) if len(r) > 2 else None
        if r and r[0] == "Date" and "Consignment" in r:
            hdr_i = i; break
    if hdr_i is None: return []
    hdr = rows[hdr_i]
    idx = {h: j for j, h in enumerate(hdr)}
    rev = idx.get("Revenue")
    out = []
    for r in rows[hdr_i+1:]:
        if len(r) <= rev or not r[idx.get("Consignment", 1)]: continue
        if r[0] == "Nett Invoice Value": break
        last = idx.get('Cover', 35)
        nett_gbp = sum(_f(r[j]) for j in range(rev, last+1) if j < len(r))
        if nett_gbp == 0: continue
        ref = (r[idx["Senders Ref"]] if idx.get("Senders Ref") is not None and idx["Senders Ref"] < len(r) else "").strip()
        # Senders Ref 2nd numeric token is the order_item (handles <order>-<item>-<n>)
        toks = [t for t in ref.replace('/', '-').split('-') if t.isdigit()]
        oid = toks[0] if toks else None
        oiid = toks[1] if len(toks) > 1 else (toks[0] if toks else None)
        d = r[0].strip()
        out.append(Line(carrier=carrier, invoice_id=inv_id, invoice_date=None,
            order_id=oid, order_item_id=oiid,
            tracking=r[idx.get("Consignment", 1)].strip() or None, ship_date=None,
            dest_country="GB", dest_region=None,
            weight_kg=_f(r[idx["Weight"]]) if idx.get("Weight") is not None else None,
            product=(r[idx["Service Code"]].strip() if idx.get("Service Code") is not None and idx["Service Code"] < len(r) else None),
            charge_type="total", charge_label="line nett (EUR)", amount=round(nett_gbp*fx, 2),
            currency="EUR", source_file=os.path.basename(path)))
    # invoice-level residual (Nett control - sum of parcel lines)
    if nett_ctrl is not None and out:
        resid = round(nett_ctrl - sum(l.amount for l in out)/fx, 2)
        if abs(resid) > 0.01:
            m = out[0]
            out.append(Line(carrier=carrier, invoice_id=inv_id, invoice_date=None,
                order_id=None, order_item_id=None, tracking=None, ship_date=None,
                dest_country="GB", dest_region=None, weight_kg=None, product=None,
                charge_type="surcharge", charge_label="account-level residual (EUR)",
                amount=round(resid*fx, 2), currency="EUR", source_file=m.source_file))
    return out

def control_gbp(path):
    for r in csv.reader(open(path, encoding="utf-8", errors="replace")):
        if r and r[0] == "Nett Invoice Value": return _f(r[2])
    return None

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls = parse(p)
        print(p, "lines", len(ls), "EUR", round(sum(l.amount for l in ls), 2),
              "| GBP", round(sum(l.amount for l in ls)/GBP_EUR, 2), "ctrl GBP", control_gbp(p))
