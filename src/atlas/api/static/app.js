/* Atlas UI — read-only views over the JSON API. No build step, no framework:
   the point is a run's diagnosis on screen in one file of plain JS. */

const $ = (sel) => document.querySelector(sel);

const state = { runs: [], detail: null, graph: null };

document.addEventListener("DOMContentLoaded", () => {
  $("#refresh").addEventListener("click", loadRuns);
  $("#back-link").addEventListener("click", (e) => {
    e.preventDefault();
    showList();
  });
  window.addEventListener("hashchange", routeFromHash);
  routeFromHash();
});

function routeFromHash() {
  const runId = decodeURIComponent(location.hash.slice(1));
  if (runId) {
    showDetail(runId);
  } else {
    showList();
  }
}

async function loadRuns() {
  const res = await fetch("/api/runs");
  const body = await res.json();
  state.runs = body.runs || [];
  renderRunTable();
}

function showList() {
  history.replaceState(null, "", location.pathname);
  $("#run-detail-view").hidden = true;
  $("#run-list-view").hidden = false;
  loadRuns();
}

async function showDetail(runId) {
  $("#run-list-view").hidden = true;
  $("#run-detail-view").hidden = false;
  const [detailRes, graphRes] = await Promise.all([
    fetch(`/api/runs/${encodeURIComponent(runId)}`),
    fetch(`/api/runs/${encodeURIComponent(runId)}/graph`),
  ]);
  if (detailRes.status === 404) {
    await loadRuns();
    showList();
    return;
  }
  state.detail = await detailRes.json();
  state.graph = await graphRes.json();
  renderDetail();
}

/* ── run list ──────────────────────────────────────────────────────── */

function renderRunTable() {
  const rows = $("#run-rows");
  rows.innerHTML = "";
  $("#post-hint").hidden = state.runs.length > 0;
  for (const s of state.runs) {
    const tr = document.createElement("tr");
    tr.className = `status-${s.status}`;
    tr.innerHTML = `
      <td><a href="#${encodeURIComponent(s.run_id)}">${esc(s.run_id)}</a></td>
      <td><span class="badge ${s.status}">${s.status}</span></td>
      <td>${s.node_count}</td>
      <td>${s.failure_count}</td>
      <td>${fmtMs(s.duration_ms)}</td>
      <td>${s.retry_wasted_ms ? fmtMs(s.retry_wasted_ms) : "—"}</td>
      <td class="q">${esc(s.input_query || "")}</td>`;
    rows.appendChild(tr);
  }
}

/* ── run detail ────────────────────────────────────────────────────── */

function renderDetail() {
  const d = state.detail;
  const s = d.summary;
  $("#run-title").textContent = `Run ${s.run_id}`;
  $("#run-facts").innerHTML = [
    `<span class="badge ${s.status}">${s.status}</span>`,
    `${s.node_count} spans · ${s.agent_count} agents`,
    `duration ${fmtMs(s.duration_ms)}`,
    `${s.failure_count} failure(s)`,
    s.retry_wasted_ms ? `${fmtMs(s.retry_wasted_ms)} lost to retries` : null,
    s.input_query ? `“${esc(s.input_query)}”` : null,
  ].filter(Boolean).join(" · ");

  renderRootCauses(d.root_causes.candidates);
  renderFailures(d.failures);
  renderRetries(d.retry_waste);
  renderGraph();
}

function renderRootCauses(candidates) {
  const el = $("#root-causes");
  el.innerHTML = candidates.length ? "" : "<p class='hint'>none detected</p>";
  for (const c of candidates) {
    const f = c.failure;
    const div = document.createElement("div");
    div.className = "card candidate" + (c.is_root ? "" : " demoted");
    div.innerHTML = `
      <div class="card-title">
        #${c.rank} <span class="kind">${esc(f.kind)}</span> at
        <code>${esc(f.node_id)}</code>
        ${c.agent ? `<span class="agent">(${esc(c.agent)})</span>` : ""}
        ${c.is_root ? "" : "<span class='demoted-tag'>downstream</span>"}
      </div>
      ${f.message ? `<div class="msg">${esc(f.message)}</div>` : ""}
      <div class="path">propagation: ${c.propagation_path.map((id) => `<code>${esc(id)}</code>`).join(" → ")}</div>
      <ul class="reasons">${c.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;
    el.appendChild(div);
  }
}

function renderFailures(report) {
  const el = $("#failures");
  el.innerHTML = report.failures.length ? "" : "<p class='hint'>none detected</p>";
  report.failures.forEach((f, i) => {
    const radius = report.radii[i];
    const div = document.createElement("div");
    div.className = "card";
    const affected = radius.affected
      .map((a) => {
        const via = a.via.length ? ` <span class="via">via ${a.via.join("+")}</span>` : "";
        const agent = a.agent ? ` <span class="agent">(${esc(a.agent)})</span>` : "";
        return `<li class="sev-${a.severity}">${esc(a.node_id)}${agent}${via}</li>`;
      })
      .join("");
    div.innerHTML = `
      <div class="card-title"><span class="kind">${esc(f.kind)}</span> at
        <code>${esc(f.node_id)}</code></div>
      ${f.message ? `<div class="msg">${esc(f.message)}</div>` : ""}
      <ul class="evidence">${f.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
      <div class="blast-title">blast radius</div>
      <ul class="blast">${affected}</ul>`;
    el.appendChild(div);
  });
}

function renderRetries(waste) {
  const el = $("#retries");
  if (!waste.groups.length) {
    el.innerHTML = "<p class='hint'>no retries detected</p>";
    return;
  }
  el.innerHTML = waste.groups
    .map(
      (g) => `<div class="card">
        <div class="card-title">${esc(g.operation)}
          ${g.agent ? `<span class="agent">(${esc(g.agent)})</span>` : ""}</div>
        ${fmtMs(g.wasted_ms)} wasted on ${g.superseded_ids.length} superseded attempt(s)
        ${g.wasted_cost_usd != null ? `· $${g.wasted_cost_usd.toFixed(4)} carried cost` : ""}
        <div class="path">${g.superseded_ids.map((id) => `<code>${esc(id)}</code>`).join(", ")}</div>
      </div>`
    )
    .join("");
}

/* ── graph rendering ────────────────────────────────────────────────── */

function renderGraph() {
  const g = state.graph;
  const el = $("#graph");
  el.innerHTML = "";
  const affected = new Map(); // node_id -> severity
  for (const radius of (state.detail.failures.radii || [])) {
    for (const a of radius.affected) {
      affected.set(a.node_id, a.severity);
    }
  }
  const lines = layoutLines(g);
  for (const line of lines) {
    for (const node of line) {
      el.appendChild(nodeChip(node, affected.get(node.id)));
    }
    el.appendChild(document.createElement("br"));
  }
  $("#graph-coverage").textContent = g.unjoined_handoffs.length
    ? `unjoined handoffs (coverage gaps): ${g.unjoined_handoffs.join(", ")}`
    : "";
}

/* Depth bands: roots on the first line, one line per call-tree depth. This is
   a schematic, not a force layout -- the goal is that the branch structure is
   legible, not that it looks like a physics simulation. */
function layoutLines(g) {
  const nodes = new Map(g.nodes.map((n) => [n.id, n]));
  const depth = new Map();
  function depthOf(id) {
    if (depth.has(id)) return depth.get(id);
    const parent = nodes.get(id) && g.edges.find((e) => e.type === "call" && e.target === id);
    const d = parent ? depthOf(parent.source) + 1 : 0;
    depth.set(id, d);
    return d;
  }
  const byDepth = new Map();
  for (const n of g.nodes) {
    const d = depthOf(n.id);
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d).push(n);
  }
  return [...byDepth.keys()].sort((a, b) => a - b).map((d) => {
    return byDepth.get(d).sort((a, b) => (a.started_at || "").localeCompare(b.started_at || ""));
  });
}

function nodeChip(node, severity) {
  const span = document.createElement("span");
  span.className = [
    "node",
    `kind-${node.kind.toLowerCase()}`,
    severity ? `sev-${severity}` : "",
    node.status === "ERROR" ? "err" : "",
  ].join(" ");
  const label = node.agent
    ? `${node.agent}`
    : node.name;
  span.title = [
    node.name,
    `kind=${node.kind} status=${node.status}`,
    node.agent ? `agent=${node.agent} (${node.agent_source})` : null,
    node.tool ? `tool=${node.tool}` : null,
    node.attempt > 1 ? `attempt ${node.attempt}` : null,
    node.duration_ms != null ? `${(node.duration_ms / 1000).toFixed(1)}s` : null,
  ].filter(Boolean).join("\n");
  span.textContent = label;
  return span;
}

/* ── helpers ────────────────────────────────────────────────────────── */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function fmtMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}
