"""DPD FR parser (general@dpd076.dpd.fr) - complement .xlsx.
One row per parcel. col20 'Votre référence 1' = <order>-<orderitem>,
col47 'Prix cumulé' = line total (base + surcharges), col33 'Prix transport' base,
col39 'Indexation gasoil' fuel, col23 'Date expédition' ship date.
Control = sum(Prix cumulé).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line
import openpyxl

def _f(v):
    try: return float(str(v).replace(',', '.'))
    except: return 0.0

def parse(path, carrier="dpd_fr"):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    hdr = next(rows)
    idx = {h: i for i, h in enumerate(hdr)}
    def g(row, name): 
        i = idx.get(name); return row[i] if i is not None and i < len(row) else None
    out = []
    for r in rows:
        inv = g(r, "No facture")
        if inv is None or not str(inv).isdigit() or len(str(inv)) < 10: continue
        ref = str(g(r, "Votre référence 1") or "").strip()
        oid, oiid = (ref.split("-", 1) + [None])[:2] if "-" in ref else (None, ref or None)
        sd = g(r, "Date expédition")
        ship = str(sd)[:10] if sd else None
        total = _f(g(r, "Prix cumulé"))
        if total == 0: continue
        invd = str(g(r, "Date de facture"))[:10]
        out.append(Line(carrier=carrier, invoice_id=str(inv), invoice_date=invd,
            order_id=oid, order_item_id=oiid, tracking=str(g(r, "N° Colis") or "") or None,
            ship_date=ship, dest_country=str(g(r, "Code pays destinataire") or "") or None,
            dest_region=str(g(r, "CP destinataire") or "")[:2] or None,
            weight_kg=_f(g(r, "Poids")), product=None,
            charge_type="total", charge_label="prix cumulé", amount=total,
            currency="EUR", source_file=os.path.basename(path)))
    return out

if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, round(sum(l.amount for l in parse(p)), 2))
