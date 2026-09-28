// I-BOM audit scorecard: runs ibom_ingest + ibom_risk in the browser with Pyodide.
// Files are written to Pyodide's in-memory file system and never sent anywhere.

const PYODIDE_VERSION = "314.0.7";
const params = new URLSearchParams(location.search);
// ?pyodide=<base url> lets the page run against a self-hosted copy (offline use, tests)
const PYODIDE_BASE = params.get("pyodide") || `https://cdn.jsdelivr.net/npm/pyodide@${PYODIDE_VERSION}/`;
const WHEELS = ["et_xmlfile-2.0.0-py3-none-any.whl", "openpyxl-3.1.5-py2.py3-none-any.whl", "pypdf-6.19.0-py3-none-any.whl"];

const GLUE = `
import json, sys
if "/ibom" not in sys.path:
    sys.path.insert(0, "/ibom")
from ibom_risk import audit, scorecard

def classify(path):
    kind, detail = audit.classify(path)
    return json.dumps({"kind": kind, "label": audit.LABELS[kind], "detail": detail})

def run(paths, today, at, need_by, project):
    r = audit.audit(json.loads(paths), today or None, at, need_by or None, project or "Uploaded files (audit)")
    return json.dumps({"scorecard": r["scorecard"], "files": r["files"], "log": r["log"],
                       "markdown": scorecard.render(r["scorecard"], markdown=True) + "\\n", "bom": r["doc"]},
                      default=str)
`;

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) n.append(k);
  return n;
};

let py = null;           // Pyodide instance once ready
let files = [];          // {id, name, bytes, kind, label, detail, status, note}
let nextId = 1;
let last = null;         // last result, for downloads
let demoAt = null;       // fixed timestamp so the sample pod always scores the same

const status = (msg, err = false) => { const s = $("#status"); s.textContent = msg; s.classList.toggle("err", err); };

// ---------- Python runtime ----------

async function fetchBytes(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return new Uint8Array(await r.arrayBuffer());
}

async function boot() {
  const { loadPyodide } = await import(PYODIDE_BASE + "pyodide.mjs");
  const p = await loadPyodide({ indexURL: PYODIDE_BASE });
  const [bundle, ...wheels] = await Promise.all([fetchBytes("py/ibom.zip"), ...WHEELS.map((w) => fetchBytes("py/wheels/" + w))]);
  p.FS.mkdirTree("/ibom");
  p.unpackArchive(bundle, "zip", { extractDir: "/ibom" });
  for (const w of wheels) p.unpackArchive(w, "wheel");
  p.runPython(GLUE);
  return p;
}

const ready = boot().then((p) => {
  py = p;
  status(files.length ? "" : "Ready. Add some files, or try the sample pod.");
  refresh();
  classifyAll();
  return p;
}).catch((e) => {
  console.error(e);
  status("Could not load the Python engine. Check your connection and reload the page. (" + e.message + ")", true);
});

function writeFile(f) {
  const dir = `/upload/${f.id}`;
  py.FS.mkdirTree(dir);
  const path = `${dir}/${f.name}`;
  py.FS.writeFile(path, f.bytes);
  return path;
}

function classifyAll() {
  if (!py) return;
  const fn = py.globals.get("classify");
  for (const f of files) {
    if (f.kind) continue;
    try {
      Object.assign(f, JSON.parse(fn(writeFile(f))));
    } catch (e) {
      Object.assign(f, { kind: "unknown", label: "could not read", detail: String(e.message || e) });
    }
  }
  fn.destroy();
  refresh();
}

// ---------- file list ----------

async function addFiles(list) {
  for (const file of list) {
    if (file.name.startsWith(".") || file.name === "desktop.ini") continue;
    files.push({ id: nextId++, name: file.name, bytes: new Uint8Array(await file.arrayBuffer()) });
  }
  classifyAll();
  refresh();
}

function refresh() {
  const ul = $("#files");
  ul.replaceChildren(...files.map((f) => {
    const tagText = f.status || (f.kind === "unknown" ? "skipped" : f.kind ? "ready" : "waiting");
    const note = f.note || (f.kind === "unknown" ? f.detail : null);
    const x = el("button", { class: "x", type: "button", "aria-label": `Remove ${f.name}`, title: "Remove" }, "×");
    x.onclick = () => { files = files.filter((g) => g !== f); resetStatus(); refresh(); };
    return el("li", {},
      el("span", { class: "name", title: f.name }, f.name, el("small", { text: [f.label || "detecting...", note].filter(Boolean).join(": ") })),
      el("span", { class: "tag " + tagText, text: tagText }),
      x);
  }));
  $("#clear").hidden = files.length === 0;
  $("#run").disabled = !py || !files.some((f) => f.kind && f.kind !== "unknown");
}

function resetStatus() { for (const f of files) { delete f.status; delete f.note; } }

const drop = $("#drop");
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#pick").click(); } });
$("#pick").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
$("#pick-dir").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
$("#clear").addEventListener("click", () => { files = []; refresh(); });
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  const items = [...(e.dataTransfer.items || [])].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!items.length) return addFiles([...e.dataTransfer.files]);
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) out.push(await new Promise((res, rej) => entry.file(res, rej)));
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const c of batch) await walk(c);
      } while (batch.length);
    }
  };
  for (const it of items) await walk(it);
  addFiles(out);
});

// ---------- demo ----------

$("#demo").addEventListener("click", async () => {
  const btn = $("#demo");
  btn.disabled = true;
  try {
    const d = await (await fetch("demo/demo.json")).json();
    const loaded = await Promise.all(d.files.map(async (p) => ({ name: p.split("/").pop(), bytes: await fetchBytes("demo/" + p) })));
    files = loaded.map((f) => ({ id: nextId++, ...f }));
    $("#today").value = d.today;
    $("#needby").value = d.needBy || "";
    $("#project").value = d.project || "";
    demoAt = d.at;
    classifyAll();
    refresh();
    await ready;
    run();
  } catch (e) {
    status("Could not load the sample files: " + e.message, true);
  } finally {
    btn.disabled = false;
  }
});
["#today", "#needby", "#project"].forEach((s) => $(s).addEventListener("input", () => { demoAt = null; }));

// ---------- run ----------

$("#run").addEventListener("click", () => run());

async function run() {
  if (!py) return;
  const btn = $("#run");
  btn.disabled = true;
  status("Building the I-BOM and scoring...");
  await new Promise((r) => setTimeout(r, 30)); // let the status paint before Python blocks the thread
  try {
    const paths = files.filter((f) => f.kind && f.kind !== "unknown").map(writeFile);
    const today = $("#today").value;
    const at = demoAt || new Date().toISOString().replace(/\.\d+Z$/, "Z");
    const fn = py.globals.get("run");
    const r = JSON.parse(fn(JSON.stringify(paths), today, at, $("#needby").value, $("#project").value.trim()));
    fn.destroy();
    for (const row of r.files) {
      const f = files.find((g) => row.name === g.name && g.kind === row.kind && !g._seen);
      if (f) { f.status = row.status; f.note = row.note; f._seen = true; }
    }
    files.forEach((f) => delete f._seen);
    last = r;
    refresh();
    render(r);
    const used = r.files.filter((f) => f.status === "used").length;
    const bad = r.files.filter((f) => f.status === "error").length;
    status(`Scored ${used} file${used === 1 ? "" : "s"}` + (bad ? `; ${bad} could not be read (see the file list)` : "") + ".");
    $("#results").hidden = false;
    $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    console.error(e);
    status("Scoring failed: " + String(e.message || e).split("\n").filter(Boolean).pop(), true);
  } finally {
    refresh();
  }
}

// ---------- results ----------

const GRADE_COLOR = { A: "var(--good)", B: "var(--ok)", C: "var(--warn)", D: "var(--poor)", F: "var(--bad)" };
const scoreColor = (s) => GRADE_COLOR[s >= 90 ? "A" : s >= 80 ? "B" : s >= 70 ? "C" : s >= 60 ? "D" : "F"];
const riskColor = (s) => scoreColor(100 - s);
const fmt = (v) => (v === null || v === undefined ? "none" : typeof v === "object" ? JSON.stringify(v) : String(v));
const bar = (pct, color) => el("span", { class: "bar" }, Object.assign(el("i"), { style: `width:${Math.max(2, pct)}%;background:${color}` }));

function render(r) {
  const sc = r.scorecard;
  const o = sc.overall;
  const C = 2 * Math.PI * 52;
  const ring = $("#ring-fg");
  ring.style.strokeDasharray = C;
  ring.style.strokeDashoffset = C * (1 - o.score / 100);
  ring.style.stroke = scoreColor(o.score);
  $("#overall-grade").textContent = o.grade;
  $("#overall-grade").style.color = scoreColor(o.score);
  $("#overall-score").textContent = `${o.score} / 100`;
  $("#project-name").textContent = sc.project || "Uploaded files";
  $("#asof").textContent = `As of ${sc.asOf} · I-BOM version ${sc.bomVersion} · ${r.bom.components.length} lines`;
  const worst = [...sc.areas].sort((a, b) => a.score - b.score)[0];
  const crit = sc.actions.filter((a) => a.priority === "critical").length;
  $("#headline").textContent = `Weakest area: ${worst.area.toLowerCase()} (${worst.score}). `
    + `${sc.actions.length} recommended action${sc.actions.length === 1 ? "" : "s"}${crit ? `, ${crit} critical` : ""}`
    + (sc.shadowMonthlyCost ? `; unmanaged cloud spend about ${sc.shadowMonthlyCost.toLocaleString("en-US", { maximumFractionDigits: 0 })} USD a month.` : ".");

  $("#areas").replaceChildren(...sc.areas.map((a) => el("div", { class: "area" },
    el("div", { class: "area-top" }, el("h4", { text: a.area }), Object.assign(el("span", { class: "g", text: a.grade }), { style: `color:${scoreColor(a.score)}` })),
    el("div", { class: "num" }, String(a.score), el("small", { text: " / 100" })),
    bar(a.score, scoreColor(a.score)),
    el("p", { text: a.summary }))));

  const actions = $("#actions");
  const items = sc.actions.map((a) => el("li", {},
    el("div", { class: "what" }, el("span", { class: "meta" }, el("span", { class: "tag " + a.priority, text: a.priority }), el("span", { class: "tag", text: a.area })), a.what),
    el("div", { class: "do", text: "Do: " + a.do })));
  actions.replaceChildren(...items.slice(0, 8));
  actions.parentElement.querySelector(".more")?.remove();
  if (items.length > 8) {
    const more = el("button", { class: "linkish more", type: "button", text: `Show all ${items.length} actions` });
    more.onclick = () => { actions.replaceChildren(...items); more.remove(); };
    actions.after(more);
  }
  if (!items.length) actions.replaceChildren(el("li", { class: "empty", text: "Nothing to act on." }));

  const drift = $("#drift");
  if (sc.drift.length) {
    drift.replaceChildren(
      el("thead", {}, el("tr", {}, ...["Line", "Attribute", "Approved", "Found", "Severity", "Source"].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...sc.drift.map((d) => el("tr", {},
        el("td", { class: "mono", text: d.component + (d.unit ? " · " + d.unit : "") }),
        el("td", { class: "mono", text: (d.attribute || "").replace(/^\/configuration\//, "") }),
        el("td", { class: "mono", text: fmt(d.expected) }),
        el("td", { class: "mono", text: fmt(d.actual) }),
        el("td", {}, el("span", { class: "tag " + d.severity, text: d.severity })),
        el("td", { text: d.source })))));
  } else {
    drift.replaceChildren(el("caption", { class: "empty", text: "No open drift. Add a golden configuration or Terraform state plus a live export (cloud inventory, Redfish, refresh-only plan) to check for drift." }));
  }

  const sup = $("#supply");
  if (sc.supply.length) {
    sup.replaceChildren(
      el("thead", {}, el("tr", {}, ...["Line", "Risk", "Level", "Chance of missing need-by", "Drivers"].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...sc.supply.map((s) => el("tr", {},
        el("td", { class: "mono", text: s.ref }),
        el("td", {}, el("span", { class: "meter" }, el("span", { class: "num", text: String(s.score) }), bar(s.score, riskColor(s.score)))),
        el("td", {}, el("span", { class: "tag " + s.level, text: s.level })),
        el("td", { class: "num", text: s.delayProbability == null ? "n/a" : `${Math.round(s.delayProbability * 100)}% by ${s.needBy}` }),
        el("td", { text: s.drivers.join(", ") || "none" })))));
  } else {
    sup.replaceChildren(el("caption", { class: "empty", text: "No purchased hardware found. Add quotes, POs, invoices or receiving lists to score supply risk." }));
  }
  $("#log").textContent = r.log;
}

// ---------- downloads ----------

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

document.querySelectorAll("[data-dl]").forEach((b) => b.addEventListener("click", () => {
  if (!last) return;
  const slug = (last.scorecard.project || "audit").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  if (b.dataset.dl === "bom") download(`${slug}.ibom.json`, JSON.stringify(last.bom, null, 2) + "\n", "application/json");
  if (b.dataset.dl === "md") download(`${slug}-scorecard.md`, last.markdown, "text/markdown");
  if (b.dataset.dl === "json") download(`${slug}-scorecard.json`, JSON.stringify(last.scorecard, null, 2) + "\n", "application/json");
}));

$("#today").value = new Date().toISOString().slice(0, 10);
refresh();
