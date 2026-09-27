"""I-BOM interoperability bridge for ERP and CMDB systems.

Exports an I-BOM to the formats procurement and IT operations already use
(ServiceNow CMDB payloads and CSVs; SAP-, Oracle- or generic-style ERP CSVs
for PO lines, fixed assets, depreciation and recurring cloud spend), reads
their exports back, and reconciles all three views line by line so
procurement and engineering work from one synchronized picture.
"""
__version__ = "0.1.0"
TOOL = {"name": "ibom-bridge", "version": __version__}
