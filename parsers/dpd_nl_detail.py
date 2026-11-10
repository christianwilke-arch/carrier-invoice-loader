"""DPD NL detailed parser (no-reply@dpd.nl, account 319301). PDF, weekly.

The DPD NL invoice is AGGREGATED: it has no parcel rows, no tracking and no order
references — only service x country base totals and a named surcharge breakdown
(Customs, Energy, Oversized/overweight, Return, Toll, Other). So DPD NL can be loaded
with named surcharges + country-level base and tied to the Gross total, but it CANNOT
be reconciled per order_item from the invoice (the data is not in it).

Returns (lines, meta{gross, invoice_id}). Base lines carry a synthetic per-service-country
'tracking' so they group as lines; surcharges are invoice-level (tracking=None).
"""
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line, pdf_to_text

def _eur(s):
    s = (s or "").strip()
    if s in ("-", "", "—"): return 0.0
    return float(s.replace(".", "").replace(" ", "").replace(",", "."))

SUR_NAMES = ["Customs surcharge", "Energy surcharge", "Oversized / overweight surcharge",
             "Return surcharge", "Toll surcharge", "Total other surcharges"]
BASE_ROW = re.compile(r"^\s*(DPD Home KP|DPD Home)\s+([A-Z]{2})\s+(\d+)\s+€\s*(\S+)\s+€\s*(\S+)\s*$")
SUR_ROW = re.compile(r"^\s*(" + "|".join(re.escape(n) for n in SUR_NAMES) +
                     r")\s+€\s*(\S+)\s+€\s*(\S+)\s*$")

def parse_full(path, carrier="dpd_nl"):
    text = pdf_to_text(path) if path.lower().endswith(".pdf") else open(path, encoding="utf-8", errors="replace").read()
    invm = re.search(r"Invoice number:\s*(\d+)", text)
    invno = invm.group(1) if invm else os.path.splitext(os.path.basename(path))[0]
    dm = re.search(r"Invoice date:\s*(\d{2})-(\d{2})-(\d{4})", text)
    invd = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else None
    gm = re.search(r"Gross total\s*€?\s*([\d.\s]+,\d{2})", text)
    gross = _eur(gm.group(1)) if gm else None

    lines = []
    for ln in text.splitlines():
        b = BASE_ROW.match(ln)
        if b:
            svc, cc, qty = b.group(1), b.group(2), int(b.group(3))
            amt = round(_eur(b.group(4)) + _eur(b.group(5)), 2)
            lines.append(Line(carrier=carrier, invoice_id=invno, invoice_date=invd, order_id=None,
                order_item_id=None, tracking=f"DPDNL-{invno}-{svc.replace(' ','')}-{cc}", ship_date=invd,
                dest_country=cc, dest_region=None, weight_kg=None, product=svc, charge_type="base",
                charge_label=f"{svc} transport {cc}", amount=amt, currency="EUR",
                source_file=os.path.basename(path)))
            continue
        s = SUR_ROW.match(ln)
        if s:
            name = s.group(1)
            amt = round(_eur(s.group(2)) + _eur(s.group(3)), 2)
            label = "Other surcharges" if name == "Total other surcharges" else name
            if abs(amt) > 0.005:
                lines.append(Line(carrier=carrier, invoice_id=invno, invoice_date=invd, order_id=None,
                    order_item_id=None, tracking=None, ship_date=invd, dest_country="NL", dest_region=None,
                    weight_kg=None, product=None, charge_type="surcharge", charge_label=label, amount=amt,
                    currency="EUR", source_file=os.path.basename(path)))
    return lines, {"gross": gross, "invoice_id": invno}

def parse(path, **kw):
    return parse_full(path, **kw)[0]

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls, meta = parse_full(p)
        base = sum(l.amount for l in ls if l.charge_type == "base")
        sur = sum(l.amount for l in ls if l.charge_type == "surcharge")
        print(p, "gross", meta["gross"], "base", round(base, 2), "sur", round(sur, 2), "sum", round(base + sur, 2))
