"""Chronopost FR invoice parser (chronopost@e-facture.net).
PDF. Control total = "Net à payer <amount> EUROS" (net, ex-VAT).
Fuel = "Surcharge Carburant ... <amount>". Reconciliation (stage 5) is on tracking
(chronopost-invoice-audit skill); this emits the invoice-level net + fuel component.
"""
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line, pdf_to_text

def _eur(s): return float(s.replace(' ', '').replace('.', '').replace(',', '.'))

def parse(path, carrier="chronopost"):
    text = pdf_to_text(path) if path.lower().endswith(".pdf") else open(path, encoding="utf-8", errors="replace").read()
    net = re.search(r'Net à payer\s+([\d. ]+,\d{2})\s*EUROS', text)
    inv = re.search(r'FACTURE\s*N[°o]?\s*[:\s]*([0-9]+)', text) or re.search(r'(\d{8})', os.path.basename(path))
    md = re.search(r'(\d{2})/(\d{2})/(\d{4})', text)
    invd = f"{md.group(3)}-{md.group(2)}-{md.group(1)}" if md else None
    out = []
    meta = dict(carrier=carrier, invoice_id=inv.group(1) if inv else os.path.basename(path),
                invoice_date=invd, order_id=None, order_item_id=None, tracking=None, ship_date=None,
                dest_country="FR", dest_region=None, weight_kg=None, product=None,
                currency="EUR", source_file=os.path.basename(path))
    fuel = re.search(r'Surcharge Carburant[^\n]*?([\d. ]+,\d{2})\s*$', text, re.M)
    if net:
        total = _eur(net.group(1))
        f = _eur(fuel.group(1)) if fuel else 0.0
        out.append(Line(charge_type="base", charge_label="transport (net of fuel)", amount=round(total-f,2), **meta))
        if f: out.append(Line(charge_type="fuel", charge_label="surcharge carburant", amount=f, **meta))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls=parse(p); print(p, round(sum(l.amount for l in ls),2))
