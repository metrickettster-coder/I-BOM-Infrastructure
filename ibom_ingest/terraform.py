"""Terraform / OpenTofu state and plan JSON -> I-BOM records.

Input is the machine-readable form Terraform documents as stable:
  terraform show -json                 (current state)  -> lines in status `deployed`
  terraform show -json plan.tfplan     (a saved plan)   -> creates become `planned`,
                                                           deletes get an eol-notice event
Every managed resource of a known inventory type becomes a line keyed by its
IaC address, so a later state run, a cloud export or a purchase line all land
on the same component. Networking and IAM plumbing (security groups, roles,
routes) is skipped unless include_all=True.
"""
import json
from pathlib import Path

from .cloud import region_location, spec_fields
from .core import slug

# type -> (class, category, provider, instance-type attribute)
TYPES = {
    "aws_instance": ("cloud-resource", "vm", "aws", "instance_type"),
    "aws_spot_instance_request": ("cloud-resource", "vm", "aws", "instance_type"),
    "aws_eks_cluster": ("cloud-resource", "container-cluster", "aws", None),
    "aws_eks_node_group": ("cloud-resource", "node-group", "aws", "instance_types"),
    "aws_s3_bucket": ("cloud-resource", "object-storage", "aws", None),
    "aws_ebs_volume": ("cloud-resource", "block-storage", "aws", None),
    "aws_efs_file_system": ("cloud-resource", "file-storage", "aws", None),
    "aws_fsx_lustre_file_system": ("cloud-resource", "file-storage", "aws", None),
    "aws_db_instance": ("cloud-resource", "database", "aws", "instance_class"),
    "aws_lb": ("cloud-resource", "load-balancer", "aws", None),
    "aws_nat_gateway": ("cloud-resource", "network-gateway", "aws", None),
    "azurerm_linux_virtual_machine": ("cloud-resource", "vm", "azure", "size"),
    "azurerm_windows_virtual_machine": ("cloud-resource", "vm", "azure", "size"),
    "azurerm_kubernetes_cluster": ("cloud-resource", "container-cluster", "azure", None),
    "azurerm_storage_account": ("cloud-resource", "object-storage", "azure", None),
    "google_compute_instance": ("cloud-resource", "vm", "gcp", "machine_type"),
    "google_container_cluster": ("cloud-resource", "container-cluster", "gcp", None),
    "google_storage_bucket": ("cloud-resource", "object-storage", "gcp", None),
    "vsphere_virtual_machine": ("virtual", "vm", "vmware", None),
    "proxmox_vm_qemu": ("virtual", "vm", "proxmox", None),
    "openstack_compute_instance_v2": ("virtual", "vm", "openstack", "flavor_name"),
}
# desired-state attributes copied into `configuration` for later drift checks
CONFIG_KEYS = ["instance_type", "instance_class", "ami", "size", "machine_type", "version", "kubernetes_version",
               "engine", "engine_version", "allocated_storage", "storage_type", "size_gb", "volume_type",
               "num_cpus", "memory", "scaling_config", "encrypted", "storage_capacity"]


def _camel(k):
    head, *rest = k.split("_")
    return head + "".join(w.title() for w in rest)


def iter_state_resources(module):
    for r in module.get("resources", []):
        yield r
    for child in module.get("child_modules", []):
        yield from iter_state_resources(child)


def read(path):
    """Return (kind, [(address, type, values, action)]) for a state or plan JSON file."""
    data = json.loads(Path(path).read_text())
    if "resource_changes" in data:
        out = []
        for rc in data["resource_changes"]:
            if rc.get("mode", "managed") != "managed":
                continue
            ch = rc.get("change", {})
            actions = ch.get("actions", [])
            action = "delete" if actions == ["delete"] else "create" if "create" in actions else \
                "update" if "update" in actions else "no-op"
            values = ch.get("after") if action != "delete" else ch.get("before")
            out.append((rc["address"], rc["type"], values or {}, action))
        return "plan", out
    root = data.get("values", {}).get("root_module", {})
    return "state", [(r["address"], r["type"], r.get("values", {}), "exists")
                     for r in iter_state_resources(root) if r.get("mode", "managed") == "managed"]


def _region(provider, values):
    if values.get("region"):
        return values["region"]
    az = values.get("availability_zone") or values.get("zone") or values.get("location")
    if not az:
        arn = values.get("arn", "")
        parts = arn.split(":")
        return parts[3] if len(parts) > 3 and parts[3] else None
    if provider == "aws" and az[-1].isalpha():
        return az[:-1]
    if provider == "gcp" and az.count("-") == 2:
        return az.rsplit("-", 1)[0]
    return az


def parse(builder, path, source_url=None, observed_at=None, include_all=False, tool="terraform"):
    kind, resources = read(path)
    at = observed_at
    touched, skipped = [], []
    for address, rtype, values, action in resources:
        if action == "no-op":
            continue
        if rtype not in TYPES and not include_all:
            skipped.append(address)
            continue
        cls, category, provider, itype_attr = TYPES.get(rtype, ("cloud-resource", "other", "other", None))
        itype = values.get(itype_attr) if itype_attr else None
        if isinstance(itype, list):
            itype = itype[0] if itype else None
        if provider == "gcp" and itype:
            itype = itype.rsplit("/", 1)[-1]
        region = _region(provider, values)
        rid = values.get("arn") or values.get("self_link") or values.get("id")
        tags = values.get("tags") or values.get("labels") or {}
        name = tags.get("Name") or values.get("name") or values.get("bucket") or address

        virtual, compute, fin = spec_fields(provider, itype)
        v = {"provider": provider, "resourceType": rtype, **virtual,
             "iac": {"tool": tool, "address": address}}
        if source_url:
            v["iac"]["source"] = source_url
        if kind == "state":
            v["iac"]["stateRef"] = Path(path).name
        if rid:
            v["resourceId"] = rid
        if region:
            v["region"] = region
        if tags:
            v["tags"] = {k: str(val) for k, val in tags.items()}
        if values.get("num_cpus"):
            v["vcpus"] = int(values["num_cpus"])
        if values.get("memory") and provider == "vmware":
            v["memoryGiB"] = round(values["memory"] / 1024, 2)
        c = {"class": cls, "category": category, "name": str(name), "quantity": 1, "virtual": v,
             "identifiers": {"iacAddress": address}}
        cfg = {_camel(k): values[k] for k in CONFIG_KEYS if values.get(k) not in (None, "", [], {})}
        if cfg:
            c["configuration"] = cfg
        if rid:
            c["identifiers"]["cloud"] = {"provider": provider, "resourceId": rid}
        if compute:
            c["compute"] = compute
        if fin:
            c["financial"] = fin
        loc = region_location(builder, provider, region) if cls == "cloud-resource" else None
        if loc:
            c["placement"] = {"location": loc}

        keys = [f"iac:{tool}:{address}"]
        if rid:
            keys.append(f"cloud:{rid}")
        if values.get("id") and values.get("id") != rid:
            keys.append(f"cloud:{values['id']}")
        if rtype == "aws_eks_node_group" and values.get("node_group_name"):
            keys.append(f"nodegroup:{values['node_group_name']}")  # its EC2 instances are managed, not shadow
        rec = {"component": c, "keys": keys, "refHint": f"tf-{slug(address, 56)}",
               "source": {"kind": f"{tool}-{kind}", "id": address, "file": Path(path).name}}
        if action == "exists":
            rec.update(status="deployed", at=at, note=f"present in {tool} state")
        elif action == "create":
            rec.update(status="planned", at=at, note=f"{tool} plan creates {address}")
        elif action == "delete":
            if builder.find(keys) is None:
                continue
            rec["events"] = [{"at": at, "type": "eol-notice", "note": f"{tool} plan destroys {address}"}]
            rec["component"] = {"class": cls, "name": str(name)}
        elif action == "update":  # pending, so desired state stays as applied until the next state run
            if builder.find(keys) is None:
                continue
            rec["events"] = [{"at": at, "type": "config-change", "note": f"{tool} plan updates {address} in place"}]
            rec["component"] = {"class": cls, "name": str(name)}
        touched.append(builder.upsert(rec))
    return kind, touched, skipped
