"""Infrastructure health and drift risk scorecard.

One page that answers "how far is this infrastructure from its blueprint, and
what will bite next": four areas scored 0-100 (higher is healthier), an overall
grade, and the handful of actions that move the score most. Built only from
what is already in the I-BOM (drift observations, shadow/missing findings,
supply.risk, the ibom_ingest lifecycle report), so it can run read-only on any
I-BOM someone hands you.
"""
from ibom_ingest import lifecycle

from .drift import remediation

SEV_POINTS = {"critical": 20, "high": 10, "medium": 5, "low": 2, "info": 0}
GRADES = [(90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F")]


def grade(score):
    return next(g for floor, g in GRADES if score >= floor)


def _clamp(x):
    return max(0, min(100, round(x)))


def build(doc, today, horizon_days=365):
    lr = lifecycle.report(doc, today, horizon_days)
    obs = doc.get("observations", [])
    drift = [o for o in obs if o["kind"] == "drift" and o.get("status") in ("open", "acknowledged")]
    shadow = [o for o in obs if o["kind"] == "unmanaged" and o.get("status") in ("open", "acknowledged")]
    missing = [o for o in obs if o["kind"] == "missing" and o.get("status") in ("open", "acknowledged")]

    tracked = [c for c in doc["components"] if c.get("configuration") or c.get("drift", {}).get("attributes")]
    monitored = [c for c in tracked if c.get("drift", {}).get("discoverySources")]
    drift_score = _clamp(100 - sum(SEV_POINTS.get(o.get("severity", "medium"), 5) for o in drift))

    shadow_cost = sum((o.get("discovered", {}).get("estimatedMonthlyCost") or 0) for o in shadow)
    shadow_score = _clamp(100 - 15 * len(shadow) - 10 * len(missing))

    risks = sorted(((c["supply"]["risk"], c) for c in doc["components"] if c.get("supply", {}).get("risk")),
                   key=lambda x: -x[0]["score"])
    top = [r["score"] for r, _ in risks[:3]]
    supply_score = _clamp(100 - (sum(top) / len(top) if top else 0))

    health_pts = sum(8 if h["status"] in ("critical", "offline") else 4 for h in lr["health"])
    eol_pts = sum(10 if e["stage"] not in ("active", "preview", "unknown", None) else 5 for e in lr["eol"])
    lifecycle_score = _clamp(100 - health_pts - eol_pts - 3 * len(lr["expiring"]) - 5 * len(lr["late"]))

    areas = [
        {"area": "Configuration drift", "score": drift_score,
         "summary": f"{len(drift)} open drift findings; {len(monitored)} of {len(tracked)} lines with declared "
                    f"configuration have a live source"},
        {"area": "Shadow infrastructure", "score": shadow_score,
         "summary": f"{len(shadow)} unmanaged items (about {shadow_cost:,.0f} USD/month), {len(missing)} declared "
                    f"items not found"},
        {"area": "Supply chain", "score": supply_score,
         "summary": f"{sum(1 for r, _ in risks if r['level'] in ('high', 'critical'))} of {len(risks)} purchased lines "
                    f"at high or critical risk"},
        {"area": "Lifecycle and health", "score": lifecycle_score,
         "summary": f"{len(lr['health'])} lines with health issues, {len(lr['eol'])} with EOL milestones in "
                    f"{horizon_days} days, {len(lr['late'])} late deliveries"},
    ]
    for a in areas:
        a["grade"] = grade(a["score"])
    overall = _clamp(sum(a["score"] for a in areas) / len(areas))

    actions = []
    for o in sorted(drift, key=lambda o: -SEV_POINTS.get(o.get("severity"), 0)):
        if o.get("severity") in ("critical", "high"):
            unit = o.get("discovered", {}).get("unit")
            actions.append({"priority": o["severity"], "area": "drift",
                            "what": f"{o['component']}{' ' + unit if unit else ''}: {o['attribute']} is "
                                    f"{o.get('actual')!r}, expected {o.get('expected')!r}",
                            "do": remediation(doc, o)})
    for r, c in risks:
        if r["level"] in ("critical", "high") and r.get("recommendedAction"):
            actions.append({"priority": r["level"], "area": "supply",
                            "what": f"{c['bom-ref']}: supply risk {r['score']} ({', '.join(r['drivers'])})"
                                    + (f", {r['delayProbability']:.0%} chance of missing {r['needBy']}"
                                       if r.get("delayProbability") is not None else ""),
                            "do": r["recommendedAction"]})
    for o in shadow:
        name = o.get("discovered", {}).get("name") or o.get("component")
        cost = o.get("discovered", {}).get("estimatedMonthlyCost")
        actions.append({"priority": o.get("severity", "medium"), "area": "shadow",
                        "what": f"{name} is running but in no BOM" + (f" (about {cost:,.0f} USD/month)" if cost else ""),
                        "do": "find the owner, then adopt it into IaC/purchasing or shut it down"})
    for o in missing:
        actions.append({"priority": o.get("severity", "high"), "area": "shadow",
                        "what": f"{o['component']} is declared but was not found by {o['source']}",
                        "do": "confirm whether it was deleted outside IaC; restore or remove it from the code"})
    for h in lr["health"]:
        actions.append({"priority": "high" if h["status"] == "critical" else "medium", "area": "health",
                        "what": f"{h['ref']}: health {h['status']}" + (f" on {', '.join(h['units'])}" if h["units"] else ""),
                        "do": "open a vendor case and check warranty coverage"})
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    actions.sort(key=lambda a: order.get(a["priority"], 5))
    return {"asOf": today, "project": doc["metadata"].get("project", {}).get("name"), "bomVersion": doc.get("version"),
            "overall": {"score": overall, "grade": grade(overall)}, "areas": areas, "actions": actions,
            "supply": [{"ref": c["bom-ref"], "score": r["score"], "level": r["level"], "drivers": r["drivers"],
                        "delayProbability": r.get("delayProbability"), "needBy": r.get("needBy"),
                        "recommendedAction": r.get("recommendedAction")} for r, c in risks],
            "drift": [{"id": o["id"], "component": o["component"], "attribute": o.get("attribute"),
                       "unit": o.get("discovered", {}).get("unit"), "expected": o.get("expected"),
                       "actual": o.get("actual"), "severity": o.get("severity"), "source": o["source"],
                       "observedAt": o["observedAt"]} for o in drift],
            "shadowMonthlyCost": round(shadow_cost, 2)}


def render(sc, markdown=False):
    h = "## " if markdown else ""
    out = [f"{'# ' if markdown else ''}Infrastructure health and drift risk scorecard",
           f"{sc['project']} (I-BOM version {sc['bomVersion']}), as of {sc['asOf']}", "",
           f"Overall: {sc['overall']['score']}/100, grade {sc['overall']['grade']}", ""]
    if markdown:
        out += ["| Area | Score | Grade | Summary |", "|---|---|---|---|"]
        out += [f"| {a['area']} | {a['score']} | {a['grade']} | {a['summary']} |" for a in sc["areas"]]
    else:
        out += [f"  {a['area']:<24} {a['score']:>3}  {a['grade']}   {a['summary']}" for a in sc["areas"]]
    out += ["", f"{h}Top actions ({len(sc['actions'])})"]
    for i, a in enumerate(sc["actions"][:10], 1):
        out.append(f"{i}. [{a['priority']}] {a['what']}. Do: {a['do']}.")
    out += ["", f"{h}Drift findings ({len(sc['drift'])})"]
    for d in sc["drift"]:
        where = "BOM now says" if d["source"] == "manual-audit" else "live"
        out.append(f"{'- ' if markdown else '  '}{d['component']}{' ' + d['unit'] if d['unit'] else ''} "
                   f"{d['attribute']}: approved {d['expected']!r}, {where} {d['actual']!r} "
                   f"({d['severity']}, {d['source']} {d['observedAt']})")
    out += ["", f"{h}Supply risk by line"]
    for s in sc["supply"]:
        extra = f", {s['delayProbability']:.0%} chance of missing {s['needBy']}" if s["delayProbability"] is not None else ""
        out.append(f"{'- ' if markdown else '  '}{s['ref']}: {s['score']} {s['level']}"
                   f" [{', '.join(s['drivers']) or 'no drivers'}]{extra}")
    return "\n".join(out)
