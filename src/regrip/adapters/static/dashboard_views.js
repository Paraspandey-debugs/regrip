(() => {
  const esc = (s) =>
    String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');

  const statusColor = (status) => {
    if (status === 'CONFIRMED_REGRESSION' || status === 'FAIL') return 'var(--fail-fg)';
    if (status === 'POTENTIALLY_BROKEN' || status === 'WARN') return 'var(--warn-fg)';
    return 'var(--pass-fg)';
  };

  const renderDependencyGraph = (impacts, findings) => {
    const body = document.getElementById('graph-body');
    const meta = document.getElementById('graph-meta');
    const agents = [...new Set((impacts || []).map((i) => i.agent_id))];
    const tools = [
      ...new Set([
        ...(impacts || []).map((i) => i.tool_name),
        ...(findings || []).map((f) => f.tool_name),
      ]),
    ];

    meta.textContent = `${agents.length} agent(s) · ${tools.length} tool(s)`;
    if (!agents.length || !tools.length) {
      body.innerHTML = '<div class="state-banner">Register consumer contracts to view agent-to-tool lineage.</div>';
      return;
    }

    const rowGap = 54;
    const height = Math.max(agents.length, tools.length) * rowGap + 48;
    const width = 700;
    const leftX = 160;
    const rightX = 520;

    const agentY = new Map();
    agents.forEach((a, idx) => agentY.set(a, 42 + idx * rowGap));
    const toolY = new Map();
    tools.forEach((t, idx) => toolY.set(t, 42 + idx * rowGap));

    const toolWorst = new Map();
    for (const f of findings || []) {
      const prev = toolWorst.get(f.tool_name);
      if (f.severity === 'FAIL' || !prev) toolWorst.set(f.tool_name, f.severity);
    }

    const edges = (impacts || [])
      .map((entry) => {
        const y1 = agentY.get(entry.agent_id);
        const y2 = toolY.get(entry.tool_name);
        const col = statusColor(entry.impact_status);
        const mx = (leftX + rightX) / 2;
        const paths = (entry.matched_jsonpaths || []).length;
        const label = paths ? `${paths} path(s)` : entry.impact_status.toLowerCase();
        return `<path d="M ${leftX} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${rightX} ${y2}" `
          + `fill="none" stroke="${col}" stroke-width="2" stroke-opacity="0.85" />`
          + `<text x="${mx}" y="${(y1 + y2) / 2 - 5}" text-anchor="middle" `
          + `class="graph-edge-label">${esc(label)}</text>`;
      })
      .join('');

    const agentNodes = agents
      .map((a) => {
        const y = agentY.get(a);
        const worst = (impacts || []).some(
          (i) => i.agent_id === a && i.impact_status === 'CONFIRMED_REGRESSION'
        )
          ? 'CONFIRMED_REGRESSION'
          : (impacts || []).some((i) => i.agent_id === a && i.impact_status === 'POTENTIALLY_BROKEN')
            ? 'POTENTIALLY_BROKEN'
            : 'UNAFFECTED';
        return `<g class="graph-node">`
          + `<rect x="14" y="${y - 16}" width="${leftX - 14}" height="32" rx="6" `
          + `fill="var(--bg)" stroke="${statusColor(worst)}" stroke-width="1.5" />`
          + `<text x="26" y="${y + 4}" class="graph-node-text">${esc(a)}</text></g>`;
      })
      .join('');

    const toolNodes = tools
      .map((t) => {
        const y = toolY.get(t);
        const sev = toolWorst.get(t) || 'PASS';
        return `<g class="graph-node graph-tool-node" data-timeline-tool="${esc(t)}">`
          + `<rect x="${rightX}" y="${y - 16}" width="${width - rightX - 14}" height="32" rx="6" `
          + `fill="var(--surface-code)" stroke="${statusColor(sev)}" stroke-width="1.5" />`
          + `<text x="${rightX + 12}" y="${y + 4}" class="graph-node-text">${esc(t)}</text></g>`;
      })
      .join('');

    body.innerHTML =
      `<svg class="dep-graph-svg" viewBox="0 0 ${width} ${height}" role="img" `
      + `aria-label="Agent to tool dependency graph">${edges}${agentNodes}${toolNodes}</svg>`;
  };

  const renderToolTimeline = (history) => {
    const body = document.getElementById('timeline-body');
    const meta = document.getElementById('timeline-meta');
    if (!history) {
      meta.textContent = '0 runs';
      body.innerHTML = '<div class="state-banner">Select a tool to inspect behavioral drift over releases.</div>';
      return;
    }

    const events = history.timeline || [];
    meta.textContent = `${history.total_recorded_calls} call(s) · ${events.length} run(s)`;
    const errPct = ((history.error_rate || 0) * 100).toFixed(1);
    const agents = (history.dependent_agents || []).join(', ') || 'none';

    let chartHtml = '<div class="panel-meta">No replay runs recorded for this tool yet.</div>';
    if (events.length) {
      const w = 540;
      const h = 150;
      const padL = 36;
      const padB = 28;
      const plotW = w - padL - 16;
      const plotH = h - padB - 16;
      const step = plotW / events.length;
      const barW = Math.min(44, step * 0.56);

      const bars = events
        .map((ev, idx) => {
          const pct = Math.min(100, Math.round((ev.failure_rate || 0) * 100));
          const bh = Math.max(3, (pct / 100) * plotH);
          const x = padL + idx * step + (step - barW) / 2;
          const y = 16 + plotH - bh;
          const fill = pct > 0 ? 'var(--fail-fg)' : 'var(--pass-fg)';
          const lbl = ev.target_git_sha ? ev.target_git_sha.slice(0, 7) : ev.baseline_tag;
          return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${bh.toFixed(1)}" rx="3" fill="${fill}" fill-opacity="0.85" />`
            + `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}" text-anchor="middle" class="chart-val">${pct}%</text>`
            + `<text x="${(x + barW / 2).toFixed(1)}" y="${h - 8}" text-anchor="middle" class="chart-axis">${esc(lbl)}</text>`;
        })
        .join('');

      chartHtml =
        `<svg class="timeline-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Failure rate timeline">`
        + `<line x1="${padL}" y1="16" x2="${padL}" y2="${16 + plotH}" stroke="var(--border)" />`
        + `<line x1="${padL}" y1="${16 + plotH}" x2="${w - 16}" y2="${16 + plotH}" stroke="var(--border)" />`
        + `${bars}</svg>`;
    }

    const topArgs = (history.top_parameter_combinations || [])
      .map((p) => `<div class="param-row"><code class="path-chip">${esc(p.arguments)}</code><span class="panel-meta">×${p.count}</span></div>`)
      .join('');

    body.innerHTML =
      `<div class="timeline-stats">`
      + `<div class="stat-chip"><span>Error Rate</span><strong>${errPct}%</strong></div>`
      + `<div class="stat-chip"><span>Consumers</span><strong>${esc(agents)}</strong></div>`
      + `</div>`
      + chartHtml
      + (topArgs ? `<div class="param-list"><div class="verdict-eyebrow">TOP CALL ARGUMENTS</div>${topArgs}</div>` : '');
  };

  const renderBaselineExplorer = (baseline) => {
    const panel = document.getElementById('baseline-explorer');
    const body = document.getElementById('explorer-body');
    const title = document.getElementById('explorer-title');
    if (!baseline) {
      panel.classList.add('is-hidden');
      return;
    }
    panel.classList.remove('is-hidden');
    title.textContent = `Baseline Explorer — ${baseline.server_name}@${baseline.tag}`;

    const masks = (baseline.noise_masks || []).length
      ? baseline.noise_masks.map((m) => `<code class="path-chip">${esc(m)}</code>`).join('')
      : '<span class="panel-meta">No volatile paths masked</span>';

    const casesHtml = (baseline.cases || [])
      .map((c) => {
        const fields = Object.entries(c.expected_schema_profile || {})
          .map(
            ([path, prof]) =>
              `<li><code class="path-chip">${esc(path)}</code> `
              + `<span class="panel-meta">${esc((prof.observed_types || []).join('|'))}</span></li>`
          )
          .join('');
        return `<article class="explorer-case">`
          + `<div class="agent-tool-top"><strong class="mono">${esc(c.tool_name)}</strong>`
          + `<span class="panel-meta">${c.sample_count} sample(s) · ${esc(c.expected_status)}</span></div>`
          + `<pre class="explorer-json">${esc(JSON.stringify(c.representative_input, null, 2))}</pre>`
          + (fields ? `<ul class="explorer-fields">${fields}</ul>` : '')
          + `</article>`;
      })
      .join('');

    body.innerHTML =
      `<div class="explorer-meta">`
      + `<div class="verdict-eyebrow">AUTO-MASKED VOLATILE FIELDS</div>`
      + `<div class="agent-paths">${masks}</div>`
      + `</div>`
      + `<div class="explorer-grid">${casesHtml || '<span class="panel-meta">No cases</span>'}</div>`;
  };

  const renderImpact = (impacts, renderBadge) => {
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
    const rank = (s) => (s === 'CONFIRMED_REGRESSION' ? 2 : s === 'POTENTIALLY_BROKEN' ? 1 : 0);
    body.innerHTML = [...byAgent.entries()]
      .map(([agent, tools]) => {
        const worst = tools.reduce(
          (w, t) => (rank(t.impact_status) > rank(w) ? t.impact_status : w),
          'UNAFFECTED'
        );
        const rows = tools
          .map((t) => {
            const paths = (t.matched_jsonpaths || []).length
              ? t.matched_jsonpaths.map((p) => `<code class="path-chip">${esc(p)}</code>`).join('')
              : '<span class="panel-meta">No impacted paths</span>';
            return `<div class="agent-tool-row"><div class="agent-tool-top">`
              + `<span class="mono">${esc(t.tool_name)}</span>${renderBadge(t.impact_status, 'Impact')}`
              + `</div><div class="agent-paths">${paths}</div></div>`;
          })
          .join('');
        return `<article class="agent-card"><div class="agent-head">`
          + `<span class="agent-name">${esc(agent)}</span>${renderBadge(worst, 'Agent Status')}`
          + `</div><div class="agent-tools">${rows}</div></article>`;
      })
      .join('');
  };

  window.RegripViews = {
    esc,
    renderImpact,
    renderDependencyGraph,
    renderToolTimeline,
    renderBaselineExplorer,
  };
})();
