# Tools

Everything is plain Python 3 with `jsonschema` as the only required dependency. Each engine reads and writes standard I-BOM fields, so you can use one without the others, or swap in your own.

| Tool | Command | Page |
|---|---|---|
| Validator | `python3 tools/ibom_validate.py bom.json [--quiet]` | [below](#validator) |
| CycloneDX exporter | `python3 tools/ibom_to_cyclonedx.py bom.json -o bom.cdx.json` | [below](#cyclonedx-exporter) |
| Ingestion and lifecycle | `python3 -m ibom_ingest <command>` | [Ingestion and lifecycle](ingest.html) |
| Drift, supply risk and scorecard | `python3 -m ibom_risk <command>` | [Drift and risk](risk.html) |
| ERP and CMDB bridge | `python3 -m ibom_bridge <command>` | [ERP / CMDB bridge](bridge.html) |
| This website | `python3 tools/build_site.py` | [below](#this-website) |

## Validator

```bash
python3 tools/ibom_validate.py examples/ai-gpu-rack.ibom.json
python3 tools/ibom_validate.py my.ibom.json --quiet            # exit code only: 0 valid, 1 invalid
python3 tools/ibom_validate.py my.ibom.json --schema path/to/ibom.schema.json
```

It runs the JSON Schema check, then the rules a schema can't express (unique and resolvable `bom-ref`s, cost arithmetic, serials against quantity, rack U collisions, rack power and project budget warnings, observation references), then prints a rollup of cost by class, embodied carbon, high supply risks, end-of-life flags, health warnings, open drift and shadow items. The rules are listed in the [specification](../spec.html#validator-rules-beyond-the-schema).

## CycloneDX exporter

```bash
python3 tools/ibom_to_cyclonedx.py examples/ai-gpu-rack.ibom.json -o ai-gpu-rack.cdx.json
```

Writes CycloneDX 1.6 JSON that passes the official strict schema. The mapping, and how I-BOM-only data rides along in `ibom:data` properties so a later importer can round-trip, is in the [specification](../spec.html#cyclonedx-export).

## Engines

<div class="grid" markdown="0">
<a class="card link" href="ingest.html"><h3>Ingestion and lifecycle</h3><p><code>init</code>, <code>purchasing</code>, <code>terraform</code>, <code>ansible</code>, <code>redfish</code>, <code>discover</code>, <code>eol</code>, <code>report</code>, <code>build</code></p></a>
<a class="card link" href="risk.html"><h3>Drift and supply risk</h3><p><code>desired</code>, <code>baseline</code>, <code>drift</code>, <code>supply</code>, <code>scorecard</code>, <code>build</code></p></a>
<a class="card link" href="bridge.html"><h3>ERP and CMDB bridge</h3><p><code>cmdb-export</code>, <code>erp-export</code>, <code>reconcile</code>, <code>cmdb-pull</code>, <code>cmdb-push</code>, <code>run</code></p></a>
</div>

Every engine command that changes an I-BOM bumps its `version`, validates it against the schema before writing, and has a `build` or `run` form that replays a whole manifest, so a pipeline is one JSON file you can keep in version control.

## This website

The site is generated from the repository, so the schema reference always matches `ibom.schema.json`:

```bash
pip install markdown
python3 tools/build_site.py            # writes _site/, fails on any broken internal link
python3 -m http.server -d _site 8000   # preview at http://localhost:8000
```

Pages come from `docs/*.md`, the repository and engine READMEs, and the schema. Static files, including the scorecard page, live under `site/`. The GitHub Actions workflow `.github/workflows/pages.yml` builds and publishes it on every push to `main`.
