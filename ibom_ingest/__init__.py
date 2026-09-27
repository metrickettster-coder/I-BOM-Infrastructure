"""I-BOM ingestion engine and lifecycle tracker.

Turns purchasing records (quotes, POs, invoices, receiving lists), declarative
state (Terraform state/plan, Ansible inventory and facts) and live discovery
(cloud inventories, Redfish) into one I-BOM document, and keeps each line's
lifecycle current as new snapshots arrive.
"""
__version__ = "0.1.0"
TOOL = {"name": "ibom-ingest", "version": __version__}
