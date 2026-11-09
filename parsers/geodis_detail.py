"""Geodis D&E (FR) invoice parser — per-shipment, per-surcharge, with packages + origin/destination.

Main row (pdftotext -layout):
  <DDMM> <CODE> [<Votre référence>] <Récépissé> <destinataire name> <FR-dd-DestCity> <colis> <kg,2dec> <transport> [<options>] <total>
The line right AFTER the main row is the Expéditeur (origin depot): "<sender> FR-dd-OriginCity".
Component sub-lines that follow (one amount each) break the charge down:
  Transport <amt> -> base ; On demand / Nouvelle presentation / Livraison zones urbaines contraintes /
  Livraison grande distribution/pf log / Localités spécifiques / Traitement multicolis / ... <amt> -> surcharges (raw label kept).
Votre référence may be <order>-<item> (reconcilable) or a plain ref (kept for later Cube backfill).
Zero-amount component lines (unused return-section surcharges) are dropped. Ties to Montant Taxable HT via residual.
Returns (lines, meta) where meta[recepisse] = {pack, dest_dept, dest_city, origin_dept, votre_ref, product}.
"""
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line, pdf_to_text

NUM  = re.compile(r'(\d[\d. ]*,\d{2})')
MAIN = re.compile(r'^\s*(\d{4})\s+([A-Z]{2,4})\s+(?:([\d][\d-]{3,18}\d)\s+)?(\d{5,9})\s+(.+)')
COMP = re.compile(r'^\s{2,}([A-Za-zÀ-ÿ][\w À-ÿ\'/.&-]+?)\s{2,}(\d[\d. ]*,\d{2})\s*[A-Z]?\s*$')
DEST = re.compile(r'FR-(\d{2,3})-([A-Za-zÀ-ÿ][\w\s\'.-]*?)\s{2,}')     # dest dept + city on main row
FRLOC = re.compile(r'FR-(\d{2,3})-([A-Za-zÀ-ÿ][\w\s\'.-]*?)\s*$')      # origin depot on sender line
COLIS = re.compile(r'\s(\d{1,3})\s+\d[\d. ]*,\d{2}')                   # integer right before the weight
EURV = re.compile(r'(\d[\d. ]*,\d{2})\s*EUR')
END_DETAIL = re.compile(r'Total\s+(Messagerieplus|Retour|Expéditions|g[ée]n[ée]ral)', re.I)

def _eur(s): return float(s.replace(' ', '').replace('.', '').replace(',', '.'))

def _read(path):
    return pdf_to_text(path) if path.lower().endswith(".pdf") else open(path, encoding="utf-8", errors="replace").read()

def parse_full(path, carrier="geodis", invoice_id=None, invoice_date=None):
    text = _read(path)
    rows = text.splitlines()
    invoice_id = invoice_id or (re.search(r'FACTURE\s*:?\s*n[°o]?\s*(\d+)', text) or [None, None])[1]
    md = re.search(r'Date\s*:?\s*(\d{2})/(\d{2})/(\d{4})', text)
    invoice_date = invoice_date or (f"{md.group(3)}-{md.group(2)}-{md.group(1)}" if md else None)
    year = invoice_date[:4] if invoice_date else "2026"
    src = os.path.basename(path)
    out, total_components = [], 0.0
    cur = None
    meta = {}
    want_origin = False

    def mk(ct, lbl, amt, p):
        return Line(charge_type=ct, charge_label=lbl, amount=amt, carrier=carrier, invoice_id=invoice_id,
                    invoice_date=invoice_date, order_id=p["order_id"], order_item_id=p["order_item_id"],
                    tracking=p["tracking"], ship_date=p["ship_date"], dest_country="FR",
                    dest_region=p["dest_dept"], weight_kg=p["weight"], product=p["product"],
                    currency="EUR", source_file=src)

    for raw in rows:
        line = raw.rstrip()
        m = MAIN.match(line)
        if m:
            ddmm, code, votre_ref, recepisse, rest = m.groups()
            oid = oiid = None
            if votre_ref and '-' in str(votre_ref):
                a, b = votre_ref.split('-', 1); oid, oiid = a, b
            else:
                oid = votre_ref
            cm = COLIS.search(line); colis = int(cm.group(1)) if cm else 1
            nums = NUM.findall(line); weight = _eur(nums[0]) if nums else None
            dm = DEST.search(rest)
            dest_dept = dm.group(1) if dm else None
            dest_city = re.sub(r'\s+', ' ', dm.group(2).strip()) if dm else None
            cur = dict(order_id=oid, order_item_id=oiid, tracking=recepisse,
                       ship_date=f"{year}-{ddmm[2:]}-{ddmm[:2]}", dest_dept=dest_dept, weight=weight,
                       product=code)
            meta[recepisse] = dict(pack=colis, dest_dept=dest_dept, dest_city=dest_city,
                                   origin_dept=None, votre_ref=votre_ref, product=code)
            want_origin = True
            continue
        if END_DETAIL.search(line):
            cur = None; want_origin = False
            continue
        if want_origin and cur:
            fm = FRLOC.search(line)
            if fm:
                meta[cur["tracking"]]["origin_dept"] = fm.group(1)
                want_origin = False
                # sender line itself is not a charge; fall through (no amount anyway)
        cm = COMP.match(line)
        if cm and cur:
            label = re.sub(r'\s+', ' ', cm.group(1).strip())
            amt = _eur(cm.group(2))
            if abs(amt) < 0.005:
                continue  # drop zero-amount (unused return-section surcharges)
            low = label.lower()
            invlvl = any(k in low for k in ('energie', 'sûreté', 'surete', 'participation',
                         'gestion', 'montant de la', 'frais de', 'co2', 'affichage'))
            if label.lower().startswith('transport'):
                ct, lbl = "base", "transport"
            else:
                ct, lbl = "surcharge", label[:60]
            if invlvl:
                out.append(Line(charge_type="surcharge", charge_label=lbl, amount=amt, carrier=carrier,
                    invoice_id=invoice_id, invoice_date=invoice_date, order_id=None, order_item_id=None,
                    tracking=None, ship_date=None, dest_country="FR", dest_region=None, weight_kg=None,
                    product=None, currency="EUR", source_file=src))
            else:
                out.append(mk(ct, lbl, amt, cur))
            total_components += amt
            continue
    # tie to Montant Taxable HT via residual (invoice-level)
    ht = None
    for i, raw in enumerate(rows):
        if "Montant Taxable HT" in raw:
            for j in range(i + 1, min(i + 4, len(rows))):
                vs = EURV.findall(rows[j])
                if vs: ht = max(_eur(v) for v in vs); break
            if ht: break
    if ht is not None:
        resid = round(ht - total_components, 2)
        if abs(resid) > 0.01:
            out.append(Line(charge_type="surcharge", charge_label="energie+surete+inflation (residual to HT)",
                amount=resid, carrier=carrier, invoice_id=invoice_id, invoice_date=invoice_date,
                order_id=None, order_item_id=None, tracking=None, ship_date=None, dest_country="FR",
                dest_region=None, weight_kg=None, product=None, currency="EUR", source_file=src))
    return out, meta

def parse(path, **kw):
    return parse_full(path, **kw)[0]

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls, meta = parse_full(p)
        comp = [l for l in ls if l.tracking]
        surch = [l for l in ls if l.charge_type == 'surcharge' and l.tracking]
        sample = {k: meta[k] for k in list(meta)[:2]}
        print(os.path.basename(p), "HT=", round(sum(l.amount for l in ls), 2),
              "parcels=", len(meta), "surcharge_lines=", len(surch), "sample_meta=", sample)
