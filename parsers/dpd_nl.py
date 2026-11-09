"""DPD NL invoice parser (no-reply@dpd.nl, account 319301, Presta DPDINT-STANDARD).
PDF, weekly. Control = "Gross total € <amount>" (ex-VAT total; VAT then added to
Total invoice amount). Invoice date "Invoice date: dd-mm-yyyy". Bucketed by invoice
date (weekly billing). Emits invoice-level total; line detail per dpdnl skill.
"""
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line, pdf_to_text

def _eur(s): return float(s.replace('.', '').replace(' ', '').replace(',', '.'))

def parse(path, carrier="dpd_nl"):
    text = pdf_to_text(path) if path.lower().endswith(".pdf") else open(path, encoding="utf-8", errors="replace").read()
    g = re.search(r'Gross total\s*€?\s*([\d. ]+,\d{2})', text)
    md = re.search(r'Invoice date:\s*(\d{2})-(\d{2})-(\d{4})', text)
    invd = f"{md.group(3)}-{md.group(2)}-{md.group(1)}" if md else None
    inv = re.search(r'Invoice number:?\s*(\d+)', text) or re.search(r'(\d{8})', os.path.basename(path))
    out = []
    if g:
        out.append(Line(carrier=carrier, invoice_id=inv.group(1) if inv else os.path.basename(path),
            invoice_date=invd, order_id=None, order_item_id=None, tracking=None,
            ship_date=invd, dest_country="NL", dest_region=None, weight_kg=None, product=None,
            charge_type="total", charge_label="gross total (ex-VAT)", amount=_eur(g.group(1)),
            currency="EUR", source_file=os.path.basename(path)))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls=parse(p); print(p, round(sum(l.amount for l in ls),2))
