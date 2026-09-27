"""Predictive supply chain risk for purchased lines.

For every hardware and consumable line (and anything carrying sub-tier data) this
module works out, from what the I-BOM already holds plus three optional inputs:

  sub-tier catalog   hidden tier-2/3 dependencies (GPU die, BMC SoC, switch ASIC, laser die)
  alternates catalog form-fit-function alternates, plus the vendor's successor from lifecycle data
  market signals     lead-time multipliers and risk drivers by category, part, country or supplier

and writes back, in schema fields:

  supply.subTierDependencies    enriched from the catalog
  supply.sources[].leadTimeDays observed (ordered -> received), predicted, predictedAt
  supply.singleSource           no second supplier and no qualified alternate
  supply.risk                   score 0-100, level, drivers, delayProbability, needBy, recommendedAction
  variants.alternates           suggested alternates (qualified: false until engineering signs off)
  organizations[].risk          supplier on-time delivery rate and score, from this BOM's own history

The prediction is deliberately explainable rather than a black box: quoted lead
time x the supplier's observed slip ratio x market multipliers gives a predicted
lead time; the gap between the predicted arrival and the need-by date, through a
logistic curve, gives the probability of missing it. Every number that feeds the
score is in the report, so a buyer can see why a line is red and replace any
input with better data later.
"""
import copy
import json
import math
import re
import statistics
from datetime import date, timedelta
from pathlib import Path

CATALOG_DIR = Path(__file__).resolve().parent / "catalogs"
OPEN = ("planned", "quoted", "ordered", "backordered", "in-transit")
ASSESS_CLASSES = ("hardware", "consumable")
SKIP_STATUS = ("discovered-unmanaged", "decommissioned", "disposed")
LEVELS = [(75, "critical"), (50, "high"), (25, "medium"), (0, "low")]
FIN_POINTS = {"watch": 15, "distressed": 35}
SATURATION = 60  # score = 100 * (1 - exp(-points / SATURATION)): drivers add up, the score never pins at 100


def load(path=None, name=None):
    if path is None and name is None:
        return {}
    return json.loads(Path(path or CATALOG_DIR / name).read_text())


def _d(s):
    return date.fromisoformat(s[:10])


def _text(builder, c):
    mfr = builder.component(c["manufacturer"])["name"] if c.get("manufacturer") in builder.by_ref else ""
    return " ".join(str(x) for x in (mfr, c.get("mpn", ""), c.get("model", ""), c.get("name", "")) if x)


def _suppliers(c):
    return [s["supplier"] for s in c.get("supply", {}).get("sources", [])]


# ---------- supplier history ----------

def supplier_history(builder, today):
    """Per supplier: on-time delivery rate and median lead-time slip (observed / quoted) from this BOM."""
    hist = {}
    for c in builder.doc["components"]:
        d = c.get("lifecycle", {}).get("dates", {})
        st = c.get("lifecycle", {}).get("status")
        for s in c.get("supply", {}).get("sources", []):
            h = hist.setdefault(s["supplier"], {"delivered": 0, "onTime": 0, "openLate": 0, "ratios": []})
            quoted = s.get("leadTimeDays", {}).get("quoted")
            if d.get("received") and d.get("promised"):
                h["delivered"] += 1
                h["onTime"] += d["received"] <= d["promised"]
                if d.get("ordered") and quoted:
                    h["ratios"].append((_d(d["received"]) - _d(d["ordered"])).days / quoted)
            elif st in OPEN and d.get("promised") and d["promised"] < today:
                h["openLate"] += 1
    out = {}
    for sup, h in hist.items():
        n = h["delivered"] + h["openLate"]
        if not n:
            continue
        out[sup] = {"onTimeDeliveryRate": round(h["onTime"] / n, 2), "lines": n,
                    "slip": round(min(3.0, max(1.0, statistics.median(h["ratios"]))), 2) if h["ratios"] else 1.0}
    return out


def _signal_hits(builder, c, signals, countries):
    text = _text(builder, c)
    sup_names = [builder.component(s)["name"] for s in _suppliers(c) if s in builder.by_ref]
    hits = []
    for sig in signals.get("signals", []):
        m = sig.get("match", {})
        if "category" in m and c.get("category") != m["category"]:
            continue
        if "mpn" in m and not re.search(m["mpn"], text, re.I):
            continue
        if "country" in m and m["country"] not in countries:
            continue
        if "supplier" in m and not any(re.search(m["supplier"], n, re.I) for n in sup_names):
            continue
        hits.append(sig)
    return hits


def rate_suppliers(builder, hist, signals, at):
    for o in builder.doc.get("organizations", []):
        if o["bom-ref"] not in hist and not any(re.search(s.get("match", {}).get("supplier", "^$"), o["name"], re.I)
                                                for s in signals.get("signals", [])):
            continue
        h = hist.get(o["bom-ref"])
        risk = {k: v for k, v in o.get("risk", {}).items() if k in ("geopoliticalExposure",)}
        fin = next((s["financialHealth"] for s in signals.get("signals", [])
                    if s.get("financialHealth") and re.search(s["match"].get("supplier", "^$"), o["name"], re.I)), None)
        score = 0
        notes = []
        if h:
            risk["onTimeDeliveryRate"] = h["onTimeDeliveryRate"]
            score += round((1 - h["onTimeDeliveryRate"]) * 50)
            notes.append(f"{round(h['onTimeDeliveryRate'] * h['lines'])} of {h['lines']} lines on time in this BOM, "
                         f"median lead-time slip x{h['slip']}")
        if fin:
            risk["financialHealth"] = fin
            score += FIN_POINTS.get(fin, 0)
        risk.update(score=min(100, score), assessedAt=at)
        if notes:
            risk["notes"] = "; ".join(notes)
        o["risk"] = risk


# ---------- enrichment ----------

def enrich(builder, c, subtier, alternates):
    text = _text(builder, c)
    part = {}
    deps = []
    for e in subtier.get("entries", []):
        if re.search(e["match"], text, re.I):
            for dep in e["dependencies"]:
                dep = copy.deepcopy(dep)
                name = dep.pop("supplierName", None)
                if name:
                    dep["supplier"] = builder.org(name, "manufacturer")
                deps.append(dep)
    existing = c.get("supply", {}).get("subTierDependencies", [])
    deps = [d for d in deps if not any(x.get("name") == d["name"] for x in existing)]
    if deps:
        part["supply"] = {"subTierDependencies": deps}
    alts = []
    for e in alternates.get("entries", []):
        if re.search(e["match"], text, re.I):
            for a in e["alternates"]:
                a = copy.deepcopy(a)
                a.pop("leadTimeDays", None)
                name = a.pop("manufacturerName", None)
                if name:
                    a["manufacturer"] = builder.org(name, "manufacturer")
                a.setdefault("qualified", False)
                alts.append(a)
    succ = c.get("lifecycle", {}).get("vendorLifecycle", {}).get("successor")
    if succ:
        alt = {"mpn": succ, "qualified": False, "condition": "vendor-named successor",
               "impact": "Successor platform: requalify firmware, optics/cabling and rack power before use."}
        if c.get("manufacturer"):
            alt["manufacturer"] = c["manufacturer"]
        alts.append(alt)
    have = {a["mpn"] for a in c.get("variants", {}).get("alternates", [])}
    alts = [a for a in alts if a["mpn"] not in have]
    if alts:
        part["variants"] = {"alternates": alts}
    if part:
        builder.upsert({"component": part, "ref": c["bom-ref"],
                        "source": {"kind": "supply-catalog", "id": "ibom-risk catalogs"}})


def _alt_lead(alternates, builder, c):
    text = _text(builder, c)
    leads = [a["leadTimeDays"] for e in alternates.get("entries", []) if re.search(e["match"], text, re.I)
             for a in e["alternates"] if a.get("leadTimeDays")]
    return min(leads) if leads else None


# ---------- scoring ----------

def _countries(builder, c):
    out = set()
    sup = c.get("supply", {})
    for s in sup.get("sources", []):
        if s.get("countryOfOrigin"):
            out.add(s["countryOfOrigin"])
    for d in sup.get("subTierDependencies", []):
        if d.get("countryOfOrigin"):
            out.add(d["countryOfOrigin"])
    m = builder.component(c.get("manufacturer")) if c.get("manufacturer") else None
    if m and m.get("country"):
        out.add(m["country"])
    return out


def assess_line(builder, c, today, need_by, signals, hist, alternates, at):
    sup = c.setdefault("supply", {})
    lc = c.get("lifecycle", {})
    st = lc.get("status")
    dates = lc.get("dates", {})
    sources = sup.get("sources", [])
    points, drivers, why, actions = 0, [], [], []

    def add(driver, pts, reason):
        nonlocal points
        points += pts
        if driver not in drivers:
            drivers.append(driver)
        why.append(f"{reason} (+{pts})")

    # lead time: quoted x supplier slip x market
    src = next((s for s in sources if s.get("preferred")), sources[0] if sources else None)
    quoted = (src or {}).get("leadTimeDays", {}).get("quoted") or None
    h = hist.get((src or {}).get("supplier"), {})
    slip = h.get("slip", 1.0)
    countries = _countries(builder, c)
    hits = _signal_hits(builder, c, signals, countries)
    factor = math.prod(s.get("leadTimeFactor", 1.0) for s in hits)
    predicted = round(quoted * slip * factor) if quoted else None
    if src is not None:
        lt = src.setdefault("leadTimeDays", {})
        if dates.get("ordered") and dates.get("received"):
            lt["observed"] = (_d(dates["received"]) - _d(dates["ordered"])).days
        if predicted is not None:
            lt["predicted"], lt["predictedAt"] = predicted, at
    eff = predicted or quoted

    # single source: one supplier and no qualified alternate
    qualified = [a for a in c.get("variants", {}).get("alternates", []) if a.get("qualified")]
    single = len(set(_suppliers(c))) <= 1 and not qualified
    sup["singleSource"] = single
    if single and c["class"] != "consumable":  # commodity consumables can be bought anywhere
        add("single-source", 10, "one supplier and no qualified alternate")
    sub_pts = 0
    for d in sup.get("subTierDependencies", []):
        if d.get("singleSource") and sub_pts < 12:
            pts = min(5, 12 - sub_pts)
            sub_pts += pts
            add("single-source", pts, f"single-source sub-tier: {d['name']}"
                + (f" ({d['constraint']})" if d.get("constraint") else ""))
    if eff and eff >= 90:
        add("long-lead-time", 15, f"predicted lead time {eff} days")
    elif eff and eff >= 45:
        add("long-lead-time", 8, f"predicted lead time {eff} days")
    if quoted and predicted and predicted >= quoted * 1.25:
        add("lead-time-increasing", 8, f"quoted {quoted} days, predicted {predicted}")

    # late or backordered
    is_open = st in OPEN
    days_late = (_d(today) - _d(dates["promised"])).days if is_open and dates.get("promised") else 0
    if days_late > 0:
        add("logistics", 15 + min(15, days_late // 7), f"{st}, promised {dates['promised']}, {days_late} days late")
    if h and h.get("onTimeDeliveryRate", 1) < 0.8:
        add("logistics", 8, f"supplier on-time rate {h['onTimeDeliveryRate']:.0%}")

    # vendor lifecycle
    vl = lc.get("vendorLifecycle", {})
    horizon = (_d(today) + timedelta(days=365)).isoformat()
    soon = (_d(today) + timedelta(days=180)).isoformat()
    if vl.get("stage") not in (None, "active", "preview", "unknown"):
        add("eol-approaching", 25, f"vendor lifecycle stage {vl['stage']}")
    else:
        m = min((vl[k], k) for k in ("lastTimeBuy", "endOfSale") if vl.get(k)) if any(
            vl.get(k) for k in ("lastTimeBuy", "endOfSale")) else None
        if m and m[0] <= horizon:
            add("eol-approaching", 18 if m[0] <= soon else 10, f"{m[1]} {m[0]}")
            actions.append(f"{'Last-time-buy' if m[1] == 'lastTimeBuy' else 'End of sale'} {m[0]}: size a final buy "
                           f"for spares and expansion" + (f", or qualify {vl['successor']}" if vl.get("successor") else ""))

    # market signals and export control
    for s in hits:
        for drv in s.get("drivers", []):
            add(drv, s.get("points", 8), s.get("note", drv))
    if sup.get("exportControl", {}).get("restricted"):
        add("export-control", 10, "export-controlled item")

    risk = {"drivers": drivers, "assessedAt": at}
    detail = {"ref": c["bom-ref"], "name": c["name"], "status": st, "drivers": drivers, "why": why,
              "quotedLeadDays": quoted, "supplierSlip": slip, "marketFactor": round(factor, 2),
              "predictedLeadDays": predicted}

    # delay probability for lines still to arrive
    nb = sup.get("risk", {}).get("needBy") or need_by or dates.get("promised")
    if is_open and nb:
        ordered = _d(dates.get("ordered") or today)
        arrival = ordered + timedelta(days=eff or 30)
        if arrival < _d(today):  # already overdue by the model: assume recovery takes about half a lead time
            arrival = _d(today) + timedelta(days=max(7, round((eff or 14) * 0.5)))
        slack = (_d(nb) - arrival).days
        sigma = max(5.0, 0.3 * (eff or 14))
        p = 1 / (1 + math.exp(slack / sigma))
        if h:
            p += (1 - h["onTimeDeliveryRate"]) * 0.25 * (1 - p)
        risk["delayProbability"] = round(min(0.99, p), 2)
        risk["needBy"] = nb
        detail.update(expectedArrival=arrival.isoformat(), needBy=nb, slackDays=slack,
                      delayProbability=risk["delayProbability"])
        sup_name = builder.component(src["supplier"])["name"] if src and src["supplier"] in builder.by_ref else "the supplier"
        if slack < 0:
            actions.insert(0, f"Expected {arrival.isoformat()}, {-slack} days after need-by {nb}: expedite with {sup_name}")
        elif risk["delayProbability"] >= 0.3:
            actions.insert(0, f"Expected {arrival.isoformat()}, only {slack} days before need-by {nb}: confirm ship date with {sup_name}")
        alt_lead = _alt_lead(alternates, builder, c)
        if alt_lead and slack < 0 and _d(today) + timedelta(days=alt_lead) <= _d(nb):
            alt = next((a for a in c.get("variants", {}).get("alternates", []) if not a.get("condition")), None)
            if alt:
                actions.insert(1, f"or buy alternate {alt['mpn']} (about {alt_lead} days, arrives before need-by; "
                                  f"qualify first)")
    if risk.get("delayProbability"):
        points += round(30 * risk["delayProbability"])
        why.append(f"{risk['delayProbability']:.0%} chance of missing need-by (+{round(30 * risk['delayProbability'])})")
    mult = {"critical": 1.2, "high": 1.1}.get(c.get("criticality"), 1.0)
    score = round(100 * (1 - math.exp(-points * mult / SATURATION)))
    level = next(lv for floor, lv in LEVELS if score >= floor)
    risk = {"score": score, "level": level, **risk}
    detail.update(score=score, level=level, points=round(points * mult, 1))
    if single and not qualified and not actions and score >= 25 and c["class"] != "consumable":
        alt = next((a for a in c.get("variants", {}).get("alternates", [])), None)
        actions.append(f"Qualify alternate {alt['mpn']}" if alt else "Find and qualify a second source")
    ss = [d["name"] for d in sup.get("subTierDependencies", []) if d.get("singleSource")]
    if ss and score >= 25:
        actions.append(f"Single-source sub-tier ({', '.join(ss)}): keep critical spares on hand")
    if actions:
        risk["recommendedAction"] = "; ".join(actions[:3])
    sup["risk"] = risk
    detail["recommendedAction"] = risk.get("recommendedAction")
    return detail


def assess(builder, today, at, need_by=None, signals=None, subtier=None, alternates=None):
    """Score every purchased line. Returns report rows sorted by score, highest first."""
    signals = signals or {}
    subtier = subtier if subtier is not None else load(name="subtier.json")
    alternates = alternates if alternates is not None else load(name="alternates.json")
    hist = supplier_history(builder, today)
    rate_suppliers(builder, hist, signals, at)
    rows = []
    for c in builder.doc["components"]:
        st = c.get("lifecycle", {}).get("status")
        if st in SKIP_STATUS:
            continue
        if c["class"] not in ASSESS_CLASSES and not c.get("supply", {}).get("subTierDependencies"):
            continue
        enrich(builder, c, subtier, alternates)
        rows.append(assess_line(builder, c, today, need_by, signals, hist, alternates, at))
    rows.sort(key=lambda r: -r["score"])
    return rows


def supplier_rows(doc):
    return [{"ref": o["bom-ref"], "name": o["name"], **o["risk"]} for o in doc.get("organizations", []) if o.get("risk")]

