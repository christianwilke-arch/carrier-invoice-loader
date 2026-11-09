"""Geodis Distribution & Express (FR) invoice parser.

Input: invoice PDF (geodis.d-e@e-facture.net) or its pdftotext -layout .txt (UTF-8).
"Votre référence" = our order ref <order_id>-<order_item_id> (line-level reconcilable).

Per-shipment main row (pdftotext -layout):
  DDMM CODE <order>-<orderitem> <recepisse> <dest FR-dd-city> ... <colis> <kg> <transport> [<options>] <total>
Money cols = Transport | Options | Total (total = transport + options).
Invoice-level surcharges (energie/sûreté/inflation) are reconciled as the residual
HT - sum(base+options) so the file always ties to its Débit total control figure.
"""
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line, pdf_to_text

NUM  = re.compile(r'(\d[\d. ]*,\d{2})')
MAIN = re.compile(r'\s*(\d{4})\s+([A-Z]{2,4})\s+(\d{6,8})-(\d{6,8})\s+\d+\s+(.*)')
DEPT = re.compile(r'FR-(\d{2,3})-')
EURV = re.compile(r'(\d[\d. ]*,\d{2})\s*EUR')

def _eur(s): return float(s.replace(' ', '').replace('.', '').replace(',', '.'))

def _read(path):
    if path.lower().endswith(".pdf"): return pdf_to_text(path)
    return open(path, encoding="utf-8", errors="replace").read()

def parse(path, invoice_id=None, invoice_date=None, carrier="geodis"):
    text = _read(path)
    lines_txt = text.splitlines()
    invoice_id = invoice_id or (re.search(r'FACTURE\s*:?\s*n[°o]?\s*(\d+)', text) or [None, None])[1]
    md = re.search(r'Date\s*:?\s*(\d{2})/(\d{2})/(\d{4})', text)
    invoice_date = invoice_date or (f"{md.group(3)}-{md.group(2)}-{md.group(1)}" if md else None)
    year = invoice_date[:4] if invoice_date else "2026"
    out = []
    base_opt = 0.0
    for raw in lines_txt:
        m = MAIN.match(raw.rstrip())
        if not m: continue
        ddmm, code, oid, oiid, rest = m.groups()
        nums = NUM.findall(raw)
        if len(nums) < 2: continue
        weight, total = _eur(nums[0]), _eur(nums[-1])
        transport = _eur(nums[1]) if len(nums) >= 3 else total
        options = round(total - transport, 2)
        dept = DEPT.search(rest)
        base = dict(carrier=carrier, invoice_id=invoice_id, invoice_date=invoice_date,
                    order_id=oid, order_item_id=oiid, tracking=None,
                    ship_date=f"{year}-{ddmm[2:]}-{ddmm[:2]}", dest_country="FR",
                    dest_region=dept.group(1) if dept else None, weight_kg=weight,
                    product=code, currency="EUR", source_file=os.path.basename(path))
        out.append(Line(charge_type="base", charge_label="transport", amount=transport, **base))
        base_opt += transport
        if abs(options) > 0.001:
            out.append(Line(charge_type="option", charge_label="options", amount=options, **base))
            base_opt += options
    # invoice HT control (Debit total; VAT-exempt so = Montant Taxable HT block)
    ht = None
    for i, raw in enumerate(lines_txt):
        if "Montant Taxable HT" in raw:
            for j in range(i+1, min(i+4, len(lines_txt))):
                vs = EURV.findall(lines_txt[j])
                if vs: ht = max(_eur(v) for v in vs); break
            if ht: break
    if ht is not None:
        resid = round(ht - base_opt, 2)
        if abs(resid) > 0.01:
            out.append(Line(carrier=carrier, invoice_id=invoice_id, invoice_date=invoice_date,
                order_id=None, order_item_id=None, tracking=None, ship_date=None,
                dest_country="FR", dest_region=None, weight_kg=None, product=None,
                charge_type="surcharge", charge_label="energie+surete+inflation (residual to HT)",
                amount=resid, currency="EUR", source_file=os.path.basename(path)))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls = parse(p)
        print(p, "HT=", round(sum(l.amount for l in ls),2))
