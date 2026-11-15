const PRESETS = {
  skew: `SELECT m.merchant_id,
       m.merchant_name,
       COUNT(*)      AS order_cnt,
       SUM(o.amount) AS total_amount,
       SUM(o.fee)    AS total_fee
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE left(m.category, 1) = 'A'
GROUP BY m.merchant_id, m.merchant_name
ORDER BY total_amount DESC`,
  single: `SELECT o.order_no, o.amount, o.fee, o.settle_date, m.merchant_name
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE m.merchant_id = 42
  AND o.settle_date >= DATE '2026-11-01'
  AND o.settle_date <  DATE '2026-11-14'
ORDER BY o.settle_date DESC, o.order_no`,
  daily: `SELECT settle_date, COUNT(*) AS order_cnt, SUM(amount) AS total_amount
FROM settlement_orders
WHERE settle_date >= DATE '2026-11-01'
  AND settle_date <  DATE '2026-11-14'
GROUP BY settle_date
ORDER BY settle_date`,
};

const sqlInput = document.getElementById('sql-input');
sqlInput.value = PRESETS.skew;
document.getElementById('preset').onchange = (e) => {
  sqlInput.value = PRESETS[e.target.value] || '';
};

const expanded = new Set();   // 展开了计划详情的批次 id

const STATUS_TEXT = {
  pending: '排队中', running: '运行中', completed: '已完成',
  failed: '失败', timeout: '超时', cancelled: '已取消',
};
const SETTING_TEXT = {
  default: '默认 (enable_nestloop=on)',
  nestloop_off: '抑制嵌套循环 (enable_nestloop=off)',
};
const EX_STATUS_TEXT = {
  pending: '待执行', running: '执行中', ok: '成功',
  timeout: '超时', cancelled: '已取消', error: '报错',
};
const VERDICT_TEXT = {
  improved: '有改善', not_improved: '无改善', inconclusive: '无法判断改善',
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function fmtMs(v) { return v == null ? '—' : Number(v).toFixed(1) + ' ms'; }
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }
function fmtIso(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  return isNaN(d) ? String(ts) : d.toLocaleString();
}
function fmtNum(v, digits = 0) {
  return v == null ? '—' : Number(v).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

/* ------------------------------------------------------------------ 采样器 */

let samplerState = null;
let samplerSignature = '';
let autoTouched = false;   // 用户手动改过停止/间隔后，不再用服务端状态覆盖表单

function samplerMsg(text, kind) {
  const el = document.getElementById('sampler-msg');
  el.className = 'msg' + (kind ? ' ' + kind : '');
  el.textContent = text || '';
}

function renderSampler(data) {
  samplerState = data;
  // 候选列表和样本基本不变时才跳过重绘，避免 1.5s 轮询打断选择与滚动
  const signature = JSON.stringify({ target: data.target, samples: data.samples, candidates: data.candidates, available: data.available });
  if (signature === samplerSignature) {
    renderAuto(data.auto || {});
    return;
  }
  samplerSignature = signature;
  const statusBox = document.getElementById('sampler-status');
  if (!data.available) {
    statusBox.innerHTML = `<div class="conclusion none">
      <b>pg_stat_statements 不可用</b> · ${escapeHtml(data.reason || '')}
      <div class="hint">确认数据库已 <code>shared_preload_libraries = 'pg_stat_statements'</code>
      并执行过 <code>CREATE EXTENSION pg_stat_statements</code>。</div>
    </div>`;
  } else {
    const srv = data.server || {};
    statusBox.innerHTML = `<div class="sampler-meta">
      <span>数据库 <b>${escapeHtml(srv.database || '—')}</b> (dbid ${srv.dbid ?? '—'})</span>
      <span>用户 <b>${escapeHtml(srv.username || '—')}</b> (userid ${srv.userid ?? '—'})</span>
      <span>统计起始 <b>${fmtIso(data.stats_reset)}</b></span>
      <span>已保存样本 ${fmtNum((data.samples || []).length)} 条（最多 ${data.max_samples}）</span>
    </div>`;
  }
  renderTarget(data);
  renderSamples(data.samples || []);
  renderAuto(data.auto || {});
}

function renderTarget(data) {
  const box = document.getElementById('sampler-target');
  const t = data.target;
  const targetHtml = t
    ? `<div class="target-card">
        <div class="target-head">
          <span class="badge completed">正在采样</span>
          <span class="hint">dbid ${t.dbid} · userid ${t.userid} · queryid <code>${escapeHtml(t.queryid)}</code></span>
          <span class="spacer"></span>
          <button class="btn-toggle" data-clear-target="1">取消跟踪</button>
        </div>
        <pre class="sql">${escapeHtml(t.query)}</pre>
      </div>`
    : `<div class="conclusion none">尚未选定跟踪目标。先运行一次结算查询，再在下方候选中选择。</div>`;

  const candidates = data.candidates || [];
  const rows = candidates.map((c, i) => `<tr>
    <td>${i + 1}</td>
    <td>${escapeHtml(c.username)}<div class="hint">uid ${c.userid}</div></td>
    <td><code>${escapeHtml(c.queryid)}</code></td>
    <td>${fmtNum(c.calls)}</td>
    <td>${fmtNum(c.total_exec_time_ms, 1)} ms</td>
    <td>${fmtNum(c.mean_exec_ms, 3)} ms</td>
    <td><pre class="cand-sql">${escapeHtml(c.query)}</pre></td>
    <td><button class="btn-toggle" data-select-target="${i}"
        data-key="${c.dbid}:${c.userid}:${c.queryid}">跟踪此条</button></td>
  </tr>`).join('');

  const candHtml = candidates.length
    ? `<table class="ex-table sampler-cands">
        <thead><tr><th>#</th><th>用户</th><th>queryid</th><th>累计调用</th>
        <th>累计耗时</th><th>平均耗时</th><th>语句</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`
    : `<p class="hint">还没有涉及 <code>settlement_orders</code> 的 SELECT 记录。
        点击上方「运行结算查询产生流量」即可产生一条可采样的固定语句。</p>`;

  box.innerHTML = targetHtml + candHtml;
  box.querySelectorAll('[data-select-target]').forEach(btn => {
    btn.onclick = () => selectTarget(btn.dataset.key);
  });
  const clearBtn = box.querySelector('[data-clear-target]');
  if (clearBtn) clearBtn.onclick = clearTarget;
}

async function selectTarget(key) {
  // 用渲染时记录的 "dbid:userid:queryid" 定位，避免轮询刷新后候选顺序变化
  const parts = String(key || '').split(':');
  if (parts.length !== 3) return;
  const [dbid, userid, queryid] = parts;
  samplerMsg('正在选定目标…');
  try {
    const res = await fetch('/api/sampler/target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // queryid 是 64 位整数，超出 JS 安全整数范围，必须按字符串原样回传
      body: JSON.stringify({ dbid: Number(dbid), userid: Number(userid), queryid }),
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok) {
      samplerMsg(d.baseline ? '已选定目标并记录基线样本' : '已选定目标，并记录一条续接样本', 'ok');
    } else {
      samplerMsg('选定失败：' + (d.detail || ('HTTP ' + res.status)), 'err');
    }
  } catch (e) {
    samplerMsg('网络错误：' + e, 'err');
  }
  refreshSampler();
}

async function clearTarget() {
  if (!confirm('停止跟踪当前 SQL？（已保存的采样记录会保留）')) return;
  await fetch('/api/sampler/target', { method: 'DELETE' });
  samplerMsg('已停止跟踪', 'ok');
  refreshSampler();
}

const GAP_TEXT = {
  stats_reset: '统计重置',
  target_reappeared: '条目重现',
  counter_regressed: '计数回退',
  missing: '条目消失',
};

function renderSamples(samples) {
  const box = document.getElementById('sampler-samples');
  if (!samples.length) {
    box.innerHTML = '<p class="hint">暂无采样记录</p>';
    return;
  }
  const rows = samples.map(s => {
    const gap = s.gap_reason
      ? `<span class="gap-tag">断档：${GAP_TEXT[s.gap_reason] || s.gap_reason}</span>`
      : '';
    const note = s.state === 'missing'
      ? escapeHtml(s.gap_label || '条目已消失')
      : (s.is_baseline ? '基线样本（首个样本，无前置区间）' : gap);
    const avg = s.avg_exec_ms == null ? '—' : fmtNum(s.avg_exec_ms, 3) + ' ms';
    const delta = s.delta_calls == null ? '—' : '+' + fmtNum(s.delta_calls);
    return `<tr class="${s.gap_reason || s.state === 'missing' ? 'gap-row' : ''}">
      <td class="nowrap">${fmtIso(s.sampled_at)}</td>
      <td class="num">${delta}</td>
      <td class="num">${avg}</td>
      <td class="num">${s.delta_exec_time_ms == null ? '—' : fmtNum(s.delta_exec_time_ms, 1) + ' ms'}</td>
      <td class="num">${fmtNum(s.total_calls)}</td>
      <td class="num">${s.total_exec_time_ms == null ? '—' : fmtNum(s.total_exec_time_ms, 1) + ' ms'}</td>
      <td>${note}</td>
    </tr>`;
  }).join('');
  box.innerHTML = `<table class="ex-table sampler-samples">
    <thead><tr><th>采样时间</th><th>新增调用</th><th>平均耗时</th>
    <th>新增耗时</th><th>累计调用</th><th>累计耗时</th><th>说明</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="hint">平均耗时 = 相邻两个有效样本之间新增总耗时 ÷ 新增调用数；断档行不参与相减。</p>`;
}

function renderAuto(auto) {
  const toggle = document.getElementById('auto-toggle');
  const interval = document.getElementById('auto-interval');
  const msg = document.getElementById('auto-msg');
  if (!autoTouched) {
    toggle.checked = !!auto.running;
    if (auto.running && auto.interval_s) interval.value = auto.interval_s;
  }
  if (auto.running) {
    msg.className = 'msg ok';
    msg.textContent = `自动采样中，每 ${auto.interval_s}s 一次`
      + (auto.last_sample_at ? `，上次 ${fmtIso(auto.last_sample_at)}` : '');
  } else if (auto.last_error) {
    msg.className = 'msg err';
    msg.textContent = '自动采样已停止，最近错误：' + auto.last_error;
  } else {
    msg.className = 'msg';
    msg.textContent = '';
  }
}

async function refreshSampler() {
  try {
    const res = await fetch('/api/sampler');
    const data = await res.json();
    if (res.ok) {
      renderSampler(data);
    } else {
      document.getElementById('sampler-status').innerHTML =
        `<div class="conclusion none">采样状态不可用：${escapeHtml(data.detail || res.status)}</div>`;
    }
  } catch (e) { /* 忽略瞬时网络错误，下一轮再试 */ }
}

document.getElementById('traffic-btn').onclick = async () => {
  const btn = document.getElementById('traffic-btn');
  const times = Number(document.getElementById('traffic-times').value) || 5;
  btn.disabled = true;
  samplerMsg('正在执行结算查询…');
  try {
    const res = await fetch('/api/sampler/traffic', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ times }),
    });
    const d = await res.json().catch(() => ({}));
    if (res.ok) {
      let text = `已执行 ${d.times} 次，耗时 ${d.elapsed_ms} ms`;
      if (d.queryid) text += `，queryid ${d.queryid}`;
      if (d.auto_pinned) text += '；已自动选定该语句为跟踪目标';
      if (d.warning) {
        samplerMsg(text + ' · ' + d.warning, 'warn');
      } else {
        samplerMsg(text, 'ok');
      }
    } else {
      samplerMsg('执行失败：' + (d.detail || ('HTTP ' + res.status)), 'err');
    }
  } catch (e) {
    samplerMsg('网络错误：' + e, 'err');
  } finally {
    btn.disabled = false;
  }
  refreshSampler();
};

document.getElementById('sample-btn').onclick = async () => {
  samplerMsg('采样中…');
  try {
    const res = await fetch('/api/sampler/samples', { method: 'POST' });
    const d = await res.json().catch(() => ({}));
    if (res.ok) {
      const s = d.sample;
      if (s.gap_reason || s.state === 'missing') {
        samplerMsg('已记录断档样本：' + (s.gap_label || s.gap_reason), 'warn');
      } else if (s.is_baseline) {
        samplerMsg('已记录基线样本', 'ok');
      } else if (s.delta_calls === 0) {
        samplerMsg('已采样：自上次以来没有新增调用', 'warn');
      } else {
        samplerMsg(`已采样：新增 ${fmtNum(s.delta_calls)} 次调用，平均 ${fmtNum(s.avg_exec_ms, 3)} ms`, 'ok');
      }
    } else {
      samplerMsg('采样失败：' + (d.detail || ('HTTP ' + res.status)), 'err');
    }
  } catch (e) {
    samplerMsg('网络错误：' + e, 'err');
  }
  refreshSampler();
};

async function setAuto(running) {
  const interval = Number(document.getElementById('auto-interval').value) || 30;
  const res = await fetch('/api/sampler/auto', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ running, interval_s: interval }),
  });
  const d = await res.json().catch(() => ({}));
  const msg = document.getElementById('auto-msg');
  if (!res.ok) {
    msg.className = 'msg err';
    msg.textContent = '设置失败：' + (d.detail || ('HTTP ' + res.status));
    document.getElementById('auto-toggle').checked = !running;
    autoTouched = false;
  } else {
    autoTouched = false;
    samplerMsg(running ? '已启动定时采样' : '已停止定时采样', running ? 'ok' : '');
  }
  refreshSampler();
}

document.getElementById('auto-toggle').onchange = (e) => {
  autoTouched = true;
  setAuto(e.target.checked);
};
document.getElementById('auto-interval').onchange = () => {
  autoTouched = true;
  if (document.getElementById('auto-toggle').checked) setAuto(true);
  else autoTouched = false;
};

async function refresh() {
  try {
    const res = await fetch('/api/batches');
    const data = await res.json();
    renderSlot(data);
    await renderBatches(data.batches);
  } catch (e) { /* 忽略瞬时网络错误，下一轮再试 */ }
  refreshSampler();
}

function renderSlot(data) {
  const pill = document.getElementById('slot-pill');
  if (data.busy) {
    pill.textContent = '运行中（名额已占用）';
    pill.className = 'pill busy';
  } else {
    pill.textContent = '空闲，可提交对照';
    pill.className = 'pill idle';
  }
}

function conclusionHtml(b) {
  if (b.conclusion) {
    const v = b.conclusion.verdict;
    return `<div class="conclusion ${v}">
      <b>${VERDICT_TEXT[v] || v}</b> · ${escapeHtml(b.conclusion.message)}
    </div>`;
  }
  if (['failed', 'timeout', 'cancelled'].includes(b.status)) {
    const reason = { failed: '报错', timeout: '超时', cancelled: '已取消' }[b.status];
    return `<div class="conclusion none">无比较结论（批次${reason}）</div>`;
  }
  return '';
}

function executionsHtml(b) {
  if (!b.executions.length) {
    return b.status === 'running' ? '<p class="hint">正在建立事务快照…</p>' : '';
  }
  const rows = b.executions.map(ex => `<tr>
    <td>第 ${ex.round} 轮</td>
    <td>#${ex.seq}</td>
    <td>${SETTING_TEXT[ex.setting] || ex.setting}</td>
    <td><span class="ex-status ${ex.status}">${EX_STATUS_TEXT[ex.status] || ex.status}</span></td>
    <td>${fmtMs(ex.execution_time_ms)}</td>
    <td>${fmtMs(ex.planning_time_ms)}</td>
    <td>${ex.error ? escapeHtml(ex.error) : ''}</td>
  </tr>`).join('');
  return `<table class="ex-table">
    <thead><tr><th>轮次</th><th>顺序</th><th>设置</th><th>状态</th>
    <th>执行耗时</th><th>规划耗时</th><th>备注</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function batchCardHtml(b) {
  const cancelBtn = (b.status === 'running' || b.status === 'pending')
    ? (b.cancel_requested
        ? '<span class="msg warn">取消中…</span>'
        : `<button class="btn-cancel" data-cancel="${b.id}">取消本批</button>`)
    : '';
  const toggleBtn = b.executions.length
    ? `<button class="btn-toggle" data-toggle="${b.id}">${expanded.has(b.id) ? '收起计划' : '查看计划与结构'}</button>`
    : '';
  return `<div class="card batch">
    <div class="batch-head">
      <span class="badge ${b.status}">${STATUS_TEXT[b.status] || b.status}</span>
      <code>#${b.id}</code>
      <span class="hint">提交于 ${fmtTime(b.created_at)}</span>
      <span class="spacer"></span>
      ${cancelBtn}${toggleBtn}
    </div>
    <pre class="sql">${escapeHtml(b.sql)}</pre>
    ${b.error ? `<div class="conclusion none">错误：${escapeHtml(b.error)}</div>` : ''}
    ${conclusionHtml(b)}
    ${executionsHtml(b)}
    <div class="detail" id="detail-${b.id}"></div>
  </div>`;
}

async function renderBatches(batches) {
  const container = document.getElementById('batches');
  container.innerHTML = batches.length
    ? batches.map(batchCardHtml).join('')
    : '<p class="hint">暂无批次</p>';
  container.querySelectorAll('[data-cancel]').forEach(btn => {
    btn.onclick = () => cancelBatch(btn.dataset.cancel);
  });
  container.querySelectorAll('[data-toggle]').forEach(btn => {
    btn.onclick = () => toggleDetail(btn.dataset.toggle);
  });
  for (const id of expanded) {
    if (batches.some(b => b.id === id)) await loadDetail(id);
  }
}

async function loadDetail(id) {
  const box = document.getElementById(`detail-${id}`);
  if (!box) return;
  try {
    const res = await fetch(`/api/batches/${id}`);
    if (!res.ok) { box.innerHTML = ''; return; }
    const d = await res.json();
    box.innerHTML = d.executions.map(ex => {
      const struct = (ex.structure_lines || []).join('\n');
      const plan = ex.plan ? JSON.stringify(ex.plan, null, 2) : '（无计划）';
      return `<div class="ex-detail">
        <h4>第 ${ex.round} 轮 · #${ex.seq} · ${SETTING_TEXT[ex.setting]} ·
            ${EX_STATUS_TEXT[ex.status]}${ex.execution_time_ms != null ? ' · ' + fmtMs(ex.execution_time_ms) : ''}</h4>
        ${struct ? `<pre class="struct">${escapeHtml(struct)}</pre>` : ''}
        <details><summary>完整计划 JSON</summary><pre class="plan">${escapeHtml(plan)}</pre></details>
      </div>`;
    }).join('');
  } catch (e) { /* 下一轮再试 */ }
}

async function toggleDetail(id) {
  if (expanded.has(id)) {
    expanded.delete(id);
    const box = document.getElementById(`detail-${id}`);
    if (box) box.innerHTML = '';
  } else {
    expanded.add(id);
    await loadDetail(id);
  }
  refresh();
}

async function cancelBatch(id) {
  await fetch(`/api/batches/${id}/cancel`, { method: 'POST' });
  refresh();
}

document.getElementById('run-btn').onclick = async () => {
  const msg = document.getElementById('submit-msg');
  msg.className = 'msg';
  msg.textContent = '提交中…';
  try {
    const res = await fetch('/api/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: sqlInput.value }),
    });
    if (res.status === 409) {
      msg.textContent = '繁忙：已有对照批次在运行，请稍后再试';
      msg.classList.add('warn');
    } else if (res.status === 400) {
      const d = await res.json();
      msg.textContent = '校验失败：' + (d.detail || '');
      msg.classList.add('err');
    } else if (res.ok) {
      msg.textContent = '批次已创建，开始运行';
      msg.classList.add('ok');
    } else {
      msg.textContent = '提交失败（HTTP ' + res.status + '）';
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
  refresh();
};

document.getElementById('insert-btn').onclick = async () => {
  const msg = document.getElementById('insert-msg');
  msg.className = 'msg';
  try {
    const res = await fetch('/api/sample-row', { method: 'POST' });
    const d = await res.json();
    if (d.ok) {
      msg.textContent = `已补记 ${d.row.order_no}（商户 ${d.row.merchant_id}，金额 ${d.row.amount}，结算日 ${d.row.settle_date}）`;
      msg.classList.add('ok');
    } else {
      msg.textContent = '写入失败';
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
};

refresh();
setInterval(refresh, 1500);
