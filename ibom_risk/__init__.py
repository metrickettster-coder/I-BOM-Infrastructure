"""I-BOM drift detection, supply chain risk prediction and the health scorecard.

Builds on ibom_ingest: loads an I-BOM, compares its declared configuration and
approved baselines with what live sources report (drift), scores every
purchased line for supply risk (single source, lead time, late shipments,
EOL, market signals, sub-tier bottlenecks) and rolls both up with the
lifecycle report into one scorecard.
"""
__version__ = "0.1.0"
TOOL = {"name": "ibom-risk", "version": __version__}
