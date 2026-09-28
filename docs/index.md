<div class="hero" markdown="0">
<p class="eyebrow">Open standard · v{{version}} draft · Apache 2.0</p>
<h1>One bill of materials for everything your infrastructure is made of</h1>
<p class="lead">Open I-BOM is a JSON format for hardware, firmware, software, cloud resources, licenses, consumables, labor and soft costs, with the data that keeps each line true after go-live: lifecycle and health, drift baselines, multi-tier supply chain, SBOM links, Scope 3 carbon, and ERP and CMDB keys. It aligns with CycloneDX 1.6, so security tools that read CycloneDX can read an I-BOM export as is.</p>
<p class="cta"><a class="btn primary" href="getting-started.html">Get started</a> <a class="btn" href="schema.html">Schema reference</a> {{scorecard_cta}}</p>
</div>

## Why it exists

Software has SPDX and CycloneDX. Infrastructure has spreadsheets: every vendor and every team formats its lists differently, the list is stale the week after it is signed, supply chain dependencies below the first supplier are invisible, and cloud or edge resources appear without anyone updating the master list. Procurement's ERP and operations' CMDB each hold half the picture and rarely agree.

I-BOM gives all of that one shape, and ships open tools that fill it from the documents teams already have and keep it current.

## What it covers

<div class="grid" markdown="0">
<div class="card"><h3>Problems</h3>
<ul>
<li><strong>No standard format.</strong> One document with a small required core and typed optional blocks. <a href="spec.html#how-the-schema-addresses-the-three-lists">How</a></li>
<li><strong>Rapid AI obsolescence.</strong> Vendor lifecycle stages, last-time-buy dates, successors and qualified alternates on every line.</li>
<li><strong>Supply chain fragility.</strong> Sources, lead times, sub-tier dependencies and a scored risk per line.</li>
<li><strong>Shadow infrastructure.</strong> Unmanaged resources found in live inventories are recorded as findings, then pulled in.</li>
</ul></div>
<div class="card"><h3>Opportunities</h3>
<ul>
<li><strong>Predictive logistics.</strong> Predicted lead time and delay probability against need-by date. <a href="tools/risk.html">Risk engine</a></li>
<li><strong>Automated drift detection.</strong> Approved baselines compared with Redfish, cloud APIs and Terraform refresh plans.</li>
<li><strong>Carbon tracking.</strong> Embodied kgCO2e with GHG Protocol Scope 3 categories, plus energy and facility PUE.</li>
<li><strong>Hardware plus software security.</strong> Firmware and software are lines tied to hardware, with SBOM links and known CVEs.</li>
</ul></div>
<div class="card"><h3>What was missing</h3>
<ul>
<li><strong>An open I-BOM standard.</strong> <a href="schema.html">This schema</a>, with a lossless CycloneDX export.</li>
<li><strong>Dynamic lifecycle tracking.</strong> 18 lifecycle states, health, events and disposal. <a href="tools/ingest.html">Ingestion engine</a></li>
<li><strong>ERP and CMDB interoperability.</strong> Shared keys and a bridge that exports, reconciles and writes back. <a href="tools/bridge.html">Bridge</a></li>
</ul></div>
</div>

## One line, all the context

A single component line can carry its physical, financial, lifecycle, supply, carbon and system identity data. Only `bom-ref`, `class`, `name` and `quantity` are required.

```json
{
  "bom-ref": "optic-800g",
  "class": "hardware",
  "category": "optic",
  "name": "800G OSFP DR8 transceiver",
  "quantity": 16,
  "lifecycle": { "status": "ordered", "vendorLifecycle": { "stage": "active" } },
  "supply": {
    "singleSource": true,
    "sources": [ { "supplier": "org-reseller", "leadTimeDays": { "quoted": 70, "predicted": 112 } } ],
    "risk": { "score": 78, "level": "critical", "recommendedAction": "Qualify a second optic; hold 10% spares." }
  },
  "sustainability": { "embodiedCarbon": { "kgCO2e": 9.6, "boundary": "cradle-to-gate" } },
  "identifiers": { "erp": { "purchaseOrder": "4500018823", "poLine": "40" } }
}
```

Every field above is defined in the [schema reference](schema.html#def-component). The [GPU rack example](examples.html) has a full version of this optic line alongside 24 others, with firmware and relationships.

## The four parts

<div class="grid four" markdown="0">
<a class="card link" href="schema.html"><h3>Schema</h3><p>JSON Schema 2020-12, a validator with rules the schema can't express, and a CycloneDX 1.6 exporter.</p></a>
<a class="card link" href="tools/ingest.html"><h3>Ingestion and lifecycle</h3><p>Quotes, POs, invoices, receipts, Terraform, Ansible, Redfish and cloud inventories in; a current I-BOM out.</p></a>
<a class="card link" href="tools/risk.html"><h3>Drift and supply risk</h3><p>Drift against approved baselines, predicted lead times, sub-tier exposure and a health and risk scorecard.</p></a>
<a class="card link" href="tools/bridge.html"><h3>ERP and CMDB bridge</h3><p>ServiceNow and SAP-, Oracle- or generic-style exports, line-by-line reconciliation and key write-back.</p></a>
</div>

## Status

Version {{version}} is a draft. The format, tools and synthetic samples are complete enough to try end to end; the supply risk model uses illustrative constants rather than trained ones. Open questions for v0.2 are listed in the [specification](spec.html#open-questions-for-v02). Issues and pull requests are welcome on [GitHub]({{repo}}).
