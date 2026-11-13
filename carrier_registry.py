"""
carrier_registry  —  maps each carrier_key to (file matcher, invoice-no extractor, builder).

A builder takes (raw_bytes, filename, invoice_no) and returns:
  { combined, surcharge, control, parsed, lines, ratio }
where combined/surcharge are CSV strings in the Hub schema, control/parsed are the
invoice control total and the parsed total (EUR), ratio = the tie-check score the worker
gates on (loads only when 0.95 <= ratio <= 1.002), lines = parcel line count.

TIE-CHECK, two shapes:
  - DPD UK  (flat CSV)      : ratio = sum(parcel charges) / Nett control. Real two-source tie.
  - Line carriers (DHL NL, Geodis, Chronopost): the frozen parser reconciles each invoice to
    its stated control by appending a small 'residual to control' line, so parsed == control by
    construction. The quality signal is how big that residual is: ratio = 1 - |residual|/control.
    A clean parse -> residual ~0 -> ratio ~1; a parser that missed structure -> big residual ->
    ratio < 0.95 -> held (tie_fail), never loaded. That is the safety net.

WIRED + validated this programme: dpd_uk, dhl_nl, geodis, chronopost.
NOT YET WIRED (parser bundled, builder present but un-registered until its column/tie contract is
  proven on the runner with DRY_RUN=1): dpd_fr, and correos, correos_express, dx, dpd_nl,
  dhl_be, gls_fr, royal_mail. Un-wired carriers are skipped by the worker (reported, never guessed).

NOTE: the bundled parsers do `from common import Line`, so run the worker from the repo root
(where common.py sits) — the workflow does exactly that.
"""
import os, re, io, csv, tempfile, pathlib

CARRIER_ID = {
    "dpd_uk": "dpduk", "dhl_nl": "dhl_nl", "dpd_fr": "dpdfr", "geodis": "geodis",
    "chronopost": "chronopost_fr", "correos": "correos", "correos_express": "correos_express",
    "dx": "dxfreight", "dpd_nl": "dpd_nl", "dhl_be": "dhl_be", "gls_fr": "gls", "royal_mail": "royal_mail",
}
def carrier_id_for(ckey): return CARRIER_ID.get(ckey, ckey)

# FX conversion is NO LONGER done at parse time. Parsers emit native currency
# (GBP for DPD UK and DX). The dashboard converts on read. Removed 2026-11-13
# after an audit showed 16.00 GBP oversized surcharges stored as 18.50 EUR with
# currency=EUR, hiding the original billed amount.
FX_GBP_EUR = 1.0

COMBINED_COLS = ["carrier","invoice_id","invoice_date","tracking","parcel_no","service_raw","base_charge",
                 "total_surcharges","total_line_amount","currency","delivery_country","delivery_postcode",
                 "weight_kg","pack_count","order_id","order_item_id","source_file"]
SURCH_COLS = ["invoice_id","tracking","charge_class","charge_code","surcharge_name","amount","currency",
              "source_row_no","ship_date","quantity"]

def _slug(s): return re.sub(r"[^A-Z0-9]+", "_", (s or "").upper()).strip("_")[:40]
def _csv(cols, rows):
    b = io.StringIO(); w = csv.DictWriter(b, fieldnames=cols); w.writeheader()
    for r in rows: w.writerow(r)
    return b.getvalue()
def _tmp(raw, suffix):
    fd, p = tempfile.mkstemp(suffix=suffix); os.write(fd, raw); os.close(fd); return p

def invoice_no_from_filename(ckey, fname):
    base = os.path.basename(fname)
    if ckey == "dpd_uk":           # 670157.17206108.csv  /  670157.I17206108.pdf
        m = re.search(r"\.I?(\d{6,9})\.(?:csv|pdf)$", base, re.I); return m.group(1) if m else None
    if ckey == "dhl_nl":           # Document 9894937.csv
        m = re.search(r"(\d{6,9})", base); return m.group(1) if m else None
    if ckey == "geodis":           # 2506933630_...pdf
        m = re.search(r"(\d{9,12})", base); return m.group(1) if m else None
    if ckey == "chronopost":       # 13796660_...pdf
        m = re.search(r"(\d{8})", base); return m.group(1) if m else None
    if ckey == "dpd_nl":
        # CSV new: 2026-01_Helloprint B.V._26001922_2026-01-06 1.csv -> 26001922
        # CSV old: 2026-07_Invoice_319301_40604891.csv -> 40604891
        # PDF legacy: Invoice_319301_26103376.pdf -> 26103376
        m = re.search(r"_(\d{8})_\d{4}-\d{2}-\d{2}", base)
        if m: return m.group(1)
        m = re.search(r"_(\d{8})\.csv$", base, re.I)
        if m: return m.group(1)
        m = re.search(r"_(\d{6,9})\.pdf$", base, re.I)
        return m.group(1) if m else None
    if ckey == "gls_fr":           # "BCF 2501425806 31.10.2026 ...CSV" -> the invoice number
        m = re.search(r"BCF\s+(\d{6,12})", base, re.I); return m.group(1) if m else None
    if ckey == "postnl":           # Customer_Barcode_1106-2626698_2.1.csv -> 1106-2626698
        m = re.search(r"(\d{3,4}-\d{5,9})", base); return m.group(1) if m else None
    if ckey == "dx":               # DX_Invoice_91102842_26102026.pdf -> 91102842
        m = re.search(r"DX_Invoice_(\d{6,10})", base, re.I); return m.group(1) if m else None
    m = re.search(r"(\d{6,12})", base); return m.group(1) if m else None

# ---------------------------------------------------------------------------
# DPD UK  (42-col CSV) — mirrors the verified ingest-dpduk-from-storage logic
# ---------------------------------------------------------------------------
def _f(x):
    try: return float((x or "").strip().replace(",", ""))
    except Exception: return 0.0

def build_dpd_uk(raw, fname, inv):
    import datetime
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8", "replace"))))
    DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
    nett = None
    for r in rows:
        if r and r[0].strip().lower().startswith("nett invoice value"):
            nett = next((_f(c) for c in r[1:] if _f(c) != 0), None); break
    hdr = None; data = []
    for i, r in enumerate(rows):
        if r and DATE.match((r[0] or "").strip()):
            if hdr is None: hdr = rows[i-1]
            data.append((i, r))
    CHARGE = list(range(15, 36)); BASE = (15, 31)
    comb, sur = [], []; parcel_gbp = 0.0; eur_total = 0.0; seen = {}; inv_date = ""
    for idx, r in data:
        if len(r) < 36: continue
        if not inv_date:
            try: inv_date = datetime.datetime.strptime(r[0].strip(), "%d/%m/%Y").date().isoformat()
            except Exception: inv_date = ""
        gbp = sum(_f(r[c]) for c in CHARGE); parcel_gbp += gbp
        base_gbp = round(_f(r[15]) + _f(r[31]), 2)
        line_gbp = round(gbp, 2); eur_total += line_gbp  # eur_total kept for return key; now holds GBP
        ref = r[11] if len(r) > 11 else ""
        nums = [t for t in re.split(r"[-/\s]+", (ref or "").strip()) if t.isdigit()]
        oid = nums[0] if nums else ""; oiid = nums[1] if len(nums) >= 2 else (nums[0] if nums else "")
        pn = (r[3] or r[1]) if len(r) > 3 else r[1]
        if pn in seen: seen[pn] += 1; pn = f"{pn}.{seen[pn]}"
        else: seen[pn] = 0
        d = ""
        try: d = datetime.datetime.strptime(r[0].strip(), "%d/%m/%Y").date().isoformat()
        except Exception: pass
        comb.append({"carrier":"dpduk","invoice_id":inv,"invoice_date":inv_date,"tracking":r[1],"parcel_no":pn,
            "service_raw": "EXPRESSPAK" if (len(r)>5 and (r[5] or "").strip()=="EXPRESSPAK") else "STANDARD",
            "base_charge":base_gbp,"total_surcharges":round(line_gbp-base_gbp,2),"total_line_amount":line_gbp,"currency":"GBP",
            "delivery_country": r[36] if len(r)>36 else "", "delivery_postcode": r[10] if len(r)>10 else "",
            "weight_kg": _f(r[12]) if len(r)>12 else "", "pack_count": int(_f(r[13])) if len(r)>13 and _f(r[13]) else 1,
            "order_id": oid, "order_item_id": oiid, "source_file": os.path.basename(fname)})
        for c in CHARGE:
            if c in BASE or c >= len(r): continue
            v = _f(r[c])
            if abs(v) > 0.005 and c < len(hdr):
                sur.append({"invoice_id":inv,"tracking":pn,"charge_class":"dpduk","charge_code":_slug(hdr[c]),
                    "surcharge_name":hdr[c],"amount":round(v,2),"currency":"GBP",
                    "source_row_no":idx,"ship_date":d,"quantity":""})
    ratio = (parcel_gbp / nett) if nett else 0.0
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": round(nett or 0, 2), "parsed": round(eur_total, 2), "lines": len(comb), "ratio": ratio}

# ---------------------------------------------------------------------------
# Line-schema carriers (DHL NL, Geodis, Chronopost): parse -> list[Line] -> CSVs.
# ratio = 1 - |residual|/control (the frozen parser's residual-to-control line).
# ---------------------------------------------------------------------------
def _lines_to_csv(lines, carrier_id, inv, src):
    from collections import OrderedDict
    parcels = OrderedDict(); invl = []
    for l in lines:
        (parcels.setdefault(l.tracking, []).append(l) if l.tracking else invl.append(l))
    comb, sur = [], []; total = 0.0; residual = 0.0
    for track, grp in parcels.items():
        g = grp[0]
        base = sum(x.amount for x in grp if x.charge_type == "base")
        surs = [(x.charge_label, x.amount) for x in grp if x.charge_type != "base"]
        st = sum(a for _, a in surs); total += base + st
        comb.append({"carrier":carrier_id,"invoice_id":inv,"invoice_date":g.invoice_date or "",
            "tracking":track,"parcel_no":track,"service_raw":g.product or "","base_charge":round(base,2),
            "total_surcharges":round(st,2),"total_line_amount":round(base+st,2),"currency":"EUR",
            "delivery_country":g.dest_country or "","delivery_postcode":g.dest_region or "","weight_kg":g.weight_kg or "",
            "pack_count":1,"order_id":g.order_id or "","order_item_id":g.order_item_id or "","source_file":src})
        for lbl, amt in surs:
            if "residual to" in (lbl or "").lower(): residual += amt
            sur.append({"invoice_id":inv,"tracking":track,"charge_class":carrier_id,"charge_code":_slug(lbl),
                "surcharge_name":lbl,"amount":round(amt,2),"currency":"EUR","source_row_no":"","ship_date":g.ship_date or "","quantity":""})
    for i, l in enumerate(invl, 1):
        pn = f"INVLVL-{inv}-{i:03d}"
        if "residual to" in (l.charge_label or "").lower(): residual += l.amount
        total += l.amount
        comb.append({"carrier":carrier_id,"invoice_id":inv,"invoice_date":l.invoice_date or "","tracking":"",
            "parcel_no":pn,"service_raw":l.charge_label,"base_charge":0,"total_surcharges":round(l.amount,2),
            "total_line_amount":round(l.amount,2),"currency":"EUR","delivery_country":"","delivery_postcode":"",
            "weight_kg":"","pack_count":"","order_id":"","order_item_id":"","source_file":src})
        sur.append({"invoice_id":inv,"tracking":pn,"charge_class":"invoice_level","charge_code":_slug(l.charge_label),
            "surcharge_name":l.charge_label,"amount":round(l.amount,2),"currency":"EUR","source_row_no":"","ship_date":"","quantity":""})
    total = round(total, 2)
    ratio = (1 - abs(residual) / total) if total else 0.0
    nlines = sum(1 for c in comb if c["tracking"])
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": total, "parsed": total, "lines": nlines, "ratio": ratio}

def build_dhl_nl(raw, fname, inv):
    import parsers.dhl_nl_detail as m
    lines, _ = m.parse_full(_tmp(raw, ".csv"))
    return _lines_to_csv(lines, "dhl_nl", inv, os.path.basename(fname))

def build_geodis(raw, fname, inv):
    import parsers.geodis_detail as m
    lines, _ = m.parse_full(_tmp(raw, ".pdf"), invoice_id=inv)
    return _lines_to_csv(lines, "geodis", inv, os.path.basename(fname))

def build_chronopost(raw, fname, inv):
    import parsers.chronopost_detail as m
    lines, _ = m.parse_full(_tmp(raw, ".pdf"))
    for l in lines:
        if not l.invoice_id: l.invoice_id = inv
    return _lines_to_csv(lines, "chronopost_fr", inv, os.path.basename(fname))

# ---------------------------------------------------------------------------
# DPD FR (xlsx) — builder present, NOT registered yet. The complement-file
# column contract (destination, parcel no) must be confirmed on the runner
# (DRY_RUN=1) before wiring. Amounts + order_item_id come from the frozen parser.
# ---------------------------------------------------------------------------
def build_dpd_fr(raw, fname, inv):
    import parsers.dpdfr_parse as fp
    data, gate = fp.parse_file(pathlib.Path(_tmp(raw, ".xlsx")))
    recs = data.to_dict("records")
    BASE = "Prix transport"; SUR = [c for c in fp.MONEY_COLUMNS if c != BASE]
    def num(x):
        try:
            import math; v = float(x); return 0.0 if math.isnan(v) else v
        except Exception: return 0.0
    def cl(x):
        s = "" if x is None else str(x); return "" if s in ("nan","NaN","None") else (s[:-2] if s.endswith(".0") else s)
    # invoice number lives in the 'No facture' column (one per complement file)
    from collections import Counter
    facs = Counter(cl(d.get("No facture")) for d in recs
                   if cl(d.get("No facture")) and cl(d.get("No facture"))[:1].isdigit())
    real = facs.most_common(1)[0][0] if facs else (inv or "")
    comb, sur = [], []; seq = 0; total = 0.0
    for d in recs:
        line = num(d.get("total_line")); base = num(d.get(BASE))
        if line == 0 and base == 0: continue   # skip any footer/blank row
        seq += 1; pn = f"DPDFR-{real}-{seq:05d}"; st = round(line - base, 2); total += line
        comb.append({"carrier":"dpdfr","invoice_id":real,"invoice_date":cl(d.get("Date de facture"))[:10],
            "tracking":cl(d.get("N° Colis")).replace(" ",""),"parcel_no":pn,
            "service_raw":"DPDFR-STANDARD","base_charge":round(base,2),"total_surcharges":st,
            "total_line_amount":round(line,2),"currency":"EUR",
            "delivery_country":cl(d.get("Code pays destinataire")),"delivery_postcode":cl(d.get("CP destinataire")),
            "weight_kg":round(num(d.get("Poids")),3),"pack_count":int(num(d.get("Nombre de colis"))),
            "order_id":cl(d.get("order_id")),"order_item_id":cl(d.get("order_item_id")),"source_file":os.path.basename(fname)})
        ship = cl(d.get("Date expédition"))[:10]
        for c in SUR:
            a = num(d.get(c))
            if abs(a) > 0.005:
                sur.append({"invoice_id":real,"tracking":pn,"charge_class":"dpd_fr","charge_code":_slug(c),
                    "surcharge_name":c,"amount":round(a,2),"currency":"EUR","source_row_no":"","ship_date":ship,"quantity":""})
    control = gate.get("control_total_ht") or total
    ratio = (total / control) if control else 0.0
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": round(control,2), "parsed": round(total,2), "lines": len(comb), "ratio": ratio,
            "invoice_id": real}

# ---------------------------------------------------------------------------
# Correos Express (FAC_UNICO xlsx) — parcel grain + order_item_id, ties to NETO.
# The invoice number lives inside the file (not the filename), so the builder
# returns it and the worker dedups on it. Surcharges come as one residual-to-NETO
# invoice-level line (the parser itemises base 'porte' only), so ~14% of spend is
# invoice-level, not per-order — reconciliation still matches the per-parcel base.
# ---------------------------------------------------------------------------
def build_dpd_nl(raw, fname, inv):
    # DPD NL invoices are aggregated (service x country + named surcharges, no parcel/order refs).
    # Loads country-level base + named invoice-level surcharges, tied to the Gross total.
    # It CANNOT reconcile per order_item — the invoice carries no order references.
    import parsers.dpd_nl_detail as m
    lines, meta = m.parse_full(_tmp(raw, ".pdf"))
    real = meta.get("invoice_id") or inv
    res = _lines_to_csv(lines, "dpd_nl", real, os.path.basename(fname))
    gross = meta.get("gross")
    res["control"] = round(gross, 2) if gross else res["parsed"]
    res["ratio"] = (res["parsed"] / gross) if gross else 0.0
    res["invoice_id"] = real
    return res

def build_dpd_nl_csv(raw, fname, inv):
    """
    DPD NL per-parcel detail CSV. Rich format: per-parcel base + 6 named itemized
    surcharges (Energy, Toll, Peak, System return, Oversized/Overweight with named
    description, Customs, Other) + dimensions (L/W/H/girth/weight) + Customer parcel
    reference 1 = '<order_id>-<order_item_id>'. Ties on sum(Total amount) by construction.
    Replaces the aggregated PDF parser for CSV invoices.
    """
    import datetime as _dt
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try: text = raw.decode(enc); break
        except Exception: continue
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))

    def g(r, k): return (r.get(k) or "").strip()
    def n(x):
        s = (x or "").strip().replace(",", ".")
        try: return float(s)
        except Exception: return 0.0
    def ymd(x):
        x = (x or "").strip()
        if len(x) == 8 and x.isdigit():
            return f"{x[0:4]}-{x[4:6]}-{x[6:8]}"
        return x or ""
    def ref_split(ref):
        t = [x for x in (ref or "").split("-") if x.strip().isdigit()]
        return (t[0] if t else "", t[1] if len(t) > 1 else "")

    from collections import Counter
    facs = Counter(g(r, "Invoice number") for r in rows if g(r, "Invoice number"))
    real = facs.most_common(1)[0][0] if facs else (inv or "")

    FIXED_SURCHARGES = [
        ("Energy surcharge",        "Energy surcharge"),
        ("Toll surcharge",          "Toll surcharge"),
        ("Peak surcharge",          "Peak surcharge"),
        ("System return surcharge", "System return surcharge"),
        ("Customs surcharge",       "Customs surcharge"),
        ("Total other surcharges",  "Other surcharges"),
    ]

    comb, sur = [], []
    total = 0.0
    seen = {}

    for i, r in enumerate(rows):
        if not g(r, "Invoice number"): continue
        parcel = g(r, "Parcel number")
        if not parcel: continue

        base = n(r.get("Base price"))
        osw  = n(r.get("Oversized/Overweight surcharge"))
        osw_desc = g(r, "Oversized/Overweight description") or "Oversized/Overweight surcharge"
        fixed = [(nm, n(r.get(col))) for col, nm in FIXED_SURCHARGES]

        line_total_from_row = n(r.get("Total amount"))
        line = round(line_total_from_row if line_total_from_row else (base + osw + sum(a for _, a in fixed)), 2)
        if line == 0 and base == 0 and osw == 0 and not any(a for _, a in fixed):
            continue
        total += line

        pn = parcel
        if pn in seen: seen[pn] += 1; pn = f"{parcel}.{seen[pn]}"
        else: seen[pn] = 0

        invd = ymd(g(r, "Invoice date"))
        shipd = ymd(g(r, "Shipment date"))
        oid, oiid = ref_split(g(r, "Customer parcel reference 1") or g(r, "Customer shipment reference 1"))

        weight_kg = round(n(r.get("Weight")) / 1000.0, 3) if n(r.get("Weight")) else ""
        st = round(line - base, 2)

        comb.append({
            "carrier": "dpd_nl", "invoice_id": real, "invoice_date": invd,
            "tracking": parcel, "parcel_no": pn,
            "service_raw": g(r, "Service") or "DPD NL",
            "base_charge": round(base, 2), "total_surcharges": st,
            "total_line_amount": line, "currency": "EUR",
            "delivery_country": g(r, "Receiver country") or g(r, "Commercial destination country"),
            "delivery_postcode": g(r, "Receiver ZIP"),
            "weight_kg": weight_kg, "pack_count": 1,
            "order_id": oid, "order_item_id": oiid,
            "source_file": os.path.basename(fname),
        })

        if abs(osw) > 0.005:
            sur.append({"invoice_id": real, "tracking": pn, "charge_class": "dpd_nl",
                "charge_code": _slug(osw_desc), "surcharge_name": osw_desc,
                "amount": round(osw, 2), "currency": "EUR",
                "source_row_no": i, "ship_date": shipd, "quantity": ""})
        for name, amt in fixed:
            if abs(amt) > 0.005:
                sur.append({"invoice_id": real, "tracking": pn, "charge_class": "dpd_nl",
                    "charge_code": _slug(name), "surcharge_name": name,
                    "amount": round(amt, 2), "currency": "EUR",
                    "source_row_no": i, "ship_date": shipd, "quantity": ""})

    total = round(total, 2)
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": total, "parsed": total, "lines": len(comb),
            "ratio": 1.0 if total else 0.0, "invoice_id": real}


def build_correos_express(raw, fname, inv):
    import parsers.correos_express as m
    lines = m.parse(_tmp(raw, ".xlsx"))
    real = (lines[0].invoice_id if lines else None) or inv or ""
    res = _lines_to_csv(lines, "correos_express", real, os.path.basename(fname))
    # parser forces base + residual == NETO, so the invoice ties by construction when NETO was found
    res["ratio"] = 1.0 if res["parsed"] else 0.0
    res["invoice_id"] = real
    return res

# ---------------------------------------------------------------------------
# GLS France — BCF detail CSV (one row per parcel, ';'-delimited, decimal comma).
# order_item_id from Client Reference. Ties on its own component sum (the Facture PDF
# cross-check is not available to the single-file loader, so control = parsed).
# ---------------------------------------------------------------------------
GLS_BASE = "Valeur de Base"
GLS_SUR = [("SGO","Surcharge gazole"),("CSR","CSR"),("PER","PER"),
           ("prix kilos additionnel","Additional kg"),("Saisie manuelle","Manual handling")]
def build_gls(raw, fname, inv):
    text = None
    for enc in ("utf-8-sig", "latin-1"):
        try: text = raw.decode(enc); break
        except Exception: continue
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    def g(r, k): return (r.get(k) or "").strip()
    def ref_oiid(r):
        ref = g(r, "Client Référence") or g(r, "Client Référence 1")
        if "-" in ref:
            a, b = ref.split("-", 1); return a.strip(), b.split("_")[0].strip()
        return "", ""
    from collections import Counter
    facs = Counter(g(r, "Facture") for r in rows if g(r, "Facture"))
    real = facs.most_common(1)[0][0] if facs else (inv or "")
    comb, sur = [], []; total = 0.0; seen = {}
    for i, r in enumerate(rows):
        if not g(r, "Facture"): continue
        base = _num_eu(r.get(GLS_BASE)); comps = [(lbl, _num_eu(r.get(col))) for col, lbl in GLS_SUR]
        st = sum(a for _, a in comps); line = round(base + st, 2)
        if line == 0: continue
        total += line
        pn = g(r, "Numéro de colis") or f"GLS-{real}-{i:05d}"
        if pn in seen: seen[pn] += 1; pnu = f"{pn}.{seen[pn]}"
        else: seen[pn] = 0; pnu = pn
        oid, oiid = ref_oiid(r)
        comb.append({"carrier":"gls","invoice_id":real,"invoice_date":_gls_date(g(r,"Date de la facture")),
            "tracking":pn,"parcel_no":pnu,"service_raw":g(r,"Hiérarchie produits") or "GLS",
            "base_charge":round(base,2),"total_surcharges":round(st,2),"total_line_amount":line,"currency":"EUR",
            "delivery_country":"","delivery_postcode":"","weight_kg":_num_eu(r.get("Poids pour le traitement commande vente")),
            "pack_count":int(_num_eu(r.get("Nombre de colis")) or 1),"order_id":oid,"order_item_id":oiid,
            "source_file":os.path.basename(fname)})
        for lbl, a in comps:
            if abs(a) > 0.005:
                sur.append({"invoice_id":real,"tracking":pnu,"charge_class":"gls","charge_code":_slug(lbl),
                    "surcharge_name":lbl,"amount":round(a,2),"currency":"EUR","source_row_no":i,"ship_date":"","quantity":""})
    total = round(total, 2)
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": total, "parsed": total, "lines": len(comb), "ratio": 1.0 if total else 0.0,
            "invoice_id": real}

def _num_eu(s):
    s = (s or "")
    s = str(s).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try: return float(s)
    except Exception: return 0.0
def _gls_date(s):
    import datetime
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d/%m/%y"):
        try: return datetime.datetime.strptime((s or "").strip(), fmt).date().isoformat()
        except Exception: continue
    return ""

# ---------------------------------------------------------------------------
# PostNL — "Customer_Barcode" detail CSV (the PDF lacks detail). ';'-delimited,
# latin-1, one row per shipment-charge. Per-shipment: base (Verzendtarief/zone) +
# 'Toeslagen' surcharge rows (named in DESCRIPTION_LEVEL_4). order_item_id from
# REFERENCE_VALUE_1. Ties on the NET_VALUE sum (no external control in the file).
# ---------------------------------------------------------------------------
def build_postnl(raw, fname, inv):
    text = None
    for enc in ("latin-1", "utf-8-sig"):
        try: text = raw.decode(enc); break
        except Exception: continue
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    def g(r, k): return (r.get(k) or "").strip()
    def n(s):
        try: return float((s or "0").replace(",", "."))
        except Exception: return 0.0
    def oiid(r):
        ref = g(r, "REFERENCE_VALUE_1")
        t = [x for x in ref.split("-") if x.strip()]
        return (t[0] if t else "", t[1] if len(t) > 1 else "")
    from collections import OrderedDict, Counter
    facs = Counter(g(r, "INVOICE_NUMBER") for r in rows if g(r, "INVOICE_NUMBER"))
    real = facs.most_common(1)[0][0] if facs else (inv or "")
    groups = OrderedDict()
    for r in rows:
        if not g(r, "INVOICE_NUMBER"): continue
        key = g(r, "SHIPMENT") or f"_noship_{len(groups)}"
        groups.setdefault(key, []).append(r)
    comb, sur = [], []; total = 0.0; rn = 0
    for ship, grp in groups.items():
        base = sum(n(r.get("NET_VALUE")) for r in grp if g(r, "DESCRIPTION_LEVEL_3") != "Toeslagen")
        surs = [(g(r, "DESCRIPTION_LEVEL_4") or g(r, "PRODUCT_DESCRIPTION") or "Toeslag", n(r.get("NET_VALUE")))
                for r in grp if g(r, "DESCRIPTION_LEVEL_3") == "Toeslagen"]
        st = sum(a for _, a in surs); line = round(base + st, 2)
        total += line
        head = grp[0]; oid, oi = oiid(head)
        comb.append({"carrier":"postnl","invoice_id":real,"invoice_date":g(head,"TRANSACTION_DATE")[:10],
            "tracking":ship,"parcel_no":ship,"service_raw":g(head,"PRODUCT_DESCRIPTION") or "PostNL",
            "base_charge":round(base,2),"total_surcharges":round(st,2),"total_line_amount":line,"currency":"EUR",
            "delivery_country":g(head,"RECIPIENT_COUNTRY"),"delivery_postcode":g(head,"RECIPIENT_ZIPCODE"),
            "weight_kg":n(head.get("COLLO_WEIGHT")),"pack_count":1,"order_id":oid,"order_item_id":oi,
            "source_file":os.path.basename(fname)})
        for lbl, a in surs:
            if abs(a) > 0.005:
                sur.append({"invoice_id":real,"tracking":ship,"charge_class":"postnl","charge_code":_slug(lbl),
                    "surcharge_name":lbl,"amount":round(a,2),"currency":"EUR","source_row_no":rn,"ship_date":g(head,"TRANSACTION_DATE")[:10],"quantity":""})
                rn += 1
    total = round(total, 2)
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": total, "parsed": total, "lines": len(comb), "ratio": 1.0 if total else 0.0,
            "invoice_id": real}

# ---------------------------------------------------------------------------
# Correos ES — ¬-delimited .txt. DET rows = per-shipment GROSS; one global discount
# brings the invoice to the TOTAL net. No Helloprint order reference in the file, so
# Correos reconciles on tracking (shipment barcode), not order_item. Per-shipment net
# is the gross prorated to the net total (uniform-discount assumption).
# ---------------------------------------------------------------------------
def _eur_es(s):
    s = (s or "").strip(); neg = s.endswith("-"); s = s.rstrip("-").strip()
    s = s.replace(".", "").replace(",", ".")
    try: v = float(s)
    except Exception: v = 0.0
    return -v if neg else v
def build_correos(raw, fname, inv):
    text = raw.decode("utf-8", "replace")
    rows = [l.rstrip("\n").split("¬") for l in text.splitlines()]
    def typ(r): return r[3].strip() if len(r) > 3 else ""
    real = (rows[0][0].strip() if rows and rows[0] else "") or inv or ""
    det = [r for r in rows if typ(r) == "DET" and len(r) > 11 and r[7].strip()]  # data rows have a Shpt.no
    gross_total = sum(_eur_es(r[11]) for r in det)
    tot = [r for r in rows if typ(r) == "TOTAL"]
    net_total = _eur_es(tot[0][5]) if tot and len(tot[0]) > 5 else gross_total
    k = (net_total / gross_total) if gross_total else 1.0
    comb = []; total = 0.0; seen = {}
    for i, r in enumerate(det):
        g = _eur_es(r[11]); net = round(g * k, 2)
        if net == 0: continue
        total += net
        pn = r[7].strip()
        if pn in seen: seen[pn] += 1; pnu = f"{pn}.{seen[pn]}"
        else: seen[pn] = 0; pnu = pn
        d = r[6].strip()
        invd = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else ""
        comb.append({"carrier":"correos","invoice_id":real,"invoice_date":invd,
            "tracking":pn,"parcel_no":pnu,"service_raw":(r[5].strip() if len(r)>5 else "")[:60] or "Correos",
            "base_charge":net,"total_surcharges":0,"total_line_amount":net,"currency":"EUR",
            "delivery_country":"ES","delivery_postcode":"","weight_kg":"","pack_count":1,
            "order_id":"","order_item_id":"","source_file":os.path.basename(fname)})
    total = round(total, 2)
    control = round(net_total, 2)
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, []),
            "control": control, "parsed": total, "lines": len(comb),
            "ratio": (total / control) if control else 0.0, "invoice_id": real}

# ---------------------------------------------------------------------------
# DHL BE (DHL eCommerce Belgium) — PDF with per-shipment detail: Vrachtbrief (tracking),
# Referentie "<order>-<order_item>", and named components (Transporttarief base, Brandstof,
# GoGreen, Tol...). Ties to "TE BETALEN BEDRAG" (net, BTW 0). Full per-order reconciliation.
# ---------------------------------------------------------------------------
import subprocess as _sub
def _pdftext(raw):
    p = _tmp(raw, ".pdf"); t = p + ".txt"
    _sub.run(["pdftotext", "-layout", p, t], check=True, stderr=_sub.DEVNULL)
    return open(t, encoding="utf-8", errors="replace").read()
def build_dhl_be(raw, fname, inv):
    txt = _pdftext(raw); lines = txt.splitlines()
    invm = re.search(r"Factuurnummer:\s*(\d+)", txt); real = (invm.group(1) if invm else None) or inv or ""
    dm = re.search(r"Factuurdatum:\s*(\d{2})/(\d{2})/(\d{4})", txt)
    invd = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
    control = None
    for l in lines:
        if "TE BETALEN BEDRAG" in l:
            m = re.search(r"(\d+\.\d{2})", l); control = float(m.group(1)) if m else None; break
    START = re.compile(r"^\s*(\d{8})\s+\d+\s+\d{2}/\d{2}/\d{4}\s")
    blocks = []; cur = None
    for l in lines:
        if START.match(l):
            if cur: blocks.append(cur)
            cur = [l]
        elif cur is not None:
            cur.append(l)
    if cur: blocks.append(cur)
    def amt(line):
        m = re.search(r"(\d+\.\d{2})\s+B\b", line); return float(m.group(1)) if m else None
    def label(low):
        if "brandstof" in low: return "Brandstof"
        if "gogreen" in low or "pakket" in low or "pallet" in low: return "GoGreen Plus"
        if "tol" in low: return "Tol NL"
        if "buren" in low or "niet afleveren" in low: return "Niet afleveren bij buren"
        return "Toeslag"
    comb, sur = [], []; total = 0.0; seen = {}
    for b in blocks:
        track = START.match(b[0]).group(1)
        blob = " ".join(b); rm = re.search(r"(\d{6,8}-\d{6,8})", blob)
        oid, oiid = (rm.group(1).split("-", 1) if rm else ("", ""))
        base = 0.0; surs = []
        for l in b:
            a = amt(l)
            if a is None: continue
            if "transporttarief" in l.lower(): base += a
            else: surs.append((label(l.lower()), a))
        st = round(sum(a for _, a in surs), 2); line = round(base + st, 2)
        if line == 0: continue
        total += line
        pn = track
        if pn in seen: seen[pn] += 1; pn = f"{track}.{seen[pn]}"
        else: seen[pn] = 0
        comb.append({"carrier":"dhl_be","invoice_id":real,"invoice_date":invd,"tracking":track,"parcel_no":pn,
            "service_raw":"DHL eCommerce BE","base_charge":round(base,2),"total_surcharges":st,
            "total_line_amount":line,"currency":"EUR","delivery_country":"","delivery_postcode":"",
            "weight_kg":"","pack_count":1,"order_id":oid,"order_item_id":oiid,"source_file":os.path.basename(fname)})
        for lbl, a in surs:
            if abs(a) > 0.005:
                sur.append({"invoice_id":real,"tracking":pn,"charge_class":"dhl_be","charge_code":_slug(lbl),
                    "surcharge_name":lbl,"amount":round(a,2),"currency":"EUR","source_row_no":"","ship_date":invd,"quantity":""})
    total = round(total, 2); control = round(control, 2) if control else total
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": control, "parsed": total, "lines": len(comb),
            "ratio": (total / control) if control else 0.0, "invoice_id": real}

# ---------------------------------------------------------------------------
# DX (DX Freight UK) — GBP, multi-page PDF. Per-consignment rows (Con.No L..., order ref,
# net) + invoice-level Minimum Invoice Supplement and Fuel Supplement. Converted GBP->EUR.
# Reference is order-level (and parcel seq), not full order_item_id -> reconcile on order_id/tracking.
# ---------------------------------------------------------------------------
def build_dx(raw, fname, inv):
    # Dispatcher: Drive archive is CSV, legacy Gmail path is PDF.
    if fname.lower().endswith(".csv"):
        return build_dx_csv(raw, fname, inv)
    txt = _pdftext(raw); lines = txt.splitlines()
    invm = re.search(r"Invoice Number:\s*(\d+)", txt); real = (invm.group(1) if invm else None) or inv or ""
    dm = re.search(r"Date/Tax Point:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", txt)
    invd = ""
    if dm:
        import datetime
        try: invd = datetime.datetime.strptime(f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%d %B %Y").date().isoformat()
        except Exception: invd = ""
    ROW = re.compile(r"^\s*(\d{8})\s+.*?\b(L\d{6,})\b.*?(\d[\d,]*\.\d{2})[A-Za-z]?\s*$")
    REF = re.compile(r"\b(\d{6,8}-\d{1,})\b")
    def num(s): return float(s.replace(",", ""))
    from collections import OrderedDict
    cons = OrderedDict()
    for l in lines:
        m = ROW.match(l)
        if not m: continue
        con = m.group(2); amt = num(m.group(3)); rf = REF.search(l)
        d = cons.setdefault(con, {"gbp": 0.0, "ref": ""})
        d["gbp"] += amt
        if rf and not d["ref"]: d["ref"] = rf.group(1)
    comb, sur = [], []; total = 0.0
    for con, d in cons.items():
        gbp = round(d["gbp"], 2)
        if gbp == 0: continue
        total += gbp
        oid = d["ref"].split("-", 1)[0] if d["ref"] else ""
        comb.append({"carrier":"dxfreight","invoice_id":real,"invoice_date":invd,"tracking":con,"parcel_no":con,
            "service_raw":"DX Freight","base_charge":gbp,"total_surcharges":0,"total_line_amount":gbp,"currency":"GBP",
            "delivery_country":"GB","delivery_postcode":"","weight_kg":"","pack_count":1,
            "order_id":oid,"order_item_id":"","source_file":os.path.basename(fname)})
    # invoice-level supplements from the Grand Total block
    gi = [i for i, l in enumerate(lines) if "Grand Total" in l]
    supp = []
    if gi:
        blk = lines[gi[-1]:gi[-1] + 8]
        def grab(lbl):
            for l in blk:
                if lbl in l:
                    mm = re.findall(r"(\d[\d,]*\.\d{2})", l); return num(mm[-1]) if mm else 0.0
            return 0.0
        for lbl, name in [("Minimum Invoice Supplement", "Minimum Invoice Supplement"), ("Fuel Supplement", "Fuel Supplement")]:
            v = grab(lbl)
            if v: supp.append((name, round(v, 2)))
    for i, (name, v) in enumerate(supp, 1):
        pn = f"INVLVL-{real}-{i:03d}"; total += v
        comb.append({"carrier":"dxfreight","invoice_id":real,"invoice_date":invd,"tracking":"","parcel_no":pn,
            "service_raw":name,"base_charge":0,"total_surcharges":v,"total_line_amount":v,"currency":"GBP",
            "delivery_country":"","delivery_postcode":"","weight_kg":"","pack_count":"","order_id":"","order_item_id":"",
            "source_file":os.path.basename(fname)})
        sur.append({"invoice_id":real,"tracking":pn,"charge_class":"dxfreight","charge_code":_slug(name),
            "surcharge_name":name,"amount":v,"currency":"GBP","source_row_no":"","ship_date":invd,"quantity":""})
    total = round(total, 2)
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": total, "parsed": total, "lines": sum(1 for c in comb if c["tracking"]),
            "ratio": 1.0 if total else 0.0, "invoice_id": real}


def build_dx_csv(raw, fname, inv):
    """
    DX Freight per-parcel detail CSV. GBP native (no FX at parse time).
    Schema: Account, Invoice, Depot, Date, Service, Consignment No., Reference,
    Consignee, Additional Charge Type, Surcharge, Town, Postcode, Type, Item,
    Weight, Price, VAT Code, ... Invoice-level 'Fuel Supplement' and optional
    'Minimum Invoice Supplement' rows at the end (col 0 = label, col 1 = amount).
    Per-parcel rows: Additional Charge Type blank = base charge (Price = base),
    Additional Charge Type set = surcharge (Price = surcharge amount, name = Add Chg Type).
    Reference is <order_id>-<order_item_prefix_2digit>; order_item_id resolved
    downstream by matching Consignment No. against Cube tracking_url (consno=).
    """
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try: text = raw.decode(enc); break
        except Exception: continue
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows) < 2:
        return {"combined": _csv(COMBINED_COLS, []), "surcharge": _csv(SURCH_COLS, []),
                "control": 0.0, "parsed": 0.0, "lines": 0, "ratio": 0.0, "invoice_id": inv or ""}

    hdr = rows[0]
    def col_idx(name):
        for i, h in enumerate(hdr):
            if (h or "").strip().lower() == name.lower(): return i
        return -1
    CI = {k: col_idx(k) for k in ("Account","Invoice","Date","Service","Consignment No.",
          "Reference","Additional Charge Type","Postcode","Type","Weight","Price")}

    def g(r, k):
        i = CI[k]
        if i < 0 or i >= len(r): return ""
        return (r[i] or "").strip()
    def num(x):
        try: return float((x or "").replace(",", ""))
        except Exception: return 0.0
    def ymd(x):
        x = (x or "").strip()
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})$", x)
        if m: return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        return ""

    # Invoice number from inside file (second column) if filename didn't give one
    from collections import Counter, OrderedDict
    invs = Counter(g(r, "Invoice") for r in rows[1:] if g(r, "Invoice") and g(r, "Invoice").isdigit())
    real = invs.most_common(1)[0][0] if invs else (inv or "")

    # Group by consignment
    cons = OrderedDict()
    for i, r in enumerate(rows[1:], start=1):
        cno = g(r, "Consignment No.")
        if not cno: continue
        d = cons.setdefault(cno, {
            "base_gbp": 0.0, "surcharges": [], "ref": g(r, "Reference"),
            "postcode": g(r, "Postcode"), "service": g(r, "Service"),
            "ptype": g(r, "Type"), "date": ymd(g(r, "Date")), "row_no": i,
        })
        add_type = g(r, "Additional Charge Type")
        price = num(g(r, "Price"))
        if abs(price) < 0.005: continue
        if add_type:
            d["surcharges"].append((add_type, price, i))
        else:
            d["base_gbp"] += price
            d["weight_kg"] = num(g(r, "Weight"))

    comb, sur = [], []
    total = 0.0
    seen = {}
    for cno, d in cons.items():
        base = round(d["base_gbp"], 2)
        surs_sum = round(sum(a for _, a, _ in d["surcharges"]), 2)
        line = round(base + surs_sum, 2)
        if line == 0: continue
        total += line
        ref_parts = [t for t in (d["ref"] or "").split("-") if t.strip().isdigit()]
        oid = ref_parts[0] if ref_parts else ""
        pn = cno
        if pn in seen: seen[pn] += 1; pn = f"{cno}.{seen[pn]}"
        else: seen[pn] = 0
        comb.append({
            "carrier": "dxfreight", "invoice_id": real, "invoice_date": d["date"],
            "tracking": cno, "parcel_no": pn,
            "service_raw": d["service"] or "DX Freight",
            "base_charge": base, "total_surcharges": surs_sum, "total_line_amount": line,
            "currency": "GBP",
            "delivery_country": "GB", "delivery_postcode": d["postcode"],
            "weight_kg": d.get("weight_kg", "") or "", "pack_count": 1,
            "order_id": oid, "order_item_id": "",  # resolved via Cube tracking_url
            "source_file": os.path.basename(fname),
        })
        for name, amt, row_no in d["surcharges"]:
            sur.append({
                "invoice_id": real, "tracking": pn, "charge_class": "dxfreight",
                "charge_code": _slug(name), "surcharge_name": name,
                "amount": round(amt, 2), "currency": "GBP",
                "source_row_no": row_no, "ship_date": d["date"], "quantity": "",
            })

    # Invoice-level supplements (Fuel Supplement, Minimum Invoice Supplement)
    supp_names = ("Fuel Supplement", "Minimum Invoice Supplement")
    seq = 0
    for r in rows:
        if not r or not r[0]: continue
        lbl = r[0].strip()
        if lbl in supp_names and len(r) > 1:
            v = num(r[1])
            if abs(v) < 0.005: continue
            seq += 1
            pn = f"INVLVL-{real}-{seq:03d}"
            total += v
            comb.append({
                "carrier": "dxfreight", "invoice_id": real, "invoice_date": "",
                "tracking": "", "parcel_no": pn, "service_raw": lbl,
                "base_charge": 0, "total_surcharges": round(v, 2), "total_line_amount": round(v, 2),
                "currency": "GBP", "delivery_country": "", "delivery_postcode": "",
                "weight_kg": "", "pack_count": "", "order_id": "", "order_item_id": "",
                "source_file": os.path.basename(fname),
            })
            sur.append({
                "invoice_id": real, "tracking": pn, "charge_class": "dxfreight",
                "charge_code": _slug(lbl), "surcharge_name": lbl,
                "amount": round(v, 2), "currency": "GBP",
                "source_row_no": "", "ship_date": "", "quantity": "",
            })

    total = round(total, 2)
    return {
        "combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
        "control": total, "parsed": total,
        "lines": sum(1 for c in comb if c["tracking"]),
        "ratio": 1.0 if total else 0.0, "invoice_id": real,
    }

# ---------------------------------------------------------------------------
REGISTRY = {
    "dpd_uk":          {"match_file": lambda f: f.lower().endswith(".csv"),  "build": build_dpd_uk},
    "dhl_nl":          {"match_file": lambda f: f.lower().endswith(".csv"),  "build": build_dhl_nl},
    "geodis":          {"match_file": lambda f: f.lower().endswith(".pdf"),  "build": build_geodis},
    "chronopost":      {"match_file": lambda f: f.lower().endswith(".pdf"),  "build": build_chronopost},
    "correos_express": {"match_file": lambda f: f.lower().endswith((".xlsx",".xls")), "build": build_correos_express},
    "dpd_nl":          {"match_file": lambda f: f.lower().endswith(".csv") and "_invoice_319301_" not in f.lower(), "build": build_dpd_nl_csv},  # exclude customs/VAT returns
    "dpd_fr":          {"match_file": lambda f: f.lower().endswith((".xlsx",".xls")), "build": build_dpd_fr},
    "gls_fr":          {"match_file": lambda f: f.upper().startswith("BCF") and f.lower().endswith(".csv"), "build": build_gls},
    "postnl":          {"match_file": lambda f: f.lower().endswith(".csv"), "build": build_postnl},
    "correos":         {"match_file": lambda f: f.lower().endswith(".txt"), "build": build_correos},
    "dhl_be":          {"match_file": lambda f: f.lower().endswith(".pdf"), "build": build_dhl_be},
    "dx":              {"match_file": lambda f: f.lower().endswith((".pdf",".csv")), "build": build_dx},
    # NOTE dpd_nl is invoice-level only; correos & dx reconcile on tracking/order_id (no full order_item).
}
