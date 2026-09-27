"""Dynamic lifecycle tracking: health, firmware history, vendor EOL status and a lifecycle report.

Snapshots arrive over time (Redfish pulls, Ansible fact runs, EOL catalog
refreshes). Each one updates the current view on the component and appends to
its `lifecycle.events` history, so the I-BOM is a running record rather than a
static spreadsheet:

  * observe_firmware  - firmware-update event whenever a unit's BIOS/BMC version changes
  * observe_health    - line and per-unit health; critical -> degraded, healthy again -> in-service
  * ingest_redfish    - DMTF Redfish ComputerSystem/Manager/Thermal/Power resources
  * apply_eol         - vendor lifecycle catalog (or endoflife.date data) -> vendorLifecycle stage,
                        re-evaluated against today's date, eol-notice event when the stage moves
  * report            - late deliveries, EOL horizon, health, firmware changes, shadow items
"""
import json
import re
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from .core import slug

EXT = "ibom.dev/lifecycle"
CATALOG_PATH = Path(__file__).resolve().parent / "catalogs" / "vendor_lifecycle.json"
HEALTH_RANK = {"healthy": 0, "unknown": 1, "offline": 2, "warning": 3, "critical": 4}
REDFISH_HEALTH = {"OK": "healthy", "Warning": "warning", "Critical": "critical"}


def _unit(comp, unit_id):
    return next((u for u in comp.get("units", []) if u["serialNumber"] == unit_id), None)


# ---------- firmware ----------

def observe_firmware(builder, comp, unit_id, firmware, at, source):
    """Record observed firmware for one unit; append firmware-update events on change."""
    store = comp.setdefault("extensions", {}).setdefault(EXT, {}).setdefault("observedFirmware", {})
    prev = store.get(unit_id, {})
    if prev.get("at") and prev["at"] > at:
        return False  # older snapshot than what we already have
    changed = False
    for kind, version in firmware.items():
        old = prev.get(kind)
        if old and old != version:
            builder.add_event(comp, {"at": at, "type": "firmware-update", "from": f"{kind.upper()} {old}",
                                     "to": f"{kind.upper()} {version}", "note": f"{unit_id} via {source}"})
            changed = True
    if all(prev.get(k) == v for k, v in firmware.items()):
        return False  # nothing new; keep the time this version was first seen
    new = {**prev, **firmware, "at": at, "source": source}
    if new != prev:
        store[unit_id] = new
    return changed


# ---------- health ----------

def observe_health(builder, comp, unit_id, health, at, running=True):
    """Set health on the unit (if serialized) and the line, and move status on critical/recovery."""
    health = {"observedAt": at, **health}
    unit = _unit(comp, unit_id)
    lc = comp.setdefault("lifecycle", {})
    if unit is not None:
        unit["health"] = health
        unit_healths = [u["health"] for u in comp["units"] if u.get("health")]
        worst = max(unit_healths, key=lambda h: HEALTH_RANK.get(h["status"], 1))
        line = dict(worst)
        if len(comp["units"]) > 1:
            line.pop("metrics", None)
            line["metrics"] = {f"units{s.title()}": sum(1 for h in unit_healths if h["status"] == s)
                               for s in ("healthy", "warning", "critical", "offline")}
        lc["health"] = line
    else:
        lc["health"] = health
    status = lc.get("status")
    if status == "discovered-unmanaged":
        return
    if health["status"] == "critical":
        if builder.set_status(comp, "degraded", at, force=status != "degraded",
                              note=f"{unit_id} reported critical health via {health.get('source')}"):
            builder.add_event(comp, {"at": at, "type": "alert", "note": f"{unit_id} health critical"})
    elif health["status"] == "healthy" and running:
        if status == "degraded" and lc["health"]["status"] == "healthy":
            builder.set_status(comp, "in-service", at, note="health recovered")
        elif status in ("received", "staged", "burn-in", "deployed", "ordered", "in-transit", "backordered"):
            builder.set_status(comp, "in-service", at, note=f"powered on and healthy per {health.get('source')}")


# ---------- Redfish ----------

def read_redfish(path):
    """A snapshot file is {"collectedAt", "endpoint", "resources": [...]}; a bare resource also works."""
    data = json.loads(Path(path).read_text())
    resources = data.get("resources", [data])
    by_type = {}
    for r in resources:
        t = r.get("@odata.type", "")
        for kind in ("ComputerSystem", "Manager", "Thermal", "Power", "EnvironmentMetrics", "Chassis"):
            if f"#{kind}." in t:
                by_type.setdefault(kind, r)
    return data.get("collectedAt"), data.get("endpoint"), by_type


def ingest_redfish(builder, paths, observed_at, source="redfish"):
    summary = {"snapshots": 0, "updated": [], "shadow": [], "firmwareChanges": 0}
    for path in sorted(paths, key=lambda p: (read_redfish(p)[0] or "", str(p))):
        at, endpoint, r = read_redfish(path)
        at = at or observed_at
        system = r.get("ComputerSystem")
        if not system:
            continue
        summary["snapshots"] += 1
        serial = system.get("SerialNumber", "").strip()
        comp = builder.find([f"serial:{serial}"]) if serial else None
        if comp is None:
            comp = _shadow_server(builder, system, endpoint, at, source)
            summary["shadow"].append(comp["bom-ref"])
        st = system.get("Status", {})
        power_on = system.get("PowerState", "On") == "On"
        hstat = REDFISH_HEALTH.get(st.get("HealthRollup") or st.get("Health"), "unknown")
        if not power_on or st.get("State") in ("Absent", "Disabled", "UnavailableOffline"):
            hstat = "offline" if hstat == "healthy" else hstat
        metrics = {}
        th = r.get("Thermal", {})
        for t in th.get("Temperatures", []):
            if "inlet" in t.get("Name", "").lower() and t.get("ReadingCelsius") is not None:
                metrics["inletTempC"] = t["ReadingCelsius"]
        em = r.get("EnvironmentMetrics", {})
        if em.get("TemperatureCelsius", {}).get("Reading") is not None:
            metrics.setdefault("inletTempC", em["TemperatureCelsius"]["Reading"])
        pw = (r.get("Power", {}).get("PowerControl") or [{}])[0].get("PowerConsumedWatts") \
            or em.get("PowerWatts", {}).get("Reading")
        if pw is not None:
            metrics["powerW"] = pw
        mem = system.get("MemorySummary", {}).get("Status", {}).get("HealthRollup")
        if mem and mem != "OK":
            metrics["memoryHealth"] = mem
        health = {"status": hstat, "source": source}
        if metrics:
            health["metrics"] = metrics
        observe_health(builder, comp, serial, health, at, running=power_on)
        fw = {}
        if system.get("BiosVersion"):
            fw["bios"] = system["BiosVersion"]
        if r.get("Manager", {}).get("FirmwareVersion"):
            fw["bmc"] = r["Manager"]["FirmwareVersion"]
        if observe_firmware(builder, comp, serial, fw, at, source):
            summary["firmwareChanges"] += 1
        if comp["bom-ref"] not in summary["updated"]:
            summary["updated"].append(comp["bom-ref"])
    return summary


def _shadow_server(builder, system, endpoint, at, source):
    serial = system.get("SerialNumber", "").strip() or slug(endpoint or system.get("Id", "unknown"))
    vendor = system.get("Manufacturer", "").strip()
    model = system.get("Model", "").strip()
    c = {"class": "hardware", "category": "server", "name": f"{vendor} {model} (unmanaged)".strip(),
         "quantity": 1, "serialNumber": serial,
         "notes": f"Answered Redfish at {endpoint} but is not in purchasing records. Review and adopt or retire."}
    if model:
        c["model"] = model
    if vendor:
        c["manufacturer"] = builder.org(vendor, "manufacturer")
    ref = builder.upsert({"component": c, "keys": [f"serial:{serial}"], "refHint": f"shadow-{slug(serial)}",
                          "status": "discovered-unmanaged", "at": at, "force": True,
                          "note": f"discovered by {source}", "source": {"kind": source, "id": endpoint or serial}})
    builder.observe({"id": f"OBS-UNMANAGED-{slug(serial, 60)}", "observedAt": at, "source": source,
                     "kind": "unmanaged", "component": ref, "severity": "medium", "status": "open",
                     "discovered": {"class": "hardware", "name": f"{vendor} {model}".strip(),
                                    "serialNumber": serial, "endpoint": endpoint}})
    return builder.component(ref)


# ---------- vendor lifecycle / EOL ----------

def load_catalog(path=None):
    return json.loads(Path(path or CATALOG_PATH).read_text())["entries"]


def _match_text(builder, c):
    mfr = builder.component(c["manufacturer"])["name"] if c.get("manufacturer") in builder.by_ref else ""
    return " ".join(str(x) for x in (mfr, c.get("model", ""), c.get("mpn", ""), c.get("name", ""),
                                     c.get("version", ""), c.get("purl", "")) if x)


def stage_on(vl, today):
    """Stage implied by the milestone dates as of `today`, falling back to the declared stage."""
    for field, stage in (("endOfLife", "end-of-life"), ("endOfSupport", "end-of-support"),
                         ("endOfSale", "end-of-sale"), ("lastTimeBuy", "last-time-buy")):
        if vl.get(field) and vl[field] <= today:
            return stage
    return vl.get("stage", "active")


def apply_eol(builder, entries, today, checked_at):
    """Match catalog entries to components and set lifecycle.vendorLifecycle. Returns changed refs."""
    changed = []
    for c in builder.doc["components"]:
        if c["class"] not in ("hardware", "firmware", "software", "license"):
            continue
        text = _match_text(builder, c)
        entry = next((e for e in entries if re.search(e["match"], text, re.I)), None)
        if not entry:
            continue
        vl = {k: v for k, v in entry["vendorLifecycle"].items() if v is not None}
        vl["stage"] = stage_on(vl, today)
        vl["checkedAt"] = checked_at
        lc = c.setdefault("lifecycle", {})
        old = lc.get("vendorLifecycle", {})
        if old.get("stage") and old["stage"] != vl["stage"]:
            builder.add_event(c, {"at": checked_at, "type": "eol-notice", "from": old["stage"], "to": vl["stage"],
                                  "note": vl.get("source", "")})
        elif not old.get("stage") and vl["stage"] not in ("active", "preview", "unknown"):
            builder.add_event(c, {"at": checked_at, "type": "eol-notice", "to": vl["stage"],
                                  "note": vl.get("source", "")})
        if {k: v for k, v in old.items() if k != "checkedAt"} != {k: v for k, v in vl.items() if k != "checkedAt"}:
            changed.append(c["bom-ref"])
        lc["vendorLifecycle"] = vl
    return changed


def fetch_endoflife(product, cache_dir):
    """Download https://endoflife.date/api/<product>.json into cache_dir (network needed)."""
    url = f"https://endoflife.date/api/{product}.json"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    (Path(cache_dir) / f"{product}.json").write_text(json.dumps(data, indent=2) + "\n")
    return data


def endoflife_entries(product, cycles, name_pattern=None):
    """Convert endoflife.date cycles into catalog entries."""
    name_pattern = name_pattern or re.escape(product)
    out = []
    for cy in cycles:
        dates = {}
        eol = cy.get("eol")
        ext = cy.get("extendedSupport")
        if isinstance(eol, str):
            dates["endOfSupport"] = eol
        if isinstance(ext, str):
            dates["endOfLife"] = ext
        elif isinstance(eol, str):
            dates["endOfLife"] = eol
        if eol is True:
            dates["stage"] = "end-of-life"
        out.append({
            "match": rf"{name_pattern}\b.*(?<![\d.]){re.escape(str(cy['cycle']))}(?![\d])",
            "vendorLifecycle": {"stage": dates.pop("stage", "active"), **{k: v for k, v in dates.items() if v},
                                "announced": cy.get("releaseDate"), "successor": None,
                                "source": f"https://endoflife.date/{product}"},
        })
    return out


# ---------- report ----------

def report(doc, today, horizon_days=365):
    horizon = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
    recent = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    r = {"asOf": today, "horizonDays": horizon_days, "statusCounts": {}, "late": [], "eol": [], "health": [],
         "firmwareChanges": [], "shadow": [], "missing": [], "expiring": []}
    for c in doc["components"]:
        ref, lc = c["bom-ref"], c.get("lifecycle", {})
        st = lc.get("status", "unknown")
        r["statusCounts"][st] = r["statusCounts"].get(st, 0) + 1
        promised = lc.get("dates", {}).get("promised")
        if st in ("ordered", "backordered", "in-transit") and promised and promised < today \
                and c["class"] in ("hardware", "firmware", "consumable"):
            r["late"].append({"ref": ref, "name": c["name"], "status": st, "promised": promised,
                              "daysLate": (date.fromisoformat(today) - date.fromisoformat(promised)).days})
        vl = lc.get("vendorLifecycle", {})
        upcoming = sorted((vl[k], k) for k in ("lastTimeBuy", "endOfSale", "endOfSupport", "endOfLife")
                          if vl.get(k) and today <= vl[k] <= horizon)
        if vl.get("stage") not in (None, "active", "preview", "unknown") or upcoming:
            r["eol"].append({"ref": ref, "name": c["name"], "stage": vl.get("stage"),
                             "next": {"date": upcoming[0][0], "milestone": upcoming[0][1]} if upcoming else None,
                             "successor": vl.get("successor"), "quantity": c["quantity"]})
        h = lc.get("health", {})
        if h.get("status") in ("warning", "critical", "offline"):
            bad = [u["serialNumber"] for u in c.get("units", [])
                   if u.get("health", {}).get("status") in ("warning", "critical", "offline")]
            r["health"].append({"ref": ref, "name": c["name"], "status": h["status"], "units": bad,
                                "metrics": h.get("metrics", {})})
        for e in lc.get("events", []):
            if e["type"] == "firmware-update" and e["at"][:10] >= recent:
                r["firmwareChanges"].append({"ref": ref, "at": e["at"], "from": e.get("from"), "to": e.get("to"),
                                             "note": e.get("note")})
        if st == "discovered-unmanaged":
            cost = c.get("financial", {}).get("recurringCost", {}).get("amount", {}).get("amount")
            r["shadow"].append({"ref": ref, "name": c["name"], "class": c["class"], "estimatedMonthlyCost": cost})
        d = lc.get("dates", {})
        for k in ("warrantyEnd", "supportContractEnd"):
            if d.get(k) and today <= d[k] <= horizon:
                r["expiring"].append({"ref": ref, "name": c["name"], "what": k, "date": d[k]})
        lic = c.get("licensing", {})
        if lic.get("validTo") and lic["validTo"] <= horizon:
            r["expiring"].append({"ref": ref, "name": c["name"], "what": "license", "date": lic["validTo"]})
    for o in doc.get("observations", []):
        if o["kind"] == "missing" and o.get("status") == "open":
            r["missing"].append({"ref": o.get("component"), "source": o["source"], "observedAt": o["observedAt"]})
    r["eol"].sort(key=lambda x: (x["next"] or {}).get("date", "0"))
    return r


def render(r):
    out = [f"I-BOM lifecycle report as of {r['asOf']} (horizon {r['horizonDays']} days)", ""]
    out.append("Status: " + ", ".join(f"{k} {v}" for k, v in sorted(r["statusCounts"].items())))
    sections = [
        ("Late deliveries", r["late"], lambda x: f"{x['ref']}: {x['status']}, promised {x['promised']} ({x['daysLate']} days late)"),
        ("Vendor lifecycle / EOL", r["eol"], lambda x: f"{x['ref']} (qty {x['quantity']:g}): {x['stage']}"
            + (f", {x['next']['milestone']} {x['next']['date']}" if x["next"] else "")
            + (f", successor {x['successor']}" if x.get("successor") else "")),
        ("Health", r["health"], lambda x: f"{x['ref']}: {x['status']}"
            + (f" (unit {', '.join(x['units'])})" if x["units"] else "")
            + ("; " + ", ".join(f"{k}={v}" for k, v in x["metrics"].items() if v) if x["metrics"] else "")),
        ("Firmware changes, last 30 days", r["firmwareChanges"], lambda x: f"{x['ref']}: {x['from']} -> {x['to']} at {x['at']} ({x['note']})"),
        ("Shadow infrastructure (discovered, not in BOM)", r["shadow"], lambda x: f"{x['ref']}: {x['name']}"
            + (f", est. {x['estimatedMonthlyCost']:,.0f} USD/month" if x["estimatedMonthlyCost"] else "")),
        ("Declared but not found", r["missing"], lambda x: f"{x['ref']} (checked by {x['source']} {x['observedAt']})"),
        ("Warranty, support and licenses expiring", r["expiring"], lambda x: f"{x['ref']}: {x['what']} {x['date']}"),
    ]
    for title, items, fmt in sections:
        out.append("")
        out.append(f"{title}: {len(items)}")
        out += [f"  {fmt(i)}" for i in items]
    return "\n".join(out)
