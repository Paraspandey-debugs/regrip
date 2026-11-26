(() => {
  const esc = (s) =>
    String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');

  const badgeClass = (status) => {
    if (status === 'FAIL' || status === 'CONFIRMED_REGRESSION') return 'badge-fail';
    if (status === 'WARN' || status === 'POTENTIALLY_BROKEN') return 'badge-warn';
    return 'badge-pass';
  };

  const renderBadge = (status, prefix) =>
    `<span class="badge ${badgeClass(status)}" aria-label="${esc(prefix)}: ${esc(status)}">${esc(status)}</span>`;

  const formatReason = (reason) => {
    const safe = esc(reason);
    const arrowRe = /([A-Za-z0-9_$.[\]*'"-]+)\s*(?:-&gt;|→)\s*([A-Za-z0-9_$.[\]*'"-]+)/g;
    return safe.replace(arrowRe, (_, oldVal, newVal) =>
      `<span class="diff-pair"><del class="diff-old">${oldVal}</del>`
      + `<span class="diff-arrow" aria-hidden="true">→</span>`
      + `<ins class="diff-new">${newVal}</ins></span>`
    );
  };

  const shortTarget = (uri) => {
    const tokens = String(uri || '').trim().split(/\s+/);
    const last = tokens[tokens.length - 1] || uri;
    const parts = last.split('/');
    return parts[parts.length - 1] || last;
  };

  const rankStatus = (s) => (s === 'CONFIRMED_REGRESSION' ? 2 : s === 'POTENTIALLY_BROKEN' ? 1 : 0);

  let state = { baselines: [], runs: [], activeRunId: null };

  const renderBaselines = () => {
    const el = document.getElementById('baselines-body');
    document.getElementById('baselines-count').textContent = `${state.baselines.length} stored`;
    if (!state.baselines.length) {
      el.innerHTML = '<div class="state-banner">No baselines built yet.</div>';
      return;
    }
    el.innerHTML = state.baselines
      .map(
        (b) => `<div class="baseline-row">`
          + `<span>${esc(b.server_name)}@${esc(b.tag)}</span>`
          + `<span class="panel-meta">${b.cases.length} cases</span></div>`
      )
      .join('');
  };

  const renderRuns = () => {
    const el = document.getElementById('runs-body');
    document.getElementById('runs-count').textContent = `${state.runs.length} runs`;
    if (!state.runs.length) {
      el.innerHTML = '<div class="state-banner">No replay runs yet.</div>';
      return;
    }
    el.innerHTML = [...state.runs]
      .reverse()
      .map((r) => {
        const active = r.run_id === state.activeRunId ? ' is-active' : '';
        const label = `${esc(r.baseline_tag)} → ${esc(shortTarget(r.target_uri))}`;
        return `<div class="run-item${active}">`
          + `<div class="run-top">`
          + `<button type="button" class="run-link" data-run-id="${esc(r.run_id)}">${label}</button>`
          + `<span class="run-counts">P:${r.summary.passed} F:${r.summary.failed}</span>`
          + `</div>`
          + `<details class="cmd-details"><summary>Command</summary>`
          + `<div class="cmd-box"><code>${esc(r.target_uri)}</code>`
          + `<button type="button" class="copy-btn" data-copy="${esc(r.target_uri)}">Copy</button>`
          + `</div></details></div>`;
      })
      .join('');
  };

  const renderHero = (run, report) => {
    const hero = document.getElementById('verdict-hero');
    const stateEl = document.getElementById('verdict-state');
    const sumEl = document.getElementById('verdict-summary');
    const chipsEl = document.getElementById('verdict-chips');

    if (!run || !report) {
      hero.className = 'verdict-hero state-empty';
      stateEl.textContent = 'NO RUNS';
      sumEl.textContent = 'Record MCP calls and run regrip ci to inspect behavioral diffs.';
      chipsEl.innerHTML = '';
      return;
    }

    const v = report.verdict || 'PASS';
    hero.className = `verdict-hero state-${v.toLowerCase()}`;
    stateEl.textContent = v;

    const findings = report.findings || [];
    const toolCount = new Set(findings.map((f) => f.tool_name)).size;
    const affectedAgents = new Set(
      (report.agent_impacts || [])
        .filter((a) => a.impact_status !== 'UNAFFECTED')
        .map((a) => a.agent_id)
    ).size;

    if (v === 'PASS') {
      sumEl.textContent = `All checks passed cleanly across ${run.summary.total} baseline case(s).`;
    } else {
      sumEl.textContent =
        `${findings.length} behavioral change(s) across ${toolCount} tool(s), `
        + `${affectedAgents} consumer(s) affected`;
    }

    const bl = state.baselines.find((b) => b.baseline_id === run.baseline_id || b.tag === run.baseline_tag);
    const blLabel = bl ? `${bl.server_name}@${bl.tag}` : run.baseline_tag;
    const blCases = bl ? `${bl.cases.length} cases` : `${run.summary.total} cases`;
    chipsEl.innerHTML =
      `<div class="stat-chip"><span>Baseline</span><strong>${esc(blLabel)}</strong><span>(${esc(blCases)})</span></div>`
      + `<div class="stat-chip"><span>Run</span><strong>P:${run.summary.passed} F:${run.summary.failed}</strong></div>`;
  };

  const renderDiff = (findings) => {
    const body = document.getElementById('diff-body');
    document.getElementById('diff-count').textContent = `${findings.length} finding(s)`;
    if (!state.runs.length) {
      body.innerHTML =
        `<div class="state-banner"><div class="state-banner-title">Workspace initialized</div>`
        + `<div>Run a baseline and CI replay to populate behavioral diffs:</div>`
        + `<pre>regrip record --server commerce-mcp --stdio -- python mcp_v1.py\n`
        + `regrip baseline build --server commerce-mcp --tag v1.0.0\n`
        + `regrip ci --baseline v1.0.0 --target "python mcp_v2.py"</pre></div>`;
      return;
    }
    if (!findings.length) {
      body.innerHTML =
        `<div class="state-banner"><div class="state-banner-title">No behavioral regressions detected</div>`
        + `<div>All replayed responses matched the baseline schemas, types, ordering, and tolerances.</div></div>`;
      return;
    }

    const byTool = new Map();
    for (const f of findings) {
      if (!byTool.has(f.tool_name)) byTool.set(f.tool_name, []);
      byTool.get(f.tool_name).push(f);
    }

    let tbody = '';
    for (const [tool, items] of byTool.entries()) {
      tbody += `<tr class="tool-group-row"><th colspan="4" scope="colgroup">${esc(tool)}</th></tr>`;
      for (const f of items) {
        tbody +=
          `<tr class="diff-row">`
          + `<td data-label="Severity">${renderBadge(f.severity, 'Severity')}</td>`
          + `<td data-label="Category"><span class="cat-tag">${esc(f.category)}</span></td>`
          + `<td data-label="Path"><code class="path-chip">${esc(f.json_path)}</code></td>`
          + `<td data-label="Reason">${formatReason(f.deterministic_reason)}</td>`
          + `</tr>`;
      }
    }

    body.innerHTML =
      `<table class="diff-table"><thead><tr>`
      + `<th scope="col">Severity</th><th scope="col">Category</th>`
      + `<th scope="col">Path</th><th scope="col">Reason</th>`
      + `</tr></thead><tbody>${tbody}</tbody></table>`;
  };

  const renderImpact = (impacts) => {
    const body = document.getElementById('impact-body');
    const byAgent = new Map();
    for (const entry of impacts) {
      if (!byAgent.has(entry.agent_id)) byAgent.set(entry.agent_id, []);
      byAgent.get(entry.agent_id).push(entry);
    }
    document.getElementById('impact-count').textContent = `${byAgent.size} agent(s)`;
    if (!byAgent.size) {
      body.innerHTML = '<div class="state-banner">No consumer contracts registered.</div>';
      return;
    }

    let html = '';
    for (const [agent, tools] of byAgent.entries()) {
      let worst = 'UNAFFECTED';
      for (const t of tools) {
        if (rankStatus(t.impact_status) > rankStatus(worst)) worst = t.impact_status;
      }
      const toolRows = tools
        .map((t) => {
          const paths = (t.matched_jsonpaths || []).length
            ? t.matched_jsonpaths.map((p) => `<code class="path-chip">${esc(p)}</code>`).join('')
            : '<span class="panel-meta">No impacted paths</span>';
          return `<div class="agent-tool-row">`
            + `<div class="agent-tool-top"><span class="mono">${esc(t.tool_name)}</span>`
            + `${renderBadge(t.impact_status, 'Impact')}</div>`
            + `<div class="agent-paths">${paths}</div></div>`;
        })
        .join('');
      html +=
        `<article class="agent-card">`
        + `<div class="agent-head"><span class="agent-name">${esc(agent)}</span>`
        + `${renderBadge(worst, 'Agent Status')}</div>`
        + `<div class="agent-tools">${toolRows}</div></article>`;
    }
    body.innerHTML = html;
  };

  const selectRun = async (runId) => {
    state.activeRunId = runId;
    renderRuns();
    const run = state.runs.find((r) => r.run_id === runId);
    const report = await fetch(`/api/v1/projects/default/runs/${runId}/report`).then((r) => r.json());
    renderHero(run, report);
    renderDiff(report.findings || []);
    renderImpact(report.agent_impacts || []);
  };

  document.addEventListener('click', (ev) => {
    const runBtn = ev.target.closest('[data-run-id]');
    if (runBtn) {
      selectRun(runBtn.getAttribute('data-run-id'));
      return;
    }
    const copyBtn = ev.target.closest('[data-copy]');
    if (copyBtn) {
      const text = copyBtn.getAttribute('data-copy');
      navigator.clipboard?.writeText(text).catch(() => {});
      copyBtn.textContent = 'Copied';
      setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1200);
    }
  });

  const init = async () => {
    const health = await fetch('/api/v1/health').then((r) => r.json());
    document.querySelector('.health-label').textContent = `${health.status.toUpperCase()} · v${health.version}`;
    state.baselines = await fetch('/api/v1/projects/default/baselines').then((r) => r.json());
    state.runs = await fetch('/api/v1/projects/default/runs').then((r) => r.json());
    renderBaselines();
    renderRuns();
    if (state.runs.length) {
      await selectRun(state.runs[state.runs.length - 1].run_id);
    } else {
      renderHero(null, null);
      renderDiff([]);
      renderImpact([]);
    }
  };

  init();
})();
