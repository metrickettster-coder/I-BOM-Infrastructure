"""Document builder: loads or creates an I-BOM and upserts records from any parser.

Every parser emits *records*: a partial component plus the keys that identify
the same real-world item in other sources (MPN, serial number, cloud resource
id, IaC address). The builder finds the existing line for those keys, merges
the new facts in, advances the lifecycle status, and appends history events.
Running the same input twice changes nothing, so ingestion can run on a
schedule.
"""
import copy
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import TOOL

EXT = "ibom.dev/ingest"  # namespace for provenance and ingest bookkeeping inside `extensions`
SPEC_VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "ibom.schema.json"

# Forward flow from plan to production. A record can only move a line forward
# along this list; operational states (degraded, rma, ...) are set explicitly.
FLOW = ["planned", "quoted", "ordered", "backordered", "in-transit", "received", "staged",
        "burn-in", "deployed", "in-service"]
OPERATIONAL = {"degraded", "failed", "in-repair", "rma", "spare", "decommissioned", "disposed"}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(text, maxlen=48):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-._").lower()
    return (s[:maxlen].rstrip("-._") or "item")


def money(amount, currency="USD"):
    return {"amount": round(float(amount), 2), "currency": currency}


def norm_key(key):
    kind, _, value = key.partition(":")
    return f"{kind}:{value.strip().upper()}" if kind in ("part", "serial") else f"{kind}:{value.strip()}"


def _event_key(e):
    return (e.get("at"), e.get("type"), e.get("from"), e.get("to"), e.get("note"))


def deep_merge(dst, src, path=""):
    """Merge src into dst. Dicts recurse, scalars overwrite, lists merge by identity."""
    for k, v in src.items():
        p = f"{path}/{k}"
        if k not in dst or dst[k] is None:
            dst[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            deep_merge(dst[k], v, p)
        elif isinstance(v, list) and isinstance(dst[k], list):
            dst[k] = merge_list(dst[k], v, k)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def merge_list(old, new, key):
    id_field = {"units": "serialNumber", "sources": "supplier"}.get(key)
    out = copy.deepcopy(old)
    for item in new:
        if key == "events":
            if all(_event_key(e) != _event_key(item) for e in out):
                out.append(copy.deepcopy(item))
            continue
        if id_field and isinstance(item, dict) and id_field in item:
            match = next((o for o in out if isinstance(o, dict) and o.get(id_field) == item[id_field]), None)
            if match is not None:
                deep_merge(match, item)
                continue
        if item not in out:
            out.append(copy.deepcopy(item))
    if key == "events":
        out.sort(key=lambda e: e.get("at", ""))
    return out


class Builder:
    def __init__(self, doc):
        self.doc = doc
        self.doc.setdefault("organizations", [])
        self.doc.setdefault("locations", [])
        self.doc.setdefault("components", [])
        self._original = self._content()
        self._reindex()

    def _content(self):
        return json.dumps({k: v for k, v in self.doc.items() if k not in ("version", "metadata")}, sort_keys=True)

    # ---------- construction ----------
    @classmethod
    def new(cls, project_id, project_name, project_type="other", author=None, currency="USD", budget=None,
            timestamp=None):
        meta = {
            "timestamp": timestamp or now_iso(),
            "tools": [dict(TOOL)],
            "project": {"id": project_id, "name": project_name, "type": project_type},
            "bomViews": ["infrastructure", "procurement", "as-built", "as-maintained"],
            "structure": "multi-level",
        }
        if author:
            meta["authors"] = [{"name": author}]
        if budget:
            meta["project"]["budget"] = money(budget, currency)
        doc = {"bomFormat": "I-BOM", "specVersion": SPEC_VERSION, "serialNumber": f"urn:uuid:{uuid.uuid4()}",
               "version": 1, "metadata": meta, "components": []}
        return cls(doc)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    # ---------- indexes ----------
    def _reindex(self):
        self.by_ref = {}
        self.by_key = {}
        for section in ("organizations", "locations", "components"):
            for item in self.doc.get(section, []):
                self.by_ref[item["bom-ref"]] = item
        for c in self.doc["components"]:
            for k in self.keys_of(c):
                self.by_key.setdefault(k, c["bom-ref"])

    @staticmethod
    def keys_of(c):
        keys = set(c.get("extensions", {}).get(EXT, {}).get("keys", []))
        if c.get("serialNumber"):
            keys.add(f"serial:{c['serialNumber']}")
        for u in c.get("units", []):
            keys.add(f"serial:{u['serialNumber']}")
        v = c.get("virtual", {})
        if v.get("resourceId"):
            keys.add(f"cloud:{v['resourceId']}")
        if v.get("iac", {}).get("address"):
            keys.add(f"iac:{v['iac'].get('tool', 'other')}:{v['iac']['address']}")
        ids = c.get("identifiers", {})
        if ids.get("cloud", {}).get("resourceId"):
            keys.add(f"cloud:{ids['cloud']['resourceId']}")
        return {norm_key(k) for k in keys}

    def find(self, keys):
        for k in keys:
            ref = self.by_key.get(norm_key(k))
            if ref:
                return self.by_ref[ref]
        return None

    def component(self, ref):
        return self.by_ref.get(ref)

    # ---------- reference data ----------
    def org(self, name, role, **extra):
        """Return the bom-ref of the organization with this name, creating it if needed."""
        name = name.strip()
        for o in self.doc["organizations"]:
            if o["name"].lower() == name.lower():
                if role not in o["roles"]:
                    o["roles"].append(role)
                return o["bom-ref"]
        ref = self._unique_ref(f"org-{slug(name, 32)}")
        o = {"bom-ref": ref, "name": name, "roles": [role], **extra}
        self.doc["organizations"].append(o)
        self.by_ref[ref] = o
        return ref

    def location(self, ref, type_, name, **extra):
        if ref not in self.by_ref:
            loc = {"bom-ref": ref, "type": type_, "name": name, **extra}
            self.doc["locations"].append(loc)
            self.by_ref[ref] = loc
        return ref

    def relate(self, frm, type_, to):
        rels = self.doc.setdefault("relationships", [])
        r = {"from": frm, "type": type_, "to": to}
        if r not in rels:
            rels.append(r)

    def observe(self, obs):
        """Add or update an observation by id; returns True if it is new."""
        observations = self.doc.setdefault("observations", [])
        for o in observations:
            if o["id"] == obs["id"]:
                o.update(obs)
                return False
        observations.append(obs)
        return True

    def _unique_ref(self, base):
        ref, n = base, 2
        while ref in self.by_ref:
            ref, n = f"{base}-{n}", n + 1
        return ref

    # ---------- upsert ----------
    def upsert(self, rec):
        """Merge one parser record into the document; returns the component's bom-ref.

        rec = {
          "component": {...partial component...},
          "keys": ["part:<mpn or sku>", "serial:...", "cloud:...", "iac:terraform:addr"],
          "refHint": "hw-r760xa",            # preferred bom-ref for a new line
          "ref": "hw-r760xa",                # optional: merge into this exact line
          "status": "ordered", "at": "2026-09-01T00:00:00Z",   # lifecycle transition
          "force": False,                    # allow non-forward / operational status
          "rank": {"financial": 2, "quantity": 2},  # precedence of this source
          "source": {"kind": "po", "id": "4500018823", "file": "po.csv", "line": "10"},
          "events": [...extra lifecycle events...],
        }
        """
        part = copy.deepcopy(rec["component"])
        embedded = part.get("lifecycle", {}).pop("status", None)  # status only moves via set_status
        if embedded and not rec.get("status"):
            rec = {**rec, "status": embedded}
        keys = [k for k in rec.get("keys", []) if k and k.split(":", 1)[1]]
        existing = self.by_ref.get(rec["ref"]) if rec.get("ref") else self.find(keys)
        rank = rec.get("rank", {})
        if existing is None:
            ref = part.get("bom-ref") or self._unique_ref(rec.get("refHint") or f"{part['class']}-{slug(part['name'])}")
            part["bom-ref"] = ref
            part.setdefault("quantity", 1)
            c = part
            self.doc["components"].append(c)
            self.by_ref[ref] = c
        else:
            c = existing
            ref = c["bom-ref"]
            ranks = c.get("extensions", {}).get(EXT, {}).get("ranks", {})
            if rank.get("financial", 0) < ranks.get("financial", 0):
                for f in ("unitCost", "extendedCost", "costType"):
                    part.get("financial", {}).pop(f, None)
                for src in part.get("supply", {}).get("sources", []):
                    src.pop("unitPrice", None)
            if rank.get("quantity", 0) < ranks.get("quantity", 0) or "quantity" not in rank:
                part.pop("quantity", None)
            part.pop("bom-ref", None)
            for k in ("name", "class", "category", "licensing"):  # the first source names and classifies
                if k in c:                                           # the line; later ones add facts
                    part.pop(k, None)
            deep_merge(c, part)

        ext = c.setdefault("extensions", {}).setdefault(EXT, {})
        ext["keys"] = sorted(set(ext.get("keys", [])) | {norm_key(k) for k in keys})
        if rank:
            er = ext.setdefault("ranks", {})
            for k, v in rank.items():
                er[k] = max(er.get(k, 0), v)
        if rec.get("source"):
            srcs = ext.setdefault("sources", [])
            if rec["source"] not in srcs:
                srcs.append(rec["source"])
        for k in self.keys_of(c):
            self.by_key.setdefault(k, ref)

        if rec.get("status"):
            self.set_status(c, rec["status"], rec.get("at") or now_iso(), force=rec.get("force", False),
                            actor=rec.get("actor"), note=rec.get("note"), adopt=rec.get("adopt", True))
        for e in rec.get("events", []):
            self.add_event(c, e)
        return ref

    # ---------- lifecycle ----------
    def set_status(self, c, status, at, force=False, actor=None, note=None, adopt=False):
        """Move a line's lifecycle status and log it. Only declaring sources (purchasing, IaC,
        config management) may adopt a discovered-unmanaged line, via adopt=True."""
        lc = c.setdefault("lifecycle", {})
        cur = lc.get("status")
        if cur == status:
            return False
        last = max((e["at"] for e in lc.get("events", []) if e["type"] == "status-change"), default="")
        if at < last:
            return False  # older than the latest known transition: a replay or late-arriving document
        adopt = adopt and cur == "discovered-unmanaged" and status in FLOW
        forward = cur in (None, *FLOW) and status in FLOW and (cur is None or FLOW.index(status) > FLOW.index(cur))
        recover = cur in {"degraded", "failed", "in-repair"} and status == "in-service"
        if not (force or adopt or forward or recover):
            return False
        lc["status"] = status
        if adopt:  # a declaring source now owns this line: close its shadow finding
            for o in self.doc.get("observations", []):
                if o.get("component") == c["bom-ref"] and o["kind"] == "unmanaged" and o.get("status") == "open":
                    o["status"] = "accepted"
            note = note or "adopted into the BOM"
        e = {"at": at, "type": "status-change", "to": status}
        if cur:
            e["from"] = cur
        if actor:
            e["actor"] = actor
        if note:
            e["note"] = note
        self.add_event(c, e)
        return True

    @staticmethod
    def add_event(c, e):
        events = c.setdefault("lifecycle", {}).setdefault("events", [])
        c["lifecycle"]["events"] = merge_list(events, [e], "events")

    # ---------- output ----------
    def finalize(self, timestamp=None):
        self.doc["components"] = [_ordered(c) for c in self.doc["components"]]
        self._reindex()
        for c in self.doc["components"]:
            if isinstance(c["quantity"], float) and c["quantity"].is_integer():
                c["quantity"] = int(c["quantity"])
            fin = c.get("financial")
            if fin and "unitCost" in fin:
                fin["extendedCost"] = money(fin["unitCost"]["amount"] * c["quantity"], fin["unitCost"]["currency"])
        meta = self.doc["metadata"]
        tools = meta.setdefault("tools", [])
        if not any(t.get("name") == TOOL["name"] for t in tools):
            tools.append(dict(TOOL))
        if self.changed():
            meta["timestamp"] = timestamp or now_iso()
            self.doc["version"] = self.doc.get("version", 0) + 1
            self._original = self._content()
        for section in ("organizations", "locations", "relationships", "observations"):
            if section in self.doc and not self.doc[section]:
                del self.doc[section]
        return self.doc

    def changed(self):
        """True if anything but version/metadata differs from what was loaded (or last finalized)."""
        return self._content() != self._original

    def save(self, path, validate=True, timestamp=None):
        doc = self.finalize(timestamp)
        if validate:
            errors = validate_doc(doc)
            if errors:
                raise ValueError("I-BOM failed validation:\n  " + "\n  ".join(errors[:20]))
        Path(path).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return doc


KEY_ORDER = ["bom-ref", "class", "category", "partNumber", "name", "description", "version", "manufacturer",
             "model", "mpn", "purl", "cpe", "quantity", "unitOfMeasure", "parent", "serialNumber", "assetTag",
             "units", "placement"]


def _ordered(c):
    """Stable, readable key order: identity first, then everything else as it was."""
    head = {k: c[k] for k in KEY_ORDER if k in c}
    tail = {k: v for k, v in c.items() if k not in head and k != "extensions"}
    out = {**head, **tail}
    if "extensions" in c:
        out["extensions"] = c["extensions"]
    return out


def validate_doc(doc):
    """Schema + integrity check using the repo's validator. Returns a list of error strings."""
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    from ibom_validate import check_integrity  # noqa: E402
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads(SCHEMA_PATH.read_text())
    v = Draft202012Validator(schema, format_checker=FormatChecker())
    errs = [f"SCHEMA /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(doc)]
    if errs:
        return errs
    return check_integrity(doc)[0]
