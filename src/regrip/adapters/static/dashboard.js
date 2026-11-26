(() => {
  const esc = (s) => window.RegripViews.esc(s);

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
        (b, idx) => `<div class="baseline-row">`
          + `<button type="button" class="baseline-btn" data-baseline-idx="${idx}">`
          + `${esc(b.server_name)}@${esc(b.tag)}</button>`
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
        const hasExcerpt = f.baseline_excerpt !== null || f.candidate_excerpt !== null;
        const excerptHtml = hasExcerpt
          ? `<details class="excerpt-details"><summary>JSON Excerpt</summary>`
            + `<pre class="excerpt-pre">- ${esc(JSON.stringify(f.baseline_excerpt))}\n`
            + `+ ${esc(JSON.stringify(f.candidate_excerpt))}</pre></details>`
          : '';
        tbody +=
          `<tr class="diff-row">`
          + `<td data-label="Severity">${renderBadge(f.severity, 'Severity')}</td>`
          + `<td data-label="Category"><span class="cat-tag">${esc(f.category)}</span></td>`
          + `<td data-label="Path"><code class="path-chip">${esc(f.json_path)}</code></td>`
          + `<td data-label="Reason">${formatReason(f.deterministic_reason)}${excerptHtml}</td>`
          + `</tr>`;
      }
    }

    body.innerHTML =
      `<table class="diff-table"><thead><tr>`
      + `<th scope="col">Severity</th><th scope="col">Category</th>`
      + `<th scope="col">Path</th><th scope="col">Reason</th>`
      + `</tr></thead><tbody>${tbody}</tbody></table>`;
  };

  const loadToolTimeline = async (toolName) => {
    const select = document.getElementById('timeline-tool-select');
    if (!toolName) {
      window.RegripViews?.renderToolTimeline(null);
      return;
    }
    if (select && select.value !== toolName) select.value = toolName;
    const hist = await fetch(`/api/v1/projects/default/tools/${encodeURIComponent(toolName)}/history`).then(
      (r) => r.json()
    );
    window.RegripViews?.renderToolTimeline(hist);
  };

  const syncTimelineTools = (findings, impacts) => {
    const select = document.getElementById('timeline-tool-select');
    if (!select) return;
    const tools = [
      ...new Set([
        ...(findings || []).map((f) => f.tool_name),
        ...(impacts || []).map((i) => i.tool_name),
        ...state.baselines.flatMap((b) => (b.cases || []).map((c) => c.tool_name)),
      ]),
    ];
    select.innerHTML = tools.length
      ? tools.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join('')
      : '<option value="">No tools</option>';
    loadToolTimeline(tools[0] || '');
  };

  const selectRun = async (runId) => {
    state.activeRunId = runId;
    renderRuns();
    const run = state.runs.find((r) => r.run_id === runId);
    const report = await fetch(`/api/v1/projects/default/runs/${runId}/report`).then((r) => r.json());
    const findings = report.findings || [];
    const impacts = report.agent_impacts || [];
    renderHero(run, report);
    renderDiff(findings);
    window.RegripViews?.renderImpact(impacts, renderBadge);
    window.RegripViews?.renderDependencyGraph(impacts, findings);
    await syncTimelineTools(findings, impacts);
  };

  document.addEventListener('click', (ev) => {
    const runBtn = ev.target.closest('[data-run-id]');
    if (runBtn) {
      selectRun(runBtn.getAttribute('data-run-id'));
      return;
    }
    const blBtn = ev.target.closest('[data-baseline-idx]');
    if (blBtn) {
      const idx = Number(blBtn.getAttribute('data-baseline-idx'));
      window.RegripViews?.renderBaselineExplorer(state.baselines[idx] || null);
      return;
    }
    if (ev.target.id === 'explorer-close') {
      window.RegripViews?.renderBaselineExplorer(null);
      return;
    }
    const toolNode = ev.target.closest('[data-timeline-tool]');
    if (toolNode) {
      loadToolTimeline(toolNode.getAttribute('data-timeline-tool'));
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

  document.getElementById('timeline-tool-select')?.addEventListener('change', (ev) => {
    loadToolTimeline(ev.target.value);
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
      window.RegripViews?.renderImpact([], renderBadge);
      window.RegripViews?.renderDependencyGraph([], []);
      await syncTimelineTools([], []);
    }
  };

  init();
})();
