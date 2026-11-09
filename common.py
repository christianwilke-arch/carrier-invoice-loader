"""Common line schema for all carrier invoice parsers.

Every parser returns a list[Line]. One row per charge component per shipment,
plus invoice-level charges (order_item_id=None). Downstream (enrich/reconcile)
joins on order_item_id to Cube order_items.expected_shipping_cost.
"""
from dataclasses import dataclass, asdict
from typing import Optional
import csv, subprocess, os

@dataclass
class Line:
    carrier: str                 # presta-aligned slug, e.g. "geodis", "dhl_nl"
    invoice_id: str
    invoice_date: str            # YYYY-MM-DD
    order_id: Optional[str]
    order_item_id: Optional[str] # THE reconciliation key (None for invoice-level charges)
    tracking: Optional[str]
    ship_date: Optional[str]     # YYYY-MM-DD (dispatch date for month bucketing)
    dest_country: Optional[str]
    dest_region: Optional[str]   # dept / zone
    weight_kg: Optional[float]
    product: Optional[str]
    charge_type: str             # base | option | fuel | surcharge | discount | total
    charge_label: str
    amount: float                # EUR, ex-VAT (net)
    currency: str
    source_file: str

def pdf_to_text(path: str) -> str:
    """pdftotext -layout wrapper (PDF formats: Geodis, Chronopost, DHL-BE, DPD NL...)."""
    txt = path + ".txt"
    subprocess.run(["pdftotext", "-layout", path, txt], check=True, stderr=subprocess.DEVNULL)
    return open(txt, encoding="utf-8", errors="replace").read()

def write_csv(lines, path):
    if not lines:
        open(path, "w").close(); return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(lines[0]).keys()))
        w.writeheader()
        for ln in lines: w.writerow(asdict(ln))
