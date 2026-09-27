"""Negative and positive checks for the I-BOM schema and validator. Run: python3 tests/test_ibom.py"""
import copy, json, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EX = json.loads((ROOT / "examples/ai-gpu-rack.ibom.json").read_text())
comp = lambda d, ref: next(c for c in d["components"] if c["bom-ref"] == ref)


def run(doc):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(doc, f)
    r = subprocess.run([sys.executable, str(ROOT / "tools/ibom_validate.py"), f.name, "--quiet"], capture_output=True, text=True)
    return r.returncode, r.stdout


def case(name, mutate, expect_fail, needle=""):
    d = copy.deepcopy(EX)
    mutate(d)
    code, out = run(d)
    ok = (code != 0) == expect_fail and needle in out
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "\n" + out))
    return ok


cases = [
    case("example passes", lambda d: None, False, "PASS"),
    case("duplicate bom-ref", lambda d: d["components"].append(copy.deepcopy(comp(d, "rack-a01"))), True, "duplicate bom-ref"),
    case("unresolved supplier", lambda d: comp(d, "cdu-a01")["supply"]["sources"][0].update(supplier="org-nope"), True, "unresolved bom-ref 'org-nope'"),
    case("unresolved relationship", lambda d: d["relationships"].append({"from": "srv-gpu-01", "type": "powered-by", "to": "ghost"}), True, "ghost"),
    case("extended cost mismatch", lambda d: comp(d, "optic-800g")["financial"]["extendedCost"].update(amount=1), True, "extendedCost"),
    case("rack U collision", lambda d: comp(d, "srv-gpu-02")["placement"].update(rackUnit=14), True, "collides"),
    case("labor without labor block", lambda d: comp(d, "lab-netcfg").pop("labor"), True, "SCHEMA"),
    case("unknown class", lambda d: comp(d, "rack-a01").update({"class": "widget"}), True, "SCHEMA"),
    case("bad currency", lambda d: comp(d, "rack-a01")["financial"]["unitCost"].update(currency="usd"), True, "SCHEMA"),
    case("unknown field rejected", lambda d: comp(d, "rack-a01").update(colour="red"), True, "SCHEMA"),
    case("observation unknown baseline", lambda d: d["observations"][0].update(baseline="BL-X"), True, "unknown baseline"),
    case("power budget warning", lambda d: d["locations"][3]["rack"].update(powerBudgetKw=10), False, "exceeds budget 10"),
]
sys.exit(0 if all(cases) else 1)
