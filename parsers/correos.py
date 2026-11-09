"""Correos ES invoice parser (facturas.clientes@correos.com).
Format: ¬ (U+00AC) delimited .txt. Field 3 = record type.
Control (net, after ~53% discount) = TOTAL line "Total amount in euros".
SUBTOT "Total discount amount" = discount (negative). DET/TRAZA = detail/tracking.
Reconciliation (stage 5) on tracking per correos-invoice-audit skill.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line

def _eur(s):
    s = s.strip().replace('.', '').replace(',', '.')
    neg = s.endswith('-')
    return (-1 if neg else 1) * float(s.rstrip('-'))

def parse(path, carrier="correos"):
    out = []
    inv_id = os.path.basename(path).split('_')[0]
    net = disc = None
    inv_date = None
    for line in open(path, encoding="utf-8", errors="replace"):
        f = line.rstrip("\n").split('¬')
        if len(f) < 6: continue
        inv_id = f[0] or inv_id
        rtype = f[3].strip()
        if rtype == "TOTAL" and "Total amount" in f[4]:
            net = _eur(f[5])
        elif rtype == "SUBTOT" and "discount" in f[4].lower():
            disc = _eur(f[5])
        elif rtype == "TOTAL" and len(f) > 8 and f[8].isdigit() and len(f[8]) == 8:
            inv_date = f"{f[8][:4]}-{f[8][4:6]}-{f[8][6:8]}"
    meta = dict(carrier=carrier, invoice_id=inv_id, invoice_date=inv_date, order_id=None,
                order_item_id=None, tracking=None, ship_date=None, dest_country="ES",
                dest_region=None, weight_kg=None, product=None, currency="EUR",
                source_file=os.path.basename(path))
    if net is not None:
        out.append(Line(charge_type="total", charge_label="net amount (after discount)", amount=net, **meta))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, round(sum(l.amount for l in parse(p)), 2))
