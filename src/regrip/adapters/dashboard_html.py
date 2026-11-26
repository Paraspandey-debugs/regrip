"""Self-contained HTML Web Dashboard for `regrip serve` (FR-OBS-002)."""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Regrip — MCP Behavioral Regression Dashboard</title>
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --text:#e2e8f0; --muted:#94a3b8;
            --ok:#22c55e; --warn:#f59e0b; --fail:#ef4444; --accent:#38bdf8; }
    body { margin:0; font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); }
    header { padding:16px 24px; border-bottom:1px solid #334155; display:flex;
             justify-content:space-between; align-items:center; }
    main { padding:24px; max-width:1100px; margin:0 auto; display:grid; gap:20px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }
    .card { background:var(--card); border-radius:10px; padding:16px; border:1px solid #334155; }
    h1 { font-size:1.25rem; margin:0; color:var(--accent); }
    h2 { font-size:1rem; margin:0 0 12px; color:#f8fafc; }
    table { width:100%; border-collapse:collapse; font-size:0.88rem; }
    th, td { text-align:left; padding:8px; border-bottom:1px solid #334155; }
    .badge { padding:2px 8px; border-radius:999px; font-weight:600; font-size:0.75rem; }
    .PASS,.UNAFFECTED { background:#14532d; color:#86efac; }
    .WARN,.POTENTIALLY_BROKEN { background:#78350f; color:#fcd34d; }
    .FAIL,.CONFIRMED_REGRESSION { background:#7f1d1d; color:#fca5a5; }
  </style>
</head>
<body>
  <header>
    <h1>Regrip Local Observability</h1>
    <span id="health" style="color:var(--muted)">Loading...</span>
  </header>
  <main>
    <div class="grid">
      <div class="card"><h2>Baselines</h2><div id="baselines">Loading...</div></div>
      <div class="card"><h2>Replay Runs</h2><div id="runs">Loading...</div></div>
    </div>
    <div class="card">
      <h2>Latest Run Diff &amp; Consumer Impact</h2>
      <div id="report">Select or run a replay to inspect diffs.</div>
    </div>
  </main>
  <script>
    async function load() {
      const h = await fetch('/api/v1/health').then(r => r.json());
      document.getElementById('health').textContent = `${h.status.toUpperCase()} · v${h.version}`;
      const bl = await fetch('/api/v1/projects/default/baselines').then(r => r.json());
      document.getElementById('baselines').innerHTML = bl.length
        ? bl.map(
            b => `<div><b>${b.server_name}@${b.tag}</b> (${b.cases.length} cases)</div>`
          ).join('')
        : '<span style="color:var(--muted)">No baselines yet</span>';
      const runs = await fetch('/api/v1/projects/default/runs').then(r => r.json());
      document.getElementById('runs').innerHTML = runs.length
        ? runs.map(
            r => `<div><a href="#" style="color:var(--accent)" `
              + `onclick="showRun('${r.run_id}');return false;">`
              + `${r.baseline_tag} → ${r.target_uri}</a> `
              + `(P:${r.summary.passed} F:${r.summary.failed})</div>`
          ).join('')
        : '<span style="color:var(--muted)">No replay runs yet</span>';
      if (runs.length) await showRun(runs[runs.length - 1].run_id);
    }
    async function showRun(id) {
      const d = await fetch(`/api/v1/projects/default/runs/${id}/report`).then(r => r.json());
      const rows = (d.findings || []).map(
        f => `<tr><td>${f.tool_name}</td><td><span class="badge ${f.severity}">`
          + `${f.severity}</span></td><td>${f.category}</td>`
          + `<td><code>${f.json_path}</code></td><td>${f.deterministic_reason}</td></tr>`
      ).join('');
      const impacts = (d.agent_impacts || []).map(
        a => `<tr><td>${a.agent_id}</td><td>${a.tool_name}</td>`
          + `<td><span class="badge ${a.impact_status}">${a.impact_status}</span></td>`
          + `<td>${a.matched_jsonpaths.join(', ') || '—'}</td></tr>`
      ).join('');
      document.getElementById('report').innerHTML =
        `<p>Verdict: <span class="badge ${d.verdict}">${d.verdict}</span></p>`
        + `<table><thead><tr><th>Tool</th><th>Severity</th><th>Category</th>`
        + `<th>Path</th><th>Reason</th></tr></thead><tbody>`
        + `${rows || '<tr><td colspan="5">No findings</td></tr>'}</tbody></table>`
        + `<h2 style="margin-top:16px">Consumer Impact Matrix</h2>`
        + `<table><thead><tr><th>Agent</th><th>Tool</th><th>Impact</th>`
        + `<th>Paths</th></tr></thead><tbody>`
        + `${impacts || '<tr><td colspan="4">No consumer contracts</td></tr>'}</tbody></table>`;
    }
    load();
  </script>
</body>
</html>
"""
