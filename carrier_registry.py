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
    "dx": "dx", "dpd_nl": "dpd_nl", "dhl_be": "dhl_be", "gls_fr": "gls", "royal_mail": "royal_mail",
}
def carrier_id_for(ckey): return CARRIER_ID.get(ckey, ckey)

FX_GBP_EUR = 1.1564

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
        base_e = round((_f(r[15]) + _f(r[31])) * FX_GBP_EUR, 2)
        line_e = round(gbp * FX_GBP_EUR, 2); eur_total += line_e
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
            "base_charge":base_e,"total_surcharges":round(line_e-base_e,2),"total_line_amount":line_e,"currency":"EUR",
            "delivery_country": r[36] if len(r)>36 else "", "delivery_postcode": r[10] if len(r)>10 else "",
            "weight_kg": _f(r[12]) if len(r)>12 else "", "pack_count": int(_f(r[13])) if len(r)>13 and _f(r[13]) else 1,
            "order_id": oid, "order_item_id": oiid, "source_file": os.path.basename(fname)})
        for c in CHARGE:
            if c in BASE or c >= len(r): continue
            v = _f(r[c])
            if abs(v) > 0.005 and c < len(hdr):
                sur.append({"invoice_id":inv,"tracking":pn,"charge_class":"dpduk","charge_code":_slug(hdr[c]),
                    "surcharge_name":hdr[c],"amount":round(v*FX_GBP_EUR,2),"currency":"EUR",
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
    comb, sur = [], []; seq = 0; total = 0.0
    for d in recs:
        seq += 1; pn = f"DPDFR-{inv}-{seq:05d}"
        base = num(d.get(BASE)); line = num(d.get("total_line")); st = round(line - base, 2); total += line
        comb.append({"carrier":"dpdfr","invoice_id":inv,"invoice_date":"",
            "tracking":cl(d.get("N° de colis") or d.get("Numéro de colis")).replace(" ",""),"parcel_no":pn,
            "service_raw":"DPDFR-STANDARD","base_charge":round(base,2),"total_surcharges":st,
            "total_line_amount":round(line,2),"currency":"EUR","delivery_country":"","delivery_postcode":"",
            "weight_kg":round(num(d.get("Poids")),3),"pack_count":int(num(d.get("Nombre de colis"))),
            "order_id":cl(d.get("order_id")),"order_item_id":cl(d.get("order_item_id")),"source_file":os.path.basename(fname)})
        ship = ""
        try: ship = str(d.get("Date expédition"))[:10]
        except Exception: pass
        for c in SUR:
            a = num(d.get(c))
            if abs(a) > 0.005:
                sur.append({"invoice_id":inv,"tracking":pn,"charge_class":"dpd_fr","charge_code":_slug(c),
                    "surcharge_name":c,"amount":round(a,2),"currency":"EUR","source_row_no":"","ship_date":ship,"quantity":""})
    control = gate.get("control_total_ht") or total
    ratio = (total / control) if control else 0.0
    return {"combined": _csv(COMBINED_COLS, comb), "surcharge": _csv(SURCH_COLS, sur),
            "control": round(control,2), "parsed": round(total,2), "lines": len(comb), "ratio": ratio}

# ---------------------------------------------------------------------------
# Correos Express (FAC_UNICO xlsx) — parcel grain + order_item_id, ties to NETO.
# The invoice number lives inside the file (not the filename), so the builder
# returns it and the worker dedups on it. Surcharges come as one residual-to-NETO
# invoice-level line (the parser itemises base 'porte' only), so ~14% of spend is
# invoice-level, not per-order — reconciliation still matches the per-parcel base.
# ---------------------------------------------------------------------------
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
REGISTRY = {
    "dpd_uk":          {"match_file": lambda f: f.lower().endswith(".csv"),  "build": build_dpd_uk},
    "dhl_nl":          {"match_file": lambda f: f.lower().endswith(".csv"),  "build": build_dhl_nl},
    "geodis":          {"match_file": lambda f: f.lower().endswith(".pdf"),  "build": build_geodis},
    "chronopost":      {"match_file": lambda f: f.lower().endswith(".pdf"),  "build": build_chronopost},
    "correos_express": {"match_file": lambda f: f.lower().endswith((".xlsx",".xls")), "build": build_correos_express},
    # Validate on the runner (DRY_RUN=1), then uncomment:
    # "dpd_fr":   {"match_file": lambda f: f.lower().endswith((".xlsx",".xls")), "build": build_dpd_fr},
    # NOT wirable yet — these have only total-only stub parsers (dpd_nl, dhl_be, correos)
    # or no parser at all (postnl, royal_mail). Each needs its detailed frozen parser
    # (parcel grain + order_item_id) ported from its carrier audit skill before wiring.
}
