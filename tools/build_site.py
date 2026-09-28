#!/usr/bin/env python3
"""Build the I-BOM documentation website (GitHub Pages) into _site/.

    pip install markdown
    python3 tools/build_site.py [-o _site] [--repo-url URL]

Sources:
  docs/*.md                      hand-written pages (landing, getting started, examples, tools)
  README.md                      rendered as the specification page
  ibom_*/README.md               rendered as one page per engine under tools/
  ibom.schema.json               rendered as the schema reference
  site/                          static files copied as-is (assets/, ...)
  scorecard/                     the scorecard web page, published at /scorecard/

The schema, examples, samples, tools and Python packages are also copied to the
same relative paths, so pages (including /scorecard/) can fetch them.
"""
import argparse
import html
import json
import os
import posixpath
import re
import shutil
import sys
from pathlib import Path

try:
    import markdown
except ImportError:  # pragma: no cover
    sys.exit("build_site.py needs the 'markdown' package: pip install markdown")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "https://github.com/metrickettster-coder/I-BOM-Infrastructure"
# scorecard/ is the in-browser scorecard page (it may also live under site/scorecard/).
COPY_DIRS = ["scorecard", "examples", "samples", "tools", "ibom_ingest", "ibom_risk", "ibom_bridge"]
COPY_FILES = ["ibom.schema.json", "LICENSE"]

# (output path, source, title, nav label or None)
ENGINE_PAGES = [
    ("tools/ingest.html", "ibom_ingest/README.md", "Ingestion and lifecycle", "Ingestion"),
    ("tools/risk.html", "ibom_risk/README.md", "Drift and supply chain risk", "Drift and risk"),
    ("tools/bridge.html", "ibom_bridge/README.md", "ERP and CMDB bridge", "ERP / CMDB bridge"),
]
# Links in any markdown source that point at these repo files go to the site page instead.
MD_TO_PAGE = {
    "README.md": "spec.html",
    "ibom_ingest/README.md": "tools/ingest.html",
    "ibom_risk/README.md": "tools/risk.html",
    "ibom_bridge/README.md": "tools/bridge.html",
    "docs/getting-started.md": "getting-started.html",
    "docs/examples.md": "examples.html",
    "docs/tools.md": "tools/index.html",
    "docs/index.md": "index.html",
    "samples/pod-b02/README.md": "samples/pod-b02/index.html",
    "samples/pod-b02-ops/README.md": "samples/pod-b02-ops/index.html",
    "examples/pod-b02-scorecard.md": "examples/pod-b02-scorecard.html",
    "examples/pod-b02-bridge/reconciliation.md": "examples/pod-b02-bridge/reconciliation.html",
}
# Extra markdown files rendered as pages (not in the nav).
EXTRA_PAGES = ["samples/pod-b02/README.md", "samples/pod-b02-ops/README.md",
               "examples/pod-b02-scorecard.md", "examples/pod-b02-bridge/reconciliation.md"]


def spec_version():
    schema = json.loads((ROOT / "ibom.schema.json").read_text())
    m = re.search(r"/(\d+\.\d+)/", schema.get("$id", ""))
    return m.group(1) if m else "0.1"


# ---------------------------------------------------------------- page shell

def nav_items(has_scorecard):
    items = [
        ("index.html", "Home"),
        ("getting-started.html", "Get started"),
        ("spec.html", "Specification"),
        ("schema.html", "Schema reference"),
        ("examples.html", "Examples"),
        ("tools/index.html", "Tools"),
    ]
    if has_scorecard:
        items.append(("scorecard/index.html", "Scorecard"))
    return items


def rel(from_page, to_path):
    """Relative URL from one output page to another output path."""
    base = posixpath.dirname(from_page)
    r = posixpath.relpath(to_path, base or ".")
    if r.endswith("index.html"):
        r = r[: -len("index.html")] or "./"
    return r


def page(out_path, title, body, ctx, toc=""):
    nav = []
    for target, label in nav_items(ctx["has_scorecard"]):
        current = out_path == target or (
            target == "tools/index.html" and out_path.startswith("tools/"))
        nav.append('<a href="%s"%s>%s</a>' % (
            rel(out_path, target), ' aria-current="page"' if current else "", label))
    side = ""
    if toc:
        side = '<aside class="toc" aria-label="On this page"><div class="toc-title">On this page</div>%s</aside>' % toc
    full_title = "Open I-BOM" if out_path == "index.html" else "%s · Open I-BOM" % title
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="Open I-BOM: an open, CycloneDX-aligned Infrastructure Bill of Materials format for hardware, software, cloud, labor, lifecycle, drift, supply chain, carbon and ERP/CMDB data.">
<link rel="stylesheet" href="{css}">
</head>
<body>
<header class="top">
  <div class="top-inner">
    <a class="brand" href="{home}"><span class="logo" aria-hidden="true">I</span>Open I-BOM <span class="ver">v{ver} draft</span></a>
    <button class="nav-toggle" aria-expanded="false" aria-controls="nav" onclick="var n=document.getElementById('nav');var o=n.classList.toggle('open');this.setAttribute('aria-expanded',o)">Menu</button>
    <nav id="nav">{nav}<a href="{repo}" class="gh">GitHub</a></nav>
  </div>
</header>
<div class="layout{wide}">
<main>
{body}
</main>
{side}
</div>
<footer class="foot"><div>Open I-BOM {ver} (draft). Apache License 2.0. Sample data on this site is synthetic. <a href="{repo}">Source on GitHub</a> · <a href="{repo}/issues">Feedback</a></div></footer>
</body>
</html>
""".format(title=html.escape(full_title), css=rel(out_path, "assets/style.css"),
           home=rel(out_path, "index.html"), ver=ctx["version"], nav="".join(nav),
           repo=ctx["repo"], body=body, side=side, wide=" has-toc" if toc else "")


# ---------------------------------------------------------------- markdown

def render_md(text, src_path, out_path, ctx):
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "md_in_html", "attr_list"],
                           extension_configs={"toc": {"permalink": "#", "toc_depth": "2-3"}})
    body = md.convert(text)
    body = rewrite_links(body, src_path, out_path, ctx)
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    toc = md.toc if md.toc.count("<li") >= 3 else ""
    title_m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    title = re.sub(r"<[^>]+>|#$", "", title_m.group(1)).strip() if title_m else "Open I-BOM"
    return body, toc, html.unescape(title)


def rewrite_links(body, src_path, out_path, ctx):
    src_dir = posixpath.dirname(src_path)

    def fix(m):
        attr, url = m.group(1), m.group(2)
        if re.match(r"^[a-z]+:|^#|^//", url):
            return m.group(0)
        path, _, frag = url.partition("#")
        if src_path.startswith("docs/"):
            # docs/*.md are written with site-relative links already
            return m.group(0)
        target = posixpath.normpath(posixpath.join(src_dir, path))
        if target in MD_TO_PAGE:
            new = rel(out_path, MD_TO_PAGE[target])
        else:
            kind = "tree" if (ROOT / target).is_dir() else "blob"
            new = "%s/%s/main/%s" % (ctx["repo"], kind, target)
        return '%s="%s%s"' % (attr, new, "#" + frag if frag else "")

    return re.sub(r'(href|src)="([^"]+)"', fix, body)


# ---------------------------------------------------------------- schema reference

def esc(s):
    return html.escape(str(s))


def ref_name(ref):
    return ref.split("/")[-1]


def type_html(node, defs):
    """Short human description of a schema node's type."""
    if not isinstance(node, dict):
        return "any"
    if "$ref" in node:
        name = ref_name(node["$ref"])
        d = defs.get(name, {})
        if d.get("type") in ("string", "integer", "number") and "properties" not in d:
            return '<a href="#def-%s"><code>%s</code></a> <span class="muted">(%s)</span>' % (name, name, d["type"])
        return '<a href="#def-%s"><code>%s</code></a>' % (name, name)
    if "const" in node:
        return "<code>%s</code>" % esc(json.dumps(node["const"]))
    if "enum" in node:
        return "enum"
    t = node.get("type")
    if t == "array":
        return "array of " + type_html(node.get("items", {}), defs)
    if isinstance(t, list):
        return " | ".join(t)
    if t == "object" and "properties" in node:
        return "object"
    if t == "object" and isinstance(node.get("additionalProperties"), dict):
        return "map of " + type_html(node["additionalProperties"], defs)
    for key in ("oneOf", "anyOf"):
        if key in node:
            return " | ".join(type_html(x, defs) for x in node[key])
    return esc(t or "any")


def constraints_html(node):
    parts = []
    if not isinstance(node, dict):
        return ""
    enum = node.get("enum")
    if enum is None and node.get("type") == "array" and isinstance(node.get("items"), dict):
        enum = node["items"].get("enum")
    if enum:
        parts.append('<span class="enum">%s</span>' % " ".join("<code>%s</code>" % esc(v) for v in enum))
    for key, label in (("format", "format"), ("pattern", "pattern"), ("minimum", "min"),
                       ("maximum", "max"), ("exclusiveMinimum", "&gt;"), ("minItems", "min items"),
                       ("maxItems", "max items"), ("minLength", "min length"), ("default", "default")):
        if key in node:
            parts.append('<span class="c">%s <code>%s</code></span>' % (label, esc(node[key] if isinstance(node[key], str) else json.dumps(node[key]))))
    if node.get("uniqueItems"):
        parts.append('<span class="c">unique items</span>')
    return " ".join(parts)


def property_rows(props, required, defs, prefix="", depth=0):
    rows = []
    for name, node in props.items():
        node = node if isinstance(node, dict) else {}
        path = prefix + name
        req = '<span class="req" title="required">required</span>' if name in required else ""
        desc = esc(node.get("description", ""))
        cons = constraints_html(node)
        rows.append(
            '<tr class="d%d"><td class="prop"><code>%s</code>%s</td><td>%s</td><td>%s%s</td></tr>' % (
                min(depth, 3), esc(path), req, type_html(node, defs), desc,
                ('<div class="cons">%s</div>' % cons) if cons else ""))
        inner = node
        if node.get("type") == "array" and isinstance(node.get("items"), dict) and "properties" in node["items"]:
            inner = node["items"]
            path += "[]"
        if "properties" in inner and depth < 4:
            rows.extend(property_rows(inner["properties"], set(inner.get("required", [])), defs,
                                      path + ".", depth + 1))
    return rows


def find_refs(node, out):
    if isinstance(node, dict):
        if "$ref" in node:
            out.add(ref_name(node["$ref"]))
        for v in node.values():
            find_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            find_refs(v, out)


def object_block(anchor, title, node, defs, used_by=None):
    out = ['<section class="def" id="%s">' % anchor,
           '<h3><a class="anchor" href="#%s">#</a><code>%s</code></h3>' % (anchor, esc(title))]
    if node.get("description"):
        out.append("<p>%s</p>" % esc(node["description"]))
    meta = []
    if node.get("type"):
        meta.append("type <code>%s</code>" % esc(node["type"]))
    if node.get("additionalProperties") is False:
        meta.append("closed (unknown keys are rejected; use <code>extensions</code>)")
    if used_by:
        meta.append("used by " + ", ".join('<a href="#%s"><code>%s</code></a>' % (
            "root" if u == "(root)" else "def-" + u, u) for u in sorted(used_by)))
    if meta:
        out.append('<p class="meta">%s</p>' % " · ".join(meta))
    if "properties" in node:
        rows = property_rows(node["properties"], set(node.get("required", [])), defs)
        out.append('<div class="table-wrap"><table class="props"><thead><tr><th>Property</th><th>Type</th>'
                   '<th>Description</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows))
    else:
        cons = constraints_html(node)
        if cons:
            out.append('<p class="cons">%s</p>' % cons)
        if "propertyNames" in node:
            out.append('<p class="cons">keys: %s</p>' % constraints_html(node["propertyNames"]))
        if node.get("type") == "array" and "items" in node:
            out.append("<p>Items: %s</p>" % type_html(node["items"], defs))
            if "properties" in node["items"]:
                rows = property_rows(node["items"]["properties"], set(node["items"].get("required", [])), defs)
                out.append('<div class="table-wrap"><table class="props"><thead><tr><th>Property</th><th>Type</th>'
                           '<th>Description</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows))
    if "allOf" in node:
        out.append('<details><summary>Conditional rules (allOf)</summary><pre><code>%s</code></pre></details>'
                   % esc(json.dumps(node["allOf"], indent=2)))
    out.append("</section>")
    return "\n".join(out)


GROUPS = [
    ("Document", ["metadata", "person", "approval", "bomView", "ilmPhase", "effectivity"]),
    ("Parties and places", ["organization", "location"]),
    ("Components", ["component", "unit", "placement", "physical", "compute", "network", "software",
                    "virtual", "licensing", "labor", "quality"]),
    ("Lifecycle", ["lifecycle", "lifecycleStatus", "health"]),
    ("Supply chain", ["supply", "alternatePart"]),
    ("Security, carbon, money, identifiers", ["security", "sustainability", "financial", "systemIdentifiers"]),
    ("Drift and change", ["driftControl", "baseline", "observation", "changeRecord"]),
    ("Graph", ["relationship", "dependency"]),
    ("Common types", ["bomRef", "dateTime", "date", "uri", "currencyCode", "countryCode", "money", "hash",
                      "externalReference", "properties", "extensions"]),
]


def schema_page(ctx):
    schema = json.loads((ROOT / "ibom.schema.json").read_text())
    defs = schema.get("$defs", {})
    used_by = {name: set() for name in defs}
    for name, node in list(defs.items()) + [("(root)", schema.get("properties", {}))]:
        refs = set()
        find_refs(node, refs)
        for r in refs:
            if r in used_by and r != name:
                used_by[r].add(name)

    grouped = [n for _, names in GROUPS for n in names]
    groups = GROUPS + ([("Other", [n for n in defs if n not in grouped])] if any(n not in grouped for n in defs) else [])

    toc = ['<ul><li><a href="#root">Document root</a></li>']
    body = ["<h1>Schema reference</h1>",
            '<p class="lead">Generated from <a href="ibom.schema.json"><code>ibom.schema.json</code></a> '
            '(JSON Schema draft 2020-12). Every table below is the schema itself, so it cannot drift from it.</p>',
            '<div class="callout"><div><strong>Schema URL for tools:</strong> <code>%s</code></div>'
            '<div>Download: <a href="ibom.schema.json">ibom.schema.json</a> · pinned copy: '
            '<a href="schema/%s/ibom.schema.json">schema/%s/ibom.schema.json</a></div></div>'
            % (esc(schema.get("$id", "")), ctx["version"], ctx["version"]),
            '<p>Required properties are marked <span class="req">required</span>. Nested objects are shown '
            'as dotted paths (<code>health.metrics</code>); <code>[]</code> marks an array of objects. '
            'Most objects are closed: unknown keys fail validation, and vendor data goes in '
            '<a href="#def-extensions"><code>extensions</code></a> or <a href="#def-properties"><code>properties</code></a>.</p>',
            '<div class="filter"><label for="q">Filter properties</label>'
            '<input id="q" type="search" placeholder="e.g. leadTime, kgCO2e, sysId" autocomplete="off"></div>',
            '<h2 id="root">Document root</h2>',
            object_block("root-obj", "I-BOM document", schema, defs)]
    for title, names in groups:
        gid = "g-" + re.sub(r"[^a-z]+", "-", title.lower()).strip("-")
        toc.append('<li><a href="#%s">%s</a><ul>%s</ul></li>' % (
            gid, esc(title), "".join('<li><a href="#def-%s">%s</a></li>' % (n, n) for n in names if n in defs)))
        body.append('<h2 id="%s">%s</h2>' % (gid, esc(title)))
        for n in names:
            if n in defs:
                body.append(object_block("def-" + n, n, defs[n], defs, used_by.get(n)))
    toc.append("</ul>")
    body.append(FILTER_JS)
    return "\n".join(body), "".join(toc)


FILTER_JS = """<script>
(function(){
  var q=document.getElementById('q'); if(!q) return;
  q.addEventListener('input', function(){
    var t=q.value.trim().toLowerCase();
    document.querySelectorAll('section.def').forEach(function(s){
      var any=false;
      s.querySelectorAll('tbody tr').forEach(function(r){
        var hit=!t || r.textContent.toLowerCase().indexOf(t)>=0;
        r.style.display=hit?'':'none'; any=any||hit;
      });
      var title=s.querySelector('h3').textContent.toLowerCase();
      s.style.display=(!t || any || title.indexOf(t)>=0)?'':'none';
    });
  });
})();
</script>"""


# ---------------------------------------------------------------- build

SCORECARD_CTA = '<a class="btn" href="scorecard/">Try the scorecard</a>'


def build(out, repo):
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # static files (assets, scorecard/ from its own thread, ...)
    static = ROOT / "site"
    if static.is_dir():
        shutil.copytree(static, out, dirs_exist_ok=True)
    for d in COPY_DIRS:
        if (ROOT / d).is_dir():
            shutil.copytree(ROOT / d, out / d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in COPY_FILES:
        shutil.copy2(ROOT / f, out / f)
    ver = spec_version()
    (out / "schema" / ver).mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "ibom.schema.json", out / "schema" / ver / "ibom.schema.json")
    (out / ".nojekyll").write_text("")

    ctx = {"repo": repo.rstrip("/"), "version": ver,
           "has_scorecard": (out / "scorecard" / "index.html").is_file()}
    written = []

    def emit(path, title, body, toc=""):
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page(path, title, body, ctx, toc))
        written.append(path)

    for src, dest in [("docs/index.md", "index.html"), ("docs/getting-started.md", "getting-started.html"),
                      ("docs/examples.md", "examples.html"), ("docs/tools.md", "tools/index.html"),
                      ("README.md", "spec.html")] + [(s, d) for d, s, _, _ in ENGINE_PAGES] + \
                     [(s, MD_TO_PAGE[s]) for s in EXTRA_PAGES]:
        text = (ROOT / src).read_text()
        text = text.replace("{{version}}", ver).replace("{{repo}}", ctx["repo"])
        text = text.replace("{{scorecard_cta}}", SCORECARD_CTA if ctx["has_scorecard"] else "")
        body, toc, title = render_md(text, src, dest, ctx)
        if src == "docs/index.md":
            toc = ""
        emit(dest, title, body, toc)

    body, toc = schema_page(ctx)
    emit("schema.html", "Schema reference", body, toc)
    emit("404.html", "Not found", '<h1>Page not found</h1><p>Try the <a href="%s">home page</a> or the '
         '<a href="%sschema.html">schema reference</a>.</p>' % ("/" + repo.rstrip("/").split("/")[-1] + "/",
                                                                 "/" + repo.rstrip("/").split("/")[-1] + "/"))

    check_links(out)
    return written, ctx


def check_links(out):
    """Fail the build on a broken relative link or anchor inside the generated site."""
    problems = []
    ids = {}
    for f in out.rglob("*.html"):
        text = f.read_text(errors="replace")
        ids[f] = set(re.findall(r'id="([^"]+)"', text))
    for f in out.rglob("*.html"):
        if f.name == "404.html":
            continue
        text = f.read_text(errors="replace")
        for url in re.findall(r'(?:href|src)="([^"]+)"', text):
            if re.match(r"^[a-z]+:|^//|^data:", url) or url.startswith("mailto:"):
                continue
            path, _, frag = url.partition("#")
            target = f if not path else (f.parent / path).resolve()
            if path and target.is_dir():
                target = target / "index.html"
            if not target.exists():
                problems.append("%s -> %s (missing)" % (f.relative_to(out), url))
            elif frag and target.suffix == ".html" and target in ids and frag not in ids[target]:
                problems.append("%s -> %s (no such anchor)" % (f.relative_to(out), url))
    if problems:
        sys.exit("broken links:\n  " + "\n  ".join(sorted(set(problems))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=str(ROOT / "_site"))
    default_repo = DEFAULT_REPO
    if os.environ.get("GITHUB_REPOSITORY"):
        default_repo = "%s/%s" % (os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                                  os.environ["GITHUB_REPOSITORY"])
    ap.add_argument("--repo-url", default=default_repo)
    args = ap.parse_args()
    written, ctx = build(args.out, args.repo_url)
    print("built %d pages into %s%s" % (len(written), args.out,
                                        " (with /scorecard/)" if ctx["has_scorecard"] else ""))


if __name__ == "__main__":
    main()
