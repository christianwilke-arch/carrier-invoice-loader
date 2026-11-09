"""DHL eCommerce BE parser (parcelbilling-be@dhl.com). PDF only.
Control per invoice = the "Total: ... <net>" line (last amount, net; BTW 0.00).
Detail rows carry dd/mm/yyyy ship dates (for stage-5 month allocation) but their
line amounts are gross/pre-discount, so the invoice-level net Total is the control.
"""
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line, pdf_to_text

def _eur(s): return float(s.replace(',', ''))  # BE uses dot-decimal

def parse(path, carrier="dhl_be"):
    text = pdf_to_text(path) if path.lower().endswith(".pdf") else open(path, encoding="utf-8", errors="replace").read()
    inv = re.search(r'(\d{6,8})', os.path.basename(path))
    # "Total:   <count>   0.00   <net>"  -> take last money token on the Total line
    net = None
    for line in text.splitlines():
        if re.search(r'\bTotal:\s', line):
            nums = re.findall(r'(\d[\d,]*\.\d{2})\b', line)
            if nums: net = _eur(nums[-1])
    md = re.search(r'_(\d{2})(\d{2})\.(?:pdf|txt)', os.path.basename(path)) or re.search(r'_(\d{2})(\d{2})$', os.path.basename(path).rsplit('.',1)[0])
    out = []
    if net is not None:
        out.append(Line(carrier=carrier, invoice_id=inv.group(1) if inv else os.path.basename(path),
            invoice_date=None, order_id=None, order_item_id=None, tracking=None, ship_date=None,
            dest_country="BE", dest_region=None, weight_kg=None, product=None,
            charge_type="total", charge_label="invoice net (Total)", amount=net,
            currency="EUR", source_file=os.path.basename(path)))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, round(sum(l.amount for l in parse(p)), 2))
