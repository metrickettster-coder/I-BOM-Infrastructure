"""Ansible inventory + gathered facts -> I-BOM records.

Inventory (INI or YAML) says which hosts are under configuration management.
Facts (the JSON `ansible -m setup` returns, or a jsonfile fact cache directory)
say what each host really is: vendor, model, serial, BIOS, CPU, memory, OS.

  * A physical host is matched to its purchased line by serial number. A serial
    that isn't in the BOM but whose model matches a purchased line with open
    unit slots is attached there (it was received without a serial on file).
    Anything else is pulled in as `discovered-unmanaged` hardware.
  * A virtual machine becomes a `virtual` line.
  * The operating system becomes one software line per distribution/version,
    with a `runs-on` relationship to each host.
  * BIOS versions go to the lifecycle tracker, which records firmware-update events.
"""
import json
import re
from pathlib import Path

from . import lifecycle
from .core import slug


def read_inventory(path):
    """Return {host: {"groups": [...], "vars": {...}}} from an INI or YAML inventory."""
    p = Path(path)
    text = p.read_text()
    if p.suffix in (".yml", ".yaml") or text.lstrip().startswith(("all:", "---")):
        try:
            import yaml
        except ImportError as e:
            raise SystemExit("YAML inventories need PyYAML: pip install pyyaml") from e
        hosts = {}

        def walk(name, node):
            node = node or {}
            for h, hv in (node.get("hosts") or {}).items():
                e = hosts.setdefault(h, {"groups": [], "vars": {}})
                e["groups"].append(name)
                e["vars"].update(hv or {})
            for child, cnode in (node.get("children") or {}).items():
                walk(child, cnode)

        for name, node in (yaml.safe_load(text) or {}).items():
            walk(name, node)
        return hosts
    hosts, group = {}, "ungrouped"
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith(";") else ""
        if not line:
            continue
        m = re.match(r"\[([^\]:]+)(:\w+)?\]", line)
        if m:
            group = m.group(1) if not m.group(2) else None  # skip :vars / :children sections
            continue
        if group is None:
            continue
        name, *pairs = line.split()
        e = hosts.setdefault(name, {"groups": [], "vars": {}})
        e["groups"].append(group)
        for pair in pairs:
            if "=" in pair:
                k, v = pair.split("=", 1)
                e["vars"][k] = v
    return hosts


def read_facts(path):
    """Return {host: facts} from a directory of fact-cache files or a single `ansible -m setup` JSON."""
    p = Path(path)
    files = sorted(p.iterdir()) if p.is_dir() else [p]
    out = {}
    for f in files:
        if f.is_dir() or f.name.startswith("."):
            continue
        data = json.loads(f.read_text())
        facts = data.get("ansible_facts", data)
        # `--tree` output and the jsonfile cache name each file after the inventory host
        out[f.stem if p.is_dir() else facts.get("ansible_hostname", f.stem)] = facts
    return out


def _os_line(facts):
    dist = facts.get("distribution")
    ver = facts.get("distribution_version")
    if not dist:
        return None
    return dist, ver


def parse(builder, inventory=None, facts_path=None, observed_at=None, inventory_name=None):
    hosts = read_inventory(inventory) if inventory else {}
    facts = read_facts(facts_path) if facts_path else {}
    inv_name = inventory_name or (Path(inventory).name if inventory else "facts")
    at = observed_at
    summary = {"matched": [], "attached": [], "shadow": [], "virtual": [], "os": [], "no_facts": []}
    os_hosts = {}
    for host in sorted(set(hosts) | set(facts)):
        f = facts.get(host)
        if f is None:
            summary["no_facts"].append(host)
            continue
        f = {k[8:] if k.startswith("ansible_") else k: v for k, v in f.items()}
        groups = hosts.get(host, {}).get("groups", [])
        iac = f"ansible:{inv_name}#{host}"
        is_vm = f.get("virtualization_role") == "guest"
        ref = _virtual(builder, host, f, iac, at, summary) if is_vm else _physical(builder, host, f, iac, at, summary, groups)
        osl = _os_line(f)
        if osl:
            os_hosts.setdefault(osl, []).append(ref)
    for (dist, ver), refs in os_hosts.items():
        c = {"class": "software", "category": "os", "name": f"{dist} {ver}", "version": ver,
             "purl": f"pkg:generic/{slug(dist)}@{ver}", "quantity": len(set(refs)),
             "software": {"layer": "os"}}
        keys = [f"sw:os:{slug(dist)}:{ver}"]
        existing = builder.find(keys)
        if existing:  # quantity = number of distinct hosts running it, across runs
            prior = {r["from"] for r in builder.doc.get("relationships", []) if r["type"] == "runs-on"
                     and r["to"] == existing["bom-ref"]}
            c["quantity"] = len(prior | set(refs))
        ref = builder.upsert({"component": c, "keys": keys, "refHint": f"sw-{slug(dist)}-{slug(ver)}",
                              "rank": {"quantity": 1}, "status": "deployed", "at": at,
                              "source": {"kind": "ansible-facts", "id": inv_name}})
        for h in refs:
            builder.relate(ref, "runs-on", h)
        summary["os"].append(ref)
    return summary


def _physical(builder, host, f, iac, at, summary, groups):
    serial = (f.get("product_serial") or "").strip()
    vendor = (f.get("system_vendor") or "").strip()
    model = (f.get("product_name") or "").strip()
    unit = {"serialNumber": serial, "identifiers": {"iacAddress": iac, "other": {"hostname": host}}}
    comp = builder.find([f"serial:{serial}"]) if serial else None
    if comp is None and model:
        comp = _open_slot(builder, model)
        if comp is not None:
            summary["attached"].append(comp["bom-ref"])
    if comp is not None:
        if comp["quantity"] == 1 and not comp.get("units"):
            patch = {"serialNumber": serial} if serial else {}
            patch["identifiers"] = {"iacAddress": iac, "other": {"hostname": host}}
        else:
            patch = {"units": [unit]}
        ref = builder.upsert({"component": patch, "ref": comp["bom-ref"],
                              "keys": [f"serial:{serial}"] if serial else [f"host:{host}"],
                              "source": {"kind": "ansible-facts", "id": host}})
        if ref not in summary["attached"]:
            summary["matched"].append(ref)
    else:
        c = {"class": "hardware", "category": "server", "name": f"{vendor} {model} ({host}, unmanaged)".strip(),
             "model": model or None, "quantity": 1, "serialNumber": serial or None,
             "identifiers": {"iacAddress": iac, "other": {"hostname": host}},
             "compute": _compute(f),
             "notes": "Found by Ansible facts but not in purchasing records. Review and adopt or retire."}
        if vendor:
            c["manufacturer"] = builder.org(vendor, "manufacturer")
        c = {k: v for k, v in c.items() if v not in (None, {}, "")}
        ref = builder.upsert({"component": c, "keys": [f"serial:{serial}" if serial else f"host:{host}"],
                              "refHint": f"shadow-{slug(host)}", "status": "discovered-unmanaged", "at": at,
                              "force": True, "note": "discovered by ansible facts",
                              "source": {"kind": "ansible-facts", "id": host}})
        builder.observe({"id": f"OBS-UNMANAGED-{slug(host, 60)}", "observedAt": at, "source": "ansible-facts",
                         "kind": "unmanaged", "component": ref, "severity": "medium", "status": "open",
                         "discovered": {"class": "hardware", "name": f"{vendor} {model}".strip(),
                                        "serialNumber": serial, "hostname": host}})
        summary["shadow"].append(ref)
    comp = builder.component(ref)
    if comp.get("lifecycle", {}).get("status") != "discovered-unmanaged":
        builder.set_status(comp, "deployed", at, note=f"reachable by Ansible as {host}")
    if not comp.get("compute") and _compute(f):
        comp["compute"] = _compute(f)
    fw = {}
    if f.get("bios_version"):
        fw["bios"] = f["bios_version"]
    if fw:
        lifecycle.observe_firmware(builder, comp, serial or host, fw, at, "ansible-facts")
    return ref


def _virtual(builder, host, f, iac, at, summary):
    uuid = f.get("product_uuid") or f.get("machine_id") or host
    vtype = (f.get("virtualization_type") or "other").lower()
    provider = {"vmware": "vmware", "kvm": "proxmox" if "proxmox" in str(f.get("product_name", "")).lower() else "other",
                "openstack": "openstack", "amazon": "aws", "xen": "aws"}.get(vtype, "other")
    c = {"class": "virtual", "category": "vm", "name": host, "quantity": 1,
         "virtual": {"provider": provider, "resourceType": "vm", "resourceId": str(uuid),
                     "vcpus": int(f.get("processor_vcpus") or 0),
                     "memoryGiB": round((f.get("memtotal_mb") or 0) / 1024, 1),
                     "iac": {"tool": "ansible", "address": iac}},
         "identifiers": {"iacAddress": iac, "other": {"hostname": host}}}
    ref = builder.upsert({"component": c, "keys": [f"cloud:{uuid}", f"iac:ansible:{iac}"],
                          "refHint": f"vm-{slug(host)}", "status": "in-service", "at": at,
                          "source": {"kind": "ansible-facts", "id": host}})
    summary["virtual"].append(ref)
    return ref


def _compute(f):
    procs = [p for p in (f.get("processor") or []) if isinstance(p, str) and not p.isdigit()
             and p not in ("GenuineIntel", "AuthenticAMD")]
    out = {}
    if procs:
        out["cpuModel"] = procs[0]
    if f.get("processor_count"):
        out["cpuSockets"] = int(f["processor_count"])
    if f.get("processor_cores") and f.get("processor_count"):
        out["coresTotal"] = int(f["processor_cores"]) * int(f["processor_count"])
    if f.get("memtotal_mb"):
        out["memoryGiB"] = round(f["memtotal_mb"] / 1024)
    return out


def _open_slot(builder, model):
    """A hardware line for this model that still has unserialized units."""
    m = model.lower()
    for c in builder.doc["components"]:
        if c["class"] != "hardware" or c.get("lifecycle", {}).get("status") == "discovered-unmanaged":
            continue
        names = " ".join(str(c.get(k, "")) for k in ("model", "mpn", "name")).lower()
        if m and m in names:
            used = len(c.get("units", [])) + (1 if c.get("serialNumber") else 0)
            if used < c["quantity"]:
                return c
    return None
