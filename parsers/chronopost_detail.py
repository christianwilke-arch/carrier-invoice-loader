"""Chronopost FR DETAILED invoice parser (per-parcel).

The detailed Chronopost invoice (8-page, e.g. 13796660) lists one row per parcel:
  <seq> <DD/MM/YYYY> <N° objet> <depart> <arrivee> [P] <poids,3dec> [<MontantHT,2dec>] <Obs> <Produit> <TVA>
'Supplément ...' lines below a parcel are that parcel's surcharges.
'Collectes à vide' / 'Enlèvements sur demande' sous-totals are invoice-level fees.
N° objet = parcel tracking = Cube order_items.track_and_trace_url (reconciliation key).
Control total = 'Net à payer <amount> EUROS' / 'TOTAL FACTURE'.
"""
import re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import Line, pdf_to_text

TRACK = re.compile(r'\b([A-Z]{2}\d{6,}[A-Z]{2})\b')
# money = exactly 2 decimals, NOT part of a 3-decimal weight and NOT spanning spaces
MONEY = re.compile(r'(?<!\d)(\d+,\d{2})(?!\d)')
WEIGHT = re.compile(r'\b\d+,\d{3}\b')               # 3-decimal = weight
DATE = re.compile(r'\b(\d{2})/(\d{2})/(\d{4})\b')
# supplements/charges may end with a TVA code (e.g. "... 2,10   Z"); capture the money, allow trailing code
SUPP = re.compile(r'Suppl[ée]ment\s+(.+?)\s+(?<!\d)(\d+,\d{2})(?!\d)(?:\s+[A-Z]{1,3})?\s*$')
# grouped-parcel transport charge line: "<N> Obj   <weight>   <amount>   GT"
GT = re.compile(r'\bObj\b.*?(?<!\d)(\d+,\d{2})(?!\d)\s+GT\b')
SOUSTOTAL = re.compile(r'Sous-Total\s+(.+?)\s+(?<!\d)(\d+,\d{2})(?!\d)(?:\s+[A-Z]{1,3})?\s*$')
# invoice-level charge lines (fuel, security, management fees, forfaits) in the summary block.
# Amount = the LAST money on the line (these lines also contain a rate % or unit price first).
CHARGELINE = re.compile(r'(Surcharge Carburant|Participation.*?s[ûu]ret[ée]|Frais de gestion|Redevance s[ûu]ret[ée]|Forfait\s+Exp|Traitement Retour)', re.I)
LASTMONEY = re.compile(r'(?<!\d)(\d+,\d{2})(?!\d)')
NETAPAYER = re.compile(r'Net\s+à\s+payer\s+(\d[\d ]*,\d{2})')

def _eur(s): return float(s.replace(' ', '').replace('.', '').replace(',', '.'))

# OBS / flag legend (from the invoice): maps the raw code to its meaning
OBS_MAP = {
    'S': 'Redevance sûreté', 'V': 'Poids Volumétrique', 'P': 'Poids corrigé après contrôle',
    'RR': 'Retour Expéditeur', 'GT': 'Groupage Tarifaire', 'R': 'R (confirm with Chronopost)',
}

def parse(path, carrier="chronopost"):
    text = pdf_to_text(path) if path.lower().endswith(".pdf") else open(path, encoding="utf-8", errors="replace").read()
    rows = text.splitlines()
    inv = (re.search(r'(?:FACTURE|Avoir)\s*N?[°o]?\s*[:\s]*([0-9]{6,})', text) or [None, None])[1] \
          or re.search(r'(\d{8})', os.path.basename(path)).group(1)
    md = DATE.search(text)
    invd = f"{md.group(3)}-{md.group(2)}-{md.group(1)}" if md else None
    ctrl = NETAPAYER.search(text)
    control = _eur(ctrl.group(1)) if ctrl else None

    def meta(**kw):
        base = dict(carrier=carrier, invoice_id=inv, invoice_date=invd, order_id=None,
                    tracking=None, dest_country="FR", dest_region=None, weight_kg=None,
                    product=None, currency="EUR", source_file=os.path.basename(path))
        base.update(kw); return base

    out = []
    PMETA = {}   # tracking -> {depart, arrivee, obs, produit, pack}
    cur = None  # current parcel's order_item_id placeholder (track)
    for raw in rows:
        tr = TRACK.search(raw)
        dt = DATE.search(raw)
        if tr and dt:  # parcel detail row
            track = tr.group(1)
            # weight = 3-dec; money on the row (if any) = Montant HT
            wt = WEIGHT.search(raw)
            weight = float(wt.group(0).replace(',', '.')) if wt else None
            monies = MONEY.findall(raw)
            amt = _eur(monies[-1]) if monies else 0.0
            sd = f"{dt.group(3)}-{dt.group(2)}-{dt.group(1)}"
            # Départ + Arrivée postcodes right after the N° objet (origin -> destination)
            rm = re.search(re.escape(track) + r'\s+(\d{4,5})\s+(\d{4,5})', raw)
            depart = rm.group(1) if rm else None
            arrivee = rm.group(2) if rm else None
            # Obs (S/R/V/RR flag) + Produit (service code C18/C10) near the row end
            pm = re.search(r'\b([SRV]{1,2})\s+(C\d{1,3})\b', raw)
            obs = pm.group(1) if pm else None
            produit = pm.group(2) if pm else None
            PMETA[track] = dict(depart=depart, arrivee=arrivee, obs=obs, produit=produit, pack=1)
            out.append(Line(**meta(order_item_id=track, tracking=track, ship_date=sd, weight_kg=weight,
                                   product=produit, dest_region=arrivee, charge_type="base",
                                   charge_label="transport", amount=amt)))
            # preserve the raw per-parcel codes (service + obs, decoded) as zero-amount code rows
            if produit:
                out.append(Line(**meta(order_item_id=track, tracking=track, ship_date=sd, product=produit,
                                       charge_type="code", charge_label=f"PRODUIT:{produit}", amount=0.0)))
            if obs:
                out.append(Line(**meta(order_item_id=track, tracking=track, ship_date=sd, product=produit,
                                       charge_type="code", charge_label=f"OBS:{obs} ({OBS_MAP.get(obs,'?')})", amount=0.0)))
            cur = track
            continue
        gt = GT.search(raw)
        if gt and cur:  # grouped-parcel transport charge → attach to the parcel above as base
            out.append(Line(**meta(order_item_id=cur, tracking=cur, ship_date=None,
                                   charge_type="base", charge_label="groupage transport",
                                   amount=_eur(gt.group(1)))))
            continue
        sm = SUPP.search(raw)
        if sm and cur:  # surcharge belongs to the parcel above
            out.append(Line(**meta(order_item_id=cur, tracking=cur, ship_date=None,
                                   charge_type="surcharge", charge_label=sm.group(1).strip()[:60],
                                   amount=_eur(sm.group(2)))))
            continue
        st = SOUSTOTAL.search(raw)
        if st and 'TVA' not in raw:  # invoice-level fee bucket (collectes à vide, enlèvements)
            lbl = st.group(1).strip().lower()
            if any(k in lbl for k in ('collecte', 'enl', 'demande', 'vide')):
                out.append(Line(**meta(order_item_id=None, ship_date=None, charge_type="option",
                                       charge_label=st.group(1).strip()[:60], amount=_eur(st.group(2)))))
            continue
        cl = CHARGELINE.search(raw)
        if cl and 'Total' not in raw:  # invoice-level fuel / security / management fee / forfait
            monies = LASTMONEY.findall(raw)
            if monies:
                ct = "fuel" if "carburant" in cl.group(1).lower() else "surcharge"
                out.append(Line(**meta(order_item_id=None, ship_date=None, charge_type=ct,
                                       charge_label=cl.group(1).strip()[:60], amount=_eur(monies[-1]))))
            continue

    parsed = round(sum(l.amount for l in out), 2)
    if control is not None:
        resid = round(control - parsed, 2)
        if abs(resid) > 0.01:
            out.append(Line(**meta(order_item_id=None, ship_date=None, charge_type="surcharge",
                                   charge_label="residual to control (fuel/adjust/ungrouped)", amount=resid)))
    parse.last_meta = PMETA
    return out

def parse_full(path, carrier="chronopost"):
    lines = parse(path, carrier=carrier)
    return lines, getattr(parse, "last_meta", {})

if __name__ == "__main__":
    for p in sys.argv[1:]:
        ls = parse(p)
        rec = [l for l in ls if l.order_item_id]
        tracks = {l.order_item_id for l in ls if l.order_item_id}
        print(os.path.basename(p), "total=", round(sum(l.amount for l in ls), 2),
              "lines=", len(ls), "parcel_lines=", len(rec), "distinct_tracks=", len(tracks))
