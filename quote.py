"""BOM + quote engine. Reads a System Surveyor survey doc, prices it, writes xlsx / Salesforce-ready csv."""
import csv, re
from collections import OrderedDict
from pathlib import Path

A = {"id": 141, "status": 138, "manufacturer": 271, "model": 305, "qty": 531, "price": 532, "hours": 533,
     "label": 173, "room": 255, "cable_len": 524, "cable_extra": 521, "cable_type": 526}
CABLE_ELEMENT = 65

SYN = {
    "part": ["product code", "productcode", "part number", "part #", "part", "sku", "model", "model number", "model #", "item number", "item"],
    "description": ["description", "product name", "name", "product description", "item description"],
    "qty": ["quantity", "qty", "count"],
    "manufacturer": ["manufacturer", "vendor", "brand", "make", "mfr"],
    "price": ["unit price", "sales price", "list price", "price", "sell price", "customer price", "unitprice"],
    "cost": ["cost", "unit cost", "dealer cost", "net cost", "cost price"],
}


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def num(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return default


def read_table(path):
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        ws = openpyxl.load_workbook(p, data_only=True).active
        rows = [[c for c in r] for r in ws.iter_rows(values_only=True)]
    else:
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return []
    hdr = [str(c or "").strip().lower() for c in rows[0]]
    return [dict(zip(hdr, r)) for r in rows[1:]]


def pick(row, key):
    for s in SYN[key]:
        if s in row and row[s] not in (None, ""):
            return row[s]
    return None


def parse_bom(path):
    """Salesforce export (csv/xlsx) -> normalized rows."""
    out = []
    for r in read_table(path):
        part, desc = pick(r, "part"), pick(r, "description")
        if not part and not desc:
            continue
        out.append({
            "part": str(part or "").strip(), "description": str(desc or "").strip(),
            "qty": int(num(pick(r, "qty"), 1) or 1), "manufacturer": str(pick(r, "manufacturer") or "").strip(),
            "price": num(pick(r, "price")), "cost": num(pick(r, "cost")),
        })
    return out


def load_pricebook(path):
    """normalized part/model -> {price, cost, description, manufacturer, part}"""
    pb = {}
    for r in parse_bom(path):
        pb[norm(r["part"])] = r
    return pb


def match_profile(profiles, part, manufacturer=""):
    """Find a saved System Surveyor preset whose model number matches a part number."""
    n = norm(part)
    if not n:
        return None
    best = None
    for p in profiles:
        a = {x["attribute_id"]: x["value"] for x in p["content"]["attribute"]}
        m = norm(a.get(A["model"]))
        if not m:
            continue
        if m == n:
            if not manufacturer or norm(manufacturer) in norm(a.get(A["manufacturer"])) or not a.get(A["manufacturer"]):
                return p
            best = best or p
    return best


def _attr(el, k):
    for a in el.get("attributes", []):
        if a["attribute_id"] == A[k]:
            return a.get("value")
    return None


def build_quote(doc, palette, profiles, pricebook=None, labor_rate=None, cable_per_ft=None, markup_pct=0.0, tax_pct=0.0):
    pricebook = pricebook or {}
    by_model = {}
    for p in profiles:
        a = {x["attribute_id"]: x["value"] for x in p["content"]["attribute"]}
        if a.get(A["model"]):
            by_model.setdefault(norm(a[A["model"]]), a)
    lines = OrderedDict()
    cable_ft, hours, gaps = 0.0, 0.0, []
    for e in doc.get("elements", []):
        eid = e.get("element_id")
        tname = palette.get(eid, {}).get("name", str(eid))
        q = int(num(_attr(e, "qty"), 1) or 1)
        hours += (num(_attr(e, "hours"), 0) or 0) * q
        if eid == CABLE_ELEMENT:
            ft = (num(_attr(e, "cable_len"), 0) or 0) + (num(_attr(e, "cable_extra"), 0) or 0)
            cable_ft += ft
            if not _attr(e, "cable_type"):
                gaps.append(f"{_attr(e, 'id')}: cable type not set ({ft:g} ft)")
            continue
        man, model = _attr(e, "manufacturer") or "", _attr(e, "model") or ""
        price = num(_attr(e, "price"))
        if model and not man:
            man = by_model.get(norm(model), {}).get(A["manufacturer"], "") or ""
        src = "survey"
        if price is None and model:
            pbr = pricebook.get(norm(model))
            if pbr and pbr.get("price") is not None:
                price, src = pbr["price"], "pricebook"
            elif norm(model) in by_model and num(by_model[norm(model)].get(A["price"])) is not None:
                price, src = num(by_model[norm(model)][A["price"]]), "SS preset"
        key = (tname, man, model, price)
        ln = lines.setdefault(key, {"type": tname, "manufacturer": man, "model": model, "unit_price": price, "price_source": src if price is not None else "", "qty": 0, "ids": []})
        ln["qty"] += q
        ln["ids"].append(_attr(e, "id"))
        if not model:
            gaps.append(f"{_attr(e, 'id')} ({tname}): no model number")
        elif price is None:
            gaps.append(f"{_attr(e, 'id')} ({tname} {model}): no price")
    items = list(lines.values())
    equip = sum((l["unit_price"] or 0) * l["qty"] for l in items)
    markup = equip * markup_pct / 100.0
    cable_cost = cable_ft * cable_per_ft if cable_per_ft is not None else 0.0
    labor = hours * labor_rate if labor_rate is not None else 0.0
    if cable_per_ft is None and cable_ft:
        gaps.append(f"cable: {cable_ft:g} ft unpriced (pass cable_per_ft)")
    if labor_rate is None and hours:
        gaps.append(f"labor: {hours:g} hrs unpriced (pass labor_rate)")
    sub = equip + markup + cable_cost + labor
    tax = sub * tax_pct / 100.0
    return {
        "title": doc.get("title"), "lines": items, "cable_ft": cable_ft, "labor_hours": hours,
        "equipment": round(equip, 2), "markup": round(markup, 2), "cable": round(cable_cost, 2), "labor": round(labor, 2),
        "subtotal": round(sub, 2), "tax": round(tax, 2), "total": round(sub + tax, 2), "gaps": gaps,
    }


def write_xlsx(q, path, labor_rate=None, cable_per_ft=None):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quote"
    ws.append([f"Quote - {q['title']}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Type", "Manufacturer", "Model", "Qty", "Unit Price", "Extended", "Price source"])
    for c in ws[3]:
        c.font = Font(bold=True)
    for l in q["lines"]:
        up = l["unit_price"]
        ws.append([l["type"], l["manufacturer"], l["model"], l["qty"], up, (up or 0) * l["qty"], l["price_source"]])
    if q["cable_ft"]:
        ws.append(["Cable", "", "", q["cable_ft"], cable_per_ft, q["cable"], "per ft"])
    ws.append(["Labor", "", f"{q['labor_hours']:g} hrs", q["labor_hours"], labor_rate, q["labor"], ""])
    ws.append([])
    for k in ("equipment", "markup", "cable", "labor", "subtotal", "tax", "total"):
        ws.append([k.title(), "", "", "", "", q[k]])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for col, w in zip("ABCDEFG", (26, 22, 24, 8, 12, 12, 14)):
        ws.column_dimensions[col].width = w
    for r in ws.iter_rows(min_row=4, min_col=5, max_col=6):
        for c in r:
            c.number_format = '"$"#,##0.00'
    g = wb.create_sheet("Gaps")
    g.append(["Needs attention"])
    for x in q["gaps"]:
        g.append([x])
    g.column_dimensions["A"].width = 80
    d = wb.create_sheet("Element IDs")
    d.append(["Type", "Model", "Element IDs"])
    for l in q["lines"]:
        d.append([l["type"], l["model"], ", ".join(map(str, l["ids"]))])
    wb.save(path)
    return str(path)


def write_sf_csv(q, path):
    """Quote-line import for Salesforce (ProductCode, Description, Quantity, UnitPrice)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ProductCode", "Description", "Manufacturer", "Quantity", "UnitPrice"])
        for l in q["lines"]:
            w.writerow([l["model"], l["type"], l["manufacturer"], l["qty"], l["unit_price"] if l["unit_price"] is not None else ""])
        if q["cable_ft"]:
            w.writerow(["CABLE", "Cable (ft)", "", q["cable_ft"], ""])
        if q["labor_hours"]:
            w.writerow(["LABOR", "Installation labor (hrs)", "", q["labor_hours"], ""])
    return str(path)
