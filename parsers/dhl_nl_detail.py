"""DHL eCommerce NL invoice parser — per-shipment, per-surcharge, with packages + origin/destination.

Input: the eCommerce CSV ("Document <invoice>.csv"), ';'-delimited, latin-1.
Line Type 'I' = invoice header, 'S' = per-shipment detail.
Per shipment: base = Weight Charge; surcharges = Invoice Fee + Other Charges 1/2 + XC1..XC9 (named,
raw code kept) + Discounts (negative). Total Amount (net, ex-VAT) = sum of those. Packages = Pieces.
order_item_id = 2nd token of Shipment Reference 1 (<order>-<orderitem>). Returns (lines, meta).
"""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line

def _f(s):
    s = (s or "").strip()
    try: return float(s.replace(',', '.')) if s else 0.0
    except: return 0.0

def _d8(s):
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else None

def parse_full(path, carrier="dhl_nl"):
    rows = list(csv.reader(open(path, encoding="latin-1"), delimiter=';'))
    if not rows: return [], {}
    H = {h.strip(): i for i, h in enumerate(rows[0])}
    def g(r, n):
        i = H.get(n); return (r[i].strip() if i is not None and i < len(r) else "")
    inv_id = None; inv_date = None
    out = []; meta = {}
    for r in rows[1:]:
        lt = g(r, "Line Type")
        if lt == "I":
            inv_id = g(r, "Invoice Number") or inv_id
            inv_date = _d8(g(r, "Invoice Date")) or inv_date
            continue
        if lt != "S":
            continue
        iid = g(r, "Invoice Number") or inv_id
        idt = _d8(g(r, "Invoice Date")) or inv_date
        ref = g(r, "Shipment Reference 1")
        oid, oiid = (ref.split("-", 1) + [None])[:2] if "-" in ref else (None, ref or None)
        track = g(r, "Shipment Number") or None
        ship = _d8(g(r, "Shipment Date"))
        dest_cc = g(r, "Dest Country Code") or None
        dest_pc = g(r, "Receivers Postcode") or None
        orig_cc = g(r, "Orig Country Code") or None
        orig_pc = g(r, "Senders Postcode") or None
        pieces = g(r, "Pieces")
        pack = int(pieces) if pieces.isdigit() else 1
        weight = _f(g(r, "Weight (kg)"))
        product = g(r, "Product Name") or None
        total_amt = _f(g(r, "Total Amount"))
        cur = "EUR"
        base = dict(carrier=carrier, invoice_id=iid, invoice_date=idt, order_id=oid, order_item_id=oiid,
                    tracking=track, ship_date=ship, dest_country=dest_cc, dest_region=dest_pc,
                    weight_kg=weight, product=product, currency=cur, source_file=os.path.basename(path))
        meta[track or ref] = dict(pack=pack, orig_cc=orig_cc, orig_pc=orig_pc, dest_cc=dest_cc,
                                  dest_pc=dest_pc, ref=ref, product=product)
        comp = []
        wc = _f(g(r, "Weight Charge"))
        comp.append(("base", "weight charge", wc))
        fee = _f(g(r, "Invoice Fee"))
        if abs(fee) > 0.005: comp.append(("surcharge", "Invoice Fee", fee))
        for oc, ocn in [("Other Charges 1", "Other Charges 1 Amount"), ("Other Charges 2", "Other Charges 2 Amount")]:
            nm = g(r, oc); am = _f(g(r, ocn))
            if abs(am) > 0.005: comp.append(("surcharge", nm or oc, am))
        for k in range(1, 10):
            nm = g(r, f"XC{k} Name"); tot = _f(g(r, f"XC{k} Total"))
            if not nm and not tot: continue
            if abs(tot) > 0.005: comp.append(("surcharge", nm or f"XC{k}", tot))
        for dn, da in [("Discount 1", "Discount 1 Amount"), ("Discount 2", "Discount 2 Amount"), ("Discount 3", "Discount 3 Amount")]:
            nm = g(r, dn); am = _f(g(r, da))
            if abs(am) > 0.005: comp.append(("discount", nm or dn, am))
        # residual to tie the shipment to Total Amount (rounding / uncategorised)
        s = round(sum(a for _, _, a in comp), 2)
        resid = round(total_amt - s, 2)
        if abs(resid) > 0.01:
            comp.append(("surcharge", "residual to total", resid))
        for ct, lbl, amt in comp:
            out.append(Line(charge_type=ct, charge_label=lbl, amount=amt, **base))
    return out, meta

def parse(path, **kw):
    return parse_full(path, **kw)[0]

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls, meta = parse_full(p)
        ship = {l.tracking for l in ls}
        surch = [l for l in ls if l.charge_type == 'surcharge']
        from collections import Counter
        cc = Counter(l.charge_label for l in surch)
        print(os.path.basename(p), "total=", round(sum(l.amount for l in ls), 2),
              "shipments=", len(ship), "surcharge_lines=", len(surch),
              "top_surch=", dict(cc.most_common(6)))
