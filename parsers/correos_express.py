"""Correos Express parser (noreply@b2bconecta.com) - FAC_UNICO .xlsx.
Row where col0 starts 'F' = invoice summary (col5 TOTAL, col7 NETO).
Detail rows (col0 numeric tracking): col3 REFERENCIA=<order>-<orderitem>,
col9 PORTE=base price, col6 PESO. Control = summary NETO.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line
import openpyxl

def _f(v):
    try: return float(str(v).replace(',', '.'))
    except: return 0.0

def parse(path, carrier="correos_express"):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    out = []; inv_id = None; inv_date = None; neto = 0.0
    for r in ws.iter_rows(values_only=True):
        if not r or r[0] is None: continue
        c0 = str(r[0]).strip()
        if c0.startswith("F") and len(r) > 7 and r[1] and str(r[1]).strip().isdigit():
            inv_id = c0
            d = str(r[1]).strip()  # DDMMYYYY
            inv_date = f"{d[4:8]}-{d[2:4]}-{d[0:2]}" if len(d) == 8 else None
            neto += _f(r[7])
            continue
        if c0.isdigit() and len(c0) >= 12 and len(r) > 9:   # detail row
            ref = str(r[3] or "").strip()
            oid, oiid = (ref.split("-", 1) + [None])[:2] if "-" in ref else (None, ref or None)
            out.append(Line(carrier=carrier, invoice_id=inv_id, invoice_date=inv_date,
                order_id=oid, order_item_id=oiid, tracking=c0, ship_date=None,
                dest_country="ES", dest_region=None, weight_kg=_f(r[6]), product=None,
                charge_type="base", charge_label="porte", amount=_f(r[9]),
                currency="EUR", source_file=os.path.basename(path)))
    base = sum(l.amount for l in out)
    resid = round(neto - base, 2)
    if abs(resid) > 0.01 and out:
        m = out[0]
        out.append(Line(carrier=carrier, invoice_id=m.invoice_id, invoice_date=m.invoice_date,
            order_id=None, order_item_id=None, tracking=None, ship_date=None, dest_country="ES",
            dest_region=None, weight_kg=None, product=None, charge_type="surcharge",
            charge_label="invoice surcharges (residual to NETO)", amount=resid,
            currency="EUR", source_file=m.source_file))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, round(sum(l.amount for l in parse(p)), 2))
