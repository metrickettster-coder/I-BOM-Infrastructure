"""Purchasing documents -> I-BOM records.

Reads vendor quotes, purchase orders, invoices and receiving/inventory lists as
CSV, XLSX (needs openpyxl), PDF (needs pypdf) or plain text. Column names are
resolved through mappings/purchasing.json, so a new vendor or ERP export
format is a mapping edit, not a code change.

Each line moves its I-BOM component along the purchasing flow:
quote -> quoted, PO -> ordered, invoice -> in-transit, receipt -> received.
Price precedence is invoice > PO > quote; quantity comes from the PO when there
is one.
"""
import csv
import io
import json
import re
from datetime import date, timedelta
from pathlib import Path

from .core import money, slug

MAPPING_PATH = Path(__file__).resolve().parent / "mappings" / "purchasing.json"
STATUS = {"quote": "quoted", "po": "ordered", "invoice": "in-transit", "receipt": "received"}
COST_TYPE = {"quote": "quoted", "po": "quoted", "invoice": "actual"}
RANK = {"quote": 1, "po": 2, "invoice": 3, "receipt": 0}


def load_mapping(path=None):
    return json.loads(Path(path or MAPPING_PATH).read_text())


def _norm_header(h):
    h = re.sub(r"[^a-z0-9 ]+", " ", str(h or "").lower())
    return " ".join(w for w in h.split() if w not in ("no", "number", "num", "nr"))


def _header_index(mapping):
    idx = {}
    for field, aliases in mapping["columns"].items():
        for a in aliases + [field]:
            idx.setdefault(_norm_header(a), field)
    return idx


def _num(v):
    if v is None or str(v).strip() == "":
        return None
    if not isinstance(v, (int, float)):
        v = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
        if v in ("", "-", "."):
            return None
    f = float(v)
    return int(f) if f.is_integer() else f


def _date(v):
    if v is None or str(v).strip() == "":
        return None
    if hasattr(v, "date") and not isinstance(v, str):
        return v.date().isoformat() if hasattr(v, "hour") else v.isoformat()
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    m = re.match(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)  # US m/d/Y; SAP d.m.Y
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        mo, d = (b, a) if "." in s else (a, b)
        return date(y, mo, d).isoformat()
    return None


def _lookup(table, value):
    v = str(value or "").strip().lower()
    for key, aliases in table.items():
        if v == key.lower() or v in aliases:
            return key
    return None


def detect_doc_type(mapping, *hints):
    """Pick quote/po/invoice/receipt from an explicit value, the filename or document title."""
    for h in hints:
        if not h:
            continue
        t = _lookup(mapping["docTypes"], h)
        if t:
            return t
        words = set(re.split(r"[^a-z]+", str(h).lower()))
        for key, aliases in mapping["docTypes"].items():
            if any((" " in a and a in str(h).lower()) or a in words for a in aliases + [key]):
                return key
    return None


def classify(mapping, text):
    t = f" {text.lower()} "
    for rule in mapping["classify"]:
        if any(k in t for k in rule["any"]):
            return rule["class"], rule["category"]
    return "hardware", None


# ---------- readers: every reader yields (header_fields, rows[dict]) ----------

def _rows_from_table(table, mapping):
    idx = _header_index(mapping)
    header_row = None
    for i, row in enumerate(table[:25]):
        hits = sum(1 for cell in row if _norm_header(cell) in idx)
        if hits >= 3:
            header_row = i
            break
    if header_row is None:
        raise ValueError("no header row with at least 3 recognised columns; extend mappings/purchasing.json")
    fields = [idx.get(_norm_header(h)) for h in table[header_row]]
    preamble = {}
    for row in table[:header_row]:  # "Quote Number:,Q-123" style key/value lines above the table
        cells = [c for c in row if str(c or "").strip()]
        if len(cells) >= 2:
            f = idx.get(_norm_header(str(cells[0]).rstrip(":")))
            if f:
                preamble[f] = cells[1]
    rows = []
    for row in table[header_row + 1:]:
        rec = {f: v for f, v in zip(fields, row) if f and v not in (None, "")}
        if rec.get("description") or rec.get("mpn"):
            rows.append(rec)
    return preamble, rows


def read_csv(path, mapping):
    text = Path(path).read_text(encoding="utf-8-sig")
    return _rows_from_table(list(csv.reader(io.StringIO(text))), mapping)


def read_xlsx(path, mapping):
    try:
        import openpyxl
    except ImportError as e:
        raise SystemExit("reading .xlsx needs openpyxl: pip install openpyxl") from e
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    return _rows_from_table([list(r) for r in ws.iter_rows(values_only=True)], mapping)


def pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise SystemExit("reading .pdf needs pypdf: pip install pypdf") from e
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


def read_text(text, mapping):
    cfg = mapping["pdf"]
    header = {}
    for field, rx in cfg["header"].items():
        m = re.search(rx, text, re.I | re.M)
        if m:
            header[field] = m.group(1).strip()
    line_rx, serial_rx = re.compile(cfg["line"]), re.compile(cfg["serials"], re.I)
    rows = []
    for ln in text.splitlines():
        m = line_rx.match(ln)
        if m:
            rows.append({k: v for k, v in m.groupdict().items() if v})
            continue
        s = serial_rx.search(ln)
        if s and rows:
            rows[-1]["serials"] = (rows[-1].get("serials", "") + " " + s.group(1)).strip()
    first = text.strip().splitlines()[0] if text.strip() else ""
    header.setdefault("docType", first)
    return header, rows


def read_document(path, mapping):
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".csv":
        return read_csv(p, mapping)
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(p, mapping)
    if ext == ".pdf":
        return read_text(pdf_text(p), mapping)
    if ext in (".txt", ".eml", ".text"):
        return read_text(p.read_text(encoding="utf-8", errors="replace"), mapping)
    raise ValueError(f"unsupported purchasing file type: {p.name}")


# ---------- rows -> records ----------

ORDER = {"quote": 0, "po": 1, "invoice": 2, "receipt": 3}


def sort_documents(paths, mapping=None, doc_type=None):
    """Order files by document date, then quote < PO < invoice < receipt, so a folder of
    documents replays in business order whatever their file names."""
    mapping = mapping or load_mapping()

    def key(p):
        header, rows = read_document(p, mapping)
        first = rows[0] if rows else {}
        t = detect_doc_type(mapping, doc_type, header.get("docType"), first.get("docType"), Path(p).stem)
        return (_date(header.get("docDate") or first.get("docDate")) or "", ORDER.get(t, 9), str(p))
    return sorted(paths, key=key)


def parse(builder, path, mapping=None, doc_type=None, vendor=None, doc_number=None, doc_date=None,
          currency="USD", location=None):
    """Parse one purchasing document and upsert its lines. Returns the list of touched bom-refs."""
    mapping = mapping or load_mapping()
    header, rows = read_document(path, mapping)
    first = rows[0] if rows else {}
    dtype = detect_doc_type(mapping, doc_type, header.get("docType"), first.get("docType"), Path(path).stem)
    if dtype is None:
        raise ValueError(f"{path}: cannot tell whether this is a quote, PO, invoice or receipt; pass --type")
    vendor = vendor or header.get("vendor") or first.get("vendor")
    number = doc_number or header.get("docNumber") or first.get("docNumber") or Path(path).stem
    ddate = _date(doc_date or header.get("docDate") or first.get("docDate"))
    at = f"{ddate}T00:00:00Z" if ddate else None
    cur = (header.get("currency") or currency).upper()
    vendor_ref = builder.org(vendor, "supplier") if vendor else None
    if vendor_ref and (header.get("vendorId") or first.get("vendorId")):
        org = builder.component(vendor_ref)
        org.setdefault("identifiers", {})["erpVendorId"] = str(header.get("vendorId") or first.get("vendorId"))
    touched = []
    for i, row in enumerate(rows, 1):
        rec = _record(builder, mapping, row, dtype, number, ddate, at, cur, vendor_ref, header, path, i, location)
        if rec:
            touched.append(builder.upsert(rec))
    return dtype, number, touched


def _record(builder, mapping, row, dtype, number, ddate, at, cur, vendor_ref, header, path, i, location):
    desc = str(row.get("description") or row.get("mpn")).strip()
    mpn = str(row.get("mpn") or "").strip() or None
    spn = str(row.get("supplierPartNumber") or "").strip() or None
    cls = _lookup({k: [] for k in ("hardware", "firmware", "software", "virtual", "cloud-resource", "license",
                                    "consumable", "labor", "service", "soft-cost")}, row.get("class"))
    guess_cls, category = classify(mapping, f"{desc} {mpn or ''}")
    cls = cls or guess_cls
    category = row.get("category") or category
    qty = _num(row.get("quantity")) or 1
    uom = _lookup(mapping["uom"], row.get("uom")) or ("HR" if cls == "labor" else "EA")
    unit = _num(row.get("unitPrice"))
    ext = _num(row.get("extendedPrice"))
    if unit is None and ext is not None and qty:
        unit = ext / qty
    line_no = str(row.get("line") or i)

    c = {"class": cls, "name": desc[:120], "quantity": qty, "unitOfMeasure": uom}
    if category:
        c["category"] = str(category)
    if mpn:
        c["mpn"] = mpn
        if cls in ("hardware", "consumable"):
            c["model"] = mpn
    if row.get("material"):
        c["partNumber"] = str(row["material"])
    if len(desc) > 120:
        c["description"] = desc
    if row.get("manufacturer"):
        c["manufacturer"] = builder.org(str(row["manufacturer"]), "manufacturer")

    fin = {}
    if unit is not None and dtype != "receipt":
        fin["unitCost"] = money(unit, cur)
        fin["costType"] = COST_TYPE[dtype]
    if cls == "hardware":
        fin["expenseType"] = "capex"
    elif cls in ("service", "license"):
        fin["expenseType"] = "opex"
    for f in ("costCenter", "glAccount", "wbsElement"):
        if row.get(f):
            fin[f] = str(row[f])
    if fin:
        c["financial"] = fin

    if vendor_ref:
        src = {"supplier": vendor_ref, "tier": 1}
        if spn:
            src["supplierPartNumber"] = spn
        if unit is not None and dtype in ("quote", "po"):
            src["unitPrice"] = money(unit, cur)
        lt = _num(row.get("leadTimeDays"))
        if lt is not None:
            src["leadTimeDays"] = {"quoted": lt}
        if dtype == "po":
            src["contractRef"] = f"PO {number}"
        c["supply"] = {"sources": [src]}

    ids = {}
    erp = {}
    if dtype == "po":
        erp.update(purchaseOrder=str(number), poLine=line_no)
    elif header.get("poNumber") or row.get("poNumber"):
        erp["purchaseOrder"] = str(header.get("poNumber") or row.get("poNumber"))
    if dtype == "invoice":
        erp["invoice"] = str(number)
    if dtype == "receipt":
        erp["goodsReceipt"] = str(number)
    if row.get("material"):
        erp["materialNumber"] = str(row["material"])
    if erp:
        ids["erp"] = erp
    if dtype == "quote":
        ids["other"] = {"quote": str(number)}
    if ids:
        c["identifiers"] = ids

    lc = {}
    dates = {}
    if dtype == "po" and ddate:
        dates["ordered"] = ddate
        promised = _date(row.get("promisedDate"))
        lt = _num(row.get("leadTimeDays"))
        if not promised and lt is not None:
            promised = (date.fromisoformat(ddate) + timedelta(days=int(lt))).isoformat()
        if promised:
            dates["promised"] = promised
    if dtype == "receipt" and ddate:
        dates["received"] = ddate
    if dates:
        lc["dates"] = dates
    if lc:
        c["lifecycle"] = lc

    serials = [s for s in re.split(r"[\s,;|]+", str(row.get("serials") or "")) if s]
    if serials:
        existing = builder.find([f"part:{x}" for x in (mpn, spn) if x])
        if qty == 1 and len(serials) == 1 and (existing is None or existing["quantity"] == 1) \
                and not (existing or {}).get("units"):
            c["serialNumber"] = serials[0]
        else:
            c["units"] = [{"serialNumber": s} for s in serials]
            c["quantity"] = max(qty, len(serials))
    if row.get("assetTag") and qty == 1:
        c["assetTag"] = str(row["assetTag"])
    loc = location or row.get("location")
    if loc and cls in ("hardware", "consumable"):
        ref = builder.location(f"loc-{slug(loc, 40)}", "warehouse" if dtype == "receipt" else "site", str(loc))
        c["placement"] = {"location": ref}

    if cls == "labor":
        hours = qty if uom == "HR" else qty * 8 if uom == "DAY" else qty
        c["labor"] = {"role": mapping.get("laborRoleDefault", "technician"), "hours": hours,
                      "activity": "cabling" if "cabl" in desc.lower() else
                      "configuration" if "config" in desc.lower() else "installation"}
        if vendor_ref:
            c["labor"]["provider"] = vendor_ref
    if cls == "license":
        c["licensing"] = {"licenseType": "subscription" if "subscr" in desc.lower() else "term"}

    # MPN and supplier SKU share one key space: an invoice often prints the SKU in the part column
    keys = [f"part:{x}" for x in (mpn, spn) if x] + [f"serial:{s}" for s in serials]
    if not mpn and not spn:
        keys.append(f"desc:{slug(desc, 60)}")
    known = builder.find(keys)
    if known and mpn and not known.get("mpn") and any(
            s.get("supplierPartNumber") == mpn for s in known.get("supply", {}).get("sources", [])):
        c.pop("mpn", None)
        c.pop("model", None)
    prefix = {"hardware": "hw", "labor": "lab", "service": "svc", "license": "lic", "consumable": "con",
              "soft-cost": "soft"}.get(cls, cls)

    rec = {"component": c, "keys": keys, "refHint": f"{prefix}-{slug(mpn or spn or desc, 40)}",
           "source": {"kind": dtype, "id": str(number), "file": Path(path).name, "line": line_no}}
    rank = {"financial": RANK[dtype]} if dtype != "receipt" else {}
    if dtype in ("quote", "po"):
        rank["quantity"] = RANK[dtype]
    rec["rank"] = rank
    if at:
        status = STATUS[dtype]
        backorder = _num(row.get("backorderQty")) or 0
        if dtype == "receipt" and backorder > 0:
            status = "backordered"
            rec["note"] = f"received {qty:g}, {backorder:g} on backorder (receipt {number})"
        elif dtype != "quote":
            rec["note"] = f"{dtype} {number} line {line_no}"
        if dtype == "invoice" and cls in ("license", "service"):
            status = "in-service"  # an invoiced entitlement or contract is live
        elif dtype == "invoice" and cls in ("labor", "soft-cost"):
            status = "received"  # work performed / charge incurred
        rec["status"], rec["at"] = status, at
        rec["force"] = status == "backordered"
        if dtype == "receipt" and backorder == 0:
            existing = builder.find(keys)
            ordered = existing["quantity"] if existing else qty
            if qty < ordered:
                rec["status"] = "in-transit"
                rec["note"] = f"partial receipt {qty:g} of {ordered:g} (receipt {number})"
    return rec
