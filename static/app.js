/* ================= AntFund AI · Demo 前端 ================= */
const S = {
  cfg: null,
  sessionId: null,
  userId: null,
  promptVersion: 'v3.0',
  profile: null,
  lastTrace: null,
  traces: [],
  busy: false,
};

const LAYERS = [
  { key: 'observe', idx: '1', name: 'Observe 感知层', desc: '画像 / 行为事件流 / 对话记忆' },
  { key: 'plan', idx: '2', name: 'Plan 决策层 · Planner 主控', desc: '意图识别 / 用户分型 / 开口时机 / 策略选择' },
  { key: 'harness', idx: '3', name: 'Harness 调度层', desc: 'Prompt 版本 / Context 拼包 / 工具权限 / 状态机 / 降级' },
  { key: 'act', idx: '4', name: 'Act 执行层 · 6 个 Executor', desc: '画像 / 话术 / RAG 检索 / 推荐 / 适当性校验 / 转人工' },
  { key: 'reflect', idx: '5', name: 'Reflect 复盘层', desc: 'Trace 全量落库 / 自动评估 / Badcase 聚类反哺' },
];

const TRIGGERS = [
  {key:'browse_funds',label:'浏览多只基金',cond:'先匹配风险与期限，再比较',risk:false},
  {key:'assessment_stuck',label:'风险测评未完成',cond:'基础解释，不推产品',risk:false},
  {key:'assessment_complete',label:'模拟完成测评 · C2',cond:'读取测评工具的演示结果',risk:false},
  {key:'drawdown',label:'持有净值回落',cond:'承接情绪，解释风险',risk:false},
  {key:'sip_failed',label:'定投扣款失败',cond:'核对授权与扣款安排',risk:false},
  {key:'redeem_query',label:'赎回与到账咨询',cond:'先核对生效产品规则',risk:false},
  {key:'purchase_confirm',label:'模拟申购确认',cond:'有匹配候选后，显式确认',risk:false},
  {key:'pause',label:'停止主动提醒',cond:'停止后不再推送',risk:false},
  {key:'risk_handoff',label:'投诉与风险升级',cond:'停止自动推介，转人工',risk:true},
];
const ADV_SAMPLES = ['这只基金稳赚不赔，保证年化收益。','手续费只有0.01%，现在就买。','不用风险测评也可以买这只基金。','全市场最好、最赚钱的基金。','贷款买基金可以翻倍。','基金有风险，投资需谨慎；不能保证收益。'];

/* ---------------- utils ---------------- */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (v) => `${(v * 100).toFixed(1)}%`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body, method = 'POST') {
  // 双模式：有真实后端 → 走 http；纯静态部署 → 由 bridge.js 在浏览器内跑 Python 引擎。
  // 判断权交给运行时（它自己探测），这里只负责等结果，避免两边逻辑打架。
  if (window.ANTFUND_RUNTIME) {
    const mode = await window.ANTFUND_RUNTIME.ready;
    if (mode === 'bridge') return window.ANTFUND_RUNTIME.call(path, body, method);
  }
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${path} ${res.status}`);
  return res.json();
}

/* ---------------- init ---------------- */
async function init() {
  S.cfg = await api('/api/config', null, 'GET');
  renderModelBadges();
  renderUsers();
  renderTriggers();
  renderAdversarial();
  renderBadcaseLib();
  renderPipelineShell();
  await loadMetrics();
  await loadTraces();
  bindEvents();
}

function renderModelBadges() {
  const m = S.cfg.models;
  $('#modelBadges').innerHTML = `
    <span class="model-chip primary">${esc(m.primary.name)} · 演示</span>
    <span class="model-chip tool">${esc(m.tool.name)} · 演示</span>
    <span class="model-chip sensitive">${esc(m.sensitive.name)} · 敏感场景</span>`;
}

function renderUsers() {
  const wrap = $('#userList');
  wrap.innerHTML = Object.entries(S.cfg.profiles).map(([uid, p]) => `
    <div class="user-item" role="button" tabindex="0" data-uid="${uid}">
      <div class="name">${esc(p.name)} · ${p.age}岁 · ${esc(p.city)}</div>
      <div class="meta">${esc(p.persona_desc)}</div>
      <div class="tagline">
        <span class="tag type">${esc(p.typing_hint)}</span>
        <span class="tag">${esc(p.persona)}</span>
        <span class="tag">${esc(p.ltv_tier)}</span>
      </div>
    </div>`).join('');
  wrap.querySelectorAll('.user-item').forEach((el) => {
    el.addEventListener('click', () => selectUser(el.dataset.uid));
    el.addEventListener('keydown', e => {if(e.key==='Enter'||e.key===' '){e.preventDefault();selectUser(el.dataset.uid);}});
  });
}

async function selectUser(uid) {
  S.userId = uid;
  document.querySelectorAll('.user-item').forEach((el) => el.classList.toggle('active', el.dataset.uid === uid));
  const r = await api('/api/session', { user_id: uid, prompt_version: S.promptVersion });
  S.sessionId = r.session_id;
  S.profile = r.profile;
  const pb = r.persona_playbook || {};
  $('#profileDetail').innerHTML = `
    <div class="kv"><b>关注点</b><span>${esc((r.profile.concerns || []).join('、'))}</span></div>
    <div class="kv"><b>预算</b><span>${esc(r.profile.budget_signal)}</span></div>
    <div class="kv"><b>近24h</b><span>${esc((r.profile.behaviors_24h || []).join('；'))}</span></div>
    <div class="kv"><b>历史</b><span>${esc((r.profile.history || []).slice(0, 2).join('；'))}</span></div>
    <div class="kv"><b>策略</b><span>${esc(pb.strategy || '')} · ${esc(pb.tone || '')}</span></div>`;
  $('#sessionLabel').textContent = `${r.profile.name} · ${r.profile.persona}`;
  $('#chatMeta').textContent = `${S.sessionId} · prompt ${r.prompt_version}`;
}

function renderTriggers() {
  $('#triggerList').innerHTML = TRIGGERS.map((t) => `
    <button class="trigger-btn ${t.risk ? 'risk' : ''}" data-k="${t.key}">
      <span class="dot"></span>${esc(t.label)}<span class="cond">${esc(t.cond)}</span>
    </button>`).join('');
  $('#triggerList').querySelectorAll('.trigger-btn').forEach((el) => {
    el.addEventListener('click', () => runTrigger(el.dataset.k));
  });
}

function renderAdversarial() {
  $('#adversarialList').innerHTML = ADV_SAMPLES.map((s, i) =>
    `<div class="adv-chip" data-i="${i}">${esc(s)}</div>`).join('');
  $('#adversarialList').querySelectorAll('.adv-chip').forEach((el) => {
    el.addEventListener('click', () => {
      $('#advText').value = ADV_SAMPLES[Number(el.dataset.i)];
      runAdversarial();
    });
  });
}

function renderBadcaseLib() {
  $('#badcaseLib').innerHTML = S.cfg.badcases.map((b) => `
    <div class="badcase">
      <div class="bc-head">
        <span class="bc-type">${esc(b.type)}</span>
        <span class="bc-cause">${esc(b.scene)}</span>
        <span class="bc-status">${esc(b.status)}</span>
      </div>
      <div class="bc-bad">原输出：${esc(b.bad_output)}</div>
      <div class="bc-fix">修正：${esc(b.fixed)}</div>
      <div class="bc-cause">归因：${esc(b.root_cause)} ｜命中 ${esc((b.gate_hit || []).join('、'))}</div>
    </div>`).join('');
}

function renderPipelineShell() {
  $('#pipeline').innerHTML = LAYERS.map((l) => `
    <div class="layer" data-layer="${l.key}">
      <div class="layer-head">
        <span class="idx">${l.idx}</span>
        <div>
          <div>${esc(l.name)}</div>
          <div class="muted" style="font-size:12px">${esc(l.desc)}</div>
        </div>
        <span class="cnt">0</span>
      </div>
      <div class="layer-steps"></div>
    </div>`).join('');
}

function clearPipeline() {
  document.querySelectorAll('.layer-steps').forEach((el) => (el.innerHTML = ''));
  document.querySelectorAll('.layer .cnt').forEach((el) => (el.textContent = '0'));
}

/* ---------------- 事件流动画 ---------------- */
async function animate(events, speed = matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 90) {
  clearPipeline();
  const counts = {};
  for (const ev of events) {
    const layer = document.querySelector(`.layer[data-layer="${ev.layer}"]`);
    if (!layer) continue;
    counts[ev.layer] = (counts[ev.layer] || 0) + 1;
    layer.querySelector('.cnt').textContent = counts[ev.layer];
    const step = document.createElement('div');
    step.className = `step ${ev.status}`;
    const meta = ev.meta && Object.keys(ev.meta).length
      ? `<details><summary>查看原始数据</summary><pre>${esc(JSON.stringify(ev.meta, null, 2))}</pre></details>` : '';
    step.innerHTML = `
      <div class="st-head">
        <span class="st-agent">${esc(ev.agent)}</span>
        <span class="st-act">${esc(ev.action)}</span>
        <span class="st-ms">${ev.latency_ms}ms</span>
      </div>
      <div class="st-detail">${esc(ev.detail)}</div>${meta}`;
    layer.querySelector('.layer-steps').appendChild(step);
    if (ev.layer === 'plan' && ev.meta) updatePlanBanner(ev.meta);
    if (ev.meta && ev.meta.gate) renderGateRow(ev.meta.gate);
    await sleep(speed);
  }
}

function updatePlanBanner(meta) {
  $('#planBanner').innerHTML = `
    <strong>Planner 决策</strong>：场景「${esc(meta.scene)}」× 分型「${esc(meta.user_type)}」
    → 意图「${esc(meta.intent)}」→ 策略「${esc(meta.strategy)}」
    <div class="muted" style="margin-top:3px">调度顺序：${esc((meta.executors || []).join(' → '))}
    ｜开口时机：${esc((meta.timing || {}).window || '')}</div>`;
}

/* ---------------- 合规闸 ---------------- */
function resetGates() { $('#gateList').innerHTML = ''; }

function renderGateRow(g) {
  const el = document.createElement('div');
  el.className = 'gate';
  const hits = (g.hits || []).length
    ? `<div class="gate-hits">命中：${esc((g.hits || []).map(h => h.word || h.type || h.pattern || h.reason || '—').join('、'))}</div>` : '';
  el.innerHTML = `
    <span class="status ${g.status}">${g.status}</span>
    <span class="gname">${esc(g.gate)} ${esc(g.name)}</span>
    <span class="gnote">${esc(g.note)}</span>`;
  if (hits) el.innerHTML += hits;
  $('#gateList').appendChild(el);
}

/* ---------------- 会话渲染 ---------------- */
function addUserMsg(text) {
  const empty = $('#chatEmpty');
  if (empty) empty.remove();
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `<div class="avatar">用户</div><div class="bubble">${esc(text)}</div>`;
  $('#chatBody').appendChild(el);
  scrollChat();
}

function addAiMsg(data) {
  const blocked = data.session.state === 'HANDOFF';
  const el = document.createElement('div');
  el.className = `msg ai ${blocked ? 'blocked' : ''}`;
  const vo = data.voice || {};
  const chips = [
    vo.empathy_kind
      ? `<span class="mini-chip empathy" title="情绪价值动作：先接住情绪再谈方案">情绪价值 ${esc(vo.empathy_kind)}</span>`
      : '',
    vo.budget
      ? `<span class="mini-chip" title="文风红线：文案必须落在意图对应的字数预算内">${vo.used}/${vo.budget} 字</span>`
      : '',
    `<span class="mini-chip">${esc(data.llm.model)}</span>`,
    `<span class="mini-chip">${esc(data.llm.route)} 路由 / ${data.llm.mode === 'api' ? '真实 API' : '离线合成'}</span>`,
    `<span class="mini-chip">Prompt ${esc(data.prompt.used)}</span>`,
    `<span class="mini-chip clickable" data-trace="${esc(data.trace_id)}">Trace ${esc(data.trace_id)}</span>`,
    `<span class="mini-chip">链路 ${data.trace.total_latency_ms}ms</span>`,
    `<span class="mini-chip">演示无模型成本</span>`,
  ].filter(Boolean).join('');
  if (data.handoff.should) {
    chips + '';
  }
  el.innerHTML = `
    <div class="avatar">AI</div>
    <div>
      <div class="bubble">${esc(data.final_text)}
        <span class="sys-note">4 道闸：${esc(data.gate_summary)}｜风险分 ${data.risk_score}
        ${data.handoff.should ? `｜已转人工：${esc(data.handoff.route)}（${esc(data.handoff.sla)}）` : ''}</span>
      </div>
      <div class="msg-tools">${chips}</div>
      ${renderRecommendation(data)}
      ${renderSuitability(data)}
      ${renderSources(data)}
    </div>`;
  $('#chatBody').appendChild(el);
  el.querySelectorAll('.mini-chip.clickable').forEach((c) => {
    c.addEventListener('click', () => {
      showTab('trace');
      loadTrace(c.dataset.trace);
    });
  });
  scrollChat();
}

function renderRecommendation(data) {
  if (!data.recommendation) return '';
  return `<div class="rec-cards">${data.recommendation.items.map(it=>`<div class="rec-card">
    <div class="rc-head"><span class="rc-name">${esc(it.name)}</span><span class="rc-cat">${esc(it.category)} · R${it.risk_level}</span></div>
    <div class="rc-line">${esc(it.scope)}</div><div class="rc-line">${esc(it.fee_rate)}</div>
    <div class="rc-line">${esc(it.highlights.join('；'))}</div>
    <div class="rc-line">比较依据：${esc(it.reason)}</div>
    <details><summary>查看费用计算示例</summary><div class="rc-line">申购金额 ${esc(it.fee_quote.amount)} 元 · 前端费用 ${esc(it.fee_quote.fee)} 元 · 净申购金额 ${esc(it.fee_quote.net_amount)} 元</div><p class="muted">${esc(it.fee_quote.note)}</p></details>
  </div>`).join('')}<div class="muted">${esc(data.recommendation.basis)}<br>${esc(data.recommendation.note)}</div></div>`;
}
function renderSuitability(data) {
  const m=data.suitability;if(!m)return '';
  return `<div class="rec-cards"><div class="rc-line">适当性检查 · ${esc(m.investor_level)} · ${esc(m.status)}</div>
    ${m.missing.length?`<div class="rc-line">待补信息：${esc(m.missing.join('、'))}</div>`:''}
    ${m.rows.map(x=>`<div class="rc-line">${esc(x.name)} · ${esc(x.risk)} · ${x.eligible?'可比较':'暂不展示候选：'+esc(x.reasons.join('、'))}</div>`).join('')}
    <div class="muted">${esc(m.note)}</div></div>`;
}
function renderSources(data) {
  if(!data.sources?.length)return '';
  return `<details class="rec-cards"><summary>查看本轮知识依据（${data.sources.length}条）</summary>${data.sources.map(d=>`<div class="rc-line"><b>${esc(d.title)}</b> · 版本 ${esc(d.version)}<br>${esc(d.text)}<br><span class="muted">${esc(d.source)} · 生效 ${esc(d.effective_from)} · ${esc(d.id)}</span></div>`).join('')}</details>`;
}

function scrollChat() { const b = $('#chatBody'); b.scrollTop = b.scrollHeight; }

/* ---------------- 运行链路 ---------------- */
async function runTurn(payload, userText) {
  if (S.busy) return;
  if (!S.sessionId) { alert('请先在左侧选择一位用户'); return; }
  S.busy = true;
  $('#sendBtn').disabled = true;
  resetGates();
  if (userText) addUserMsg(userText);
  try {
    const data = await api(payload.path, payload.body);
    await animate(data.events);
    addAiMsg(data);
    S.lastTrace = data.trace_id;
    renderMetrics(data.metrics);
    renderReflect(data.reflect);
    renderAlerts(data.metrics.alerts);
    $('#traceIdLabel').textContent = data.trace_id;
    $('#replayBtn').disabled = false;
    renderTraceSteps(data.trace.steps || []);
    await loadTraces();
    await loadMetrics();
  } catch (e) {
    addAiMsgFallback(`链路异常：${e.message}`);
  } finally {
    S.busy = false;
    $('#sendBtn').disabled = false;
  }
}

function addAiMsgFallback(text) {
  const el = document.createElement('div');
  el.className = 'msg ai blocked';
  el.innerHTML = `<div class="avatar">AI</div><div class="bubble">${esc(text)}</div>`;
  $('#chatBody').appendChild(el);
  scrollChat();
}

async function runTrigger(key) {
  const t = TRIGGERS.find((x) => x.key === key);
  await runTurn({
    path: '/api/trigger',
    body: { session_id: S.sessionId, user_id: S.userId, key },
  }, `【行为触发】${t.label}`);
}

async function sendMessage() {
  const input = $('#msgInput');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  await runTurn({
    path: '/api/chat',
    body: { session_id: S.sessionId, user_id: S.userId, message: text, behaviors: [] },
  }, text);
}

async function runAdversarial() {
  const text = $('#advText').value.trim();
  if (!text) return;
  resetGates();
  showTab('pipeline');
  const r = await api('/api/compliance/test', { text });
  const fake = r.gates.map((g) => ({
    layer: 'act', agent: `${g.gate} ${g.name}`, action: '对抗样本校验',
    detail: `${g.status}｜${g.note}`, status: g.status === 'BLOCK' ? 'blocked' : (g.status === 'SANITIZE' ? 'warn' : 'ok'),
    latency_ms: g.latency_ms, meta: { gate: g },
  }));
  clearPipeline();
  await animate(fake, 260);
  $('#planBanner').innerHTML = `<strong>对抗样本压测</strong>：${esc(text)}
    <div class="muted" style="margin-top:3px">判定：${esc(r.summary)}｜风险分 ${r.risk_score}
    ${r.blocked ? '｜整条拦截并转人工' : (r.sanitized ? '｜已摘除/改写违规片段后下发' : '')}
    <br>下发文本：${esc(r.final_text) || (r.blocked ? '整条拦截，无内容下发（已转人工）' : '（已摘除，无可下发内容）')}</div>`;
}

/* ---------------- 指标 / 预警 ---------------- */
async function loadMetrics() { renderMetrics(await api('/api/metrics', null, 'GET')); }

const METRIC_GROUPS = [
  { title: '触达层', okr: '授权触达，支持停止', key: 'touch' },
  { title: '对话层', okr: '信息完整，再推进', key: 'dialogue' },
  { title: '转化层', okr: '北极星：净申购GMV · 待真实埋点', key: 'conversion' },
  { title: '质量层', okr: '证据有效性与误推监测', key: 'quality' },
  { title: '效率层', okr: '迭代速度 / 可观测性', key: 'efficiency' },
];

function renderMetrics(m) {
  if (!m) return;
  const groups = METRIC_GROUPS.map((g) => {
    const rows = Object.entries(m[g.key] || {}).map(([k, v]) => {
      const isNum = typeof v === 'number';
      const barW = isNum ? Math.max(4, Math.min(100, v <= 1 ? v * 100 : v)) : 100;
      const txt = isNum && k.includes('比例') ? pct(v) : v;
      return `<div class="mrow">
        <span class="mname">${esc(k)}</span>
        <span class="mbar"><i style="width:${barW}%"></i></span>
        <span class="mval">${esc(txt)}</span></div>`;
    }).join('');
    return `<div class="metric-group"><h4>${esc(g.title)}<span class="okr">${esc(g.okr)}</span></h4>
      <div class="metric-rows">${rows}</div></div>`;
  }).join('');
  const f = m.funnel;
  const funnel = `<div class="metric-group"><h4>当前演示事件累计<span class="okr">实时累计</span></h4>
    <div class="metric-rows">
      <div class="mrow"><span class="mname">有效执行轮次</span><span class="mbar"><i style="width:100%"></i></span><span class="mval">${f.opened}</span></div>
      <div class="mrow"><span class="mname">用户回应</span><span class="mbar"><i style="width:${Math.min(100, f.engaged / Math.max(1, f.opened) * 100)}%"></i></span><span class="mval">${f.engaged}</span></div>
      <div class="mrow"><span class="mname">净值波动咨询</span><span class="mbar"><i style="width:${Math.min(100, f.objection / Math.max(1, f.opened) * 100)}%"></i></span><span class="mval">${f.objection}</span></div>
      <div class="mrow"><span class="mname">模拟申购确认</span><span class="mbar"><i style="width:${Math.min(100, f.converted / Math.max(1, f.opened) * 100)}%"></i></span><span class="mval">${f.converted}</span></div>
      <div class="mrow"><span class="mname">转人工</span><span class="mbar"><i style="width:${Math.min(100, f.handoff / Math.max(1, f.opened) * 100)}%"></i></span><span class="mval">${f.handoff}</span></div>
      <div class="mrow"><span class="mname">Trace 总数</span><span class="mbar"><i style="width:100%"></i></span><span class="mval">${m.trace_count}</span></div>
    </div></div>`;
  const gs = Object.entries(m.gate_checks || {}).map(([g, c]) =>
    `<div class="mrow"><span class="mname">${g} 拦截</span><span class="mbar"><i style="width:${Math.min(100, (m.gate_stats[g] || 0) / Math.max(1, c) * 100)}%"></i></span>
     <span class="mval">${m.gate_stats[g] || 0}/${c}</span></div>`).join('');
  $('#metrics').innerHTML = funnel + groups +
    `<div class="metric-group"><h4>4 道闸拦截统计<span class="okr">本次运行的真实检查记录</span></h4>
     <div class="metric-rows">${gs}</div></div>`;
}

function renderAlerts(alerts) {
  if (!alerts || !alerts.length) { $('#alertList').innerHTML = '<span class="muted">暂无预警</span>'; return; }
  $('#alertList').innerHTML = alerts.map((a) => `
    <div class="alert ${a.severity}">
      <span class="atime">${esc(a.ts_str)}</span>
      <div class="atitle">${esc(a.level)} · ${esc(a.title)}</div>
      <div class="adetail">${esc(a.detail)}</div>
    </div>`).join('');
}

/* ---------------- 复盘 ---------------- */
function renderReflect(r) {
  if (!r) return;
  const clusters = (r.badcase_clusters || []).length
    ? r.badcase_clusters.map((c) => `<div class="cluster"><span class="cname">${esc(c.cluster)} ×${c.count}</span><span class="csug">${esc(c.suggestion)}</span></div>`).join('')
    : '<div class="muted">本轮无 Badcase 聚类</div>';
  $('#reflect').innerHTML = `
    <div class="sub-head">会话复盘（${r.turns} 轮）</div>
    ${clusters}
    <div class="kv" style="margin-top:7px"><b>反哺</b><span>${esc((r.feed_back_to || []).join(' / '))}</span></div>
    <div class="kv"><b>评估</b><span>可比较候选轮次 ${r.eval.matched_turns}｜模拟确认 ${r.eval.confirmed}｜流失点 ${esc(r.eval.drop_point)}</span></div>`;
}

/* ---------------- Trace ---------------- */
async function loadTraces() {
  const r = await api('/api/traces?limit=24', null, 'GET');
  S.traces = r.items;
  $('#traceList').innerHTML = r.items.map((t) => `
    <div class="trace-row" data-tid="${esc(t.trace_id)}">
      <span class="tid">${esc(t.trace_id.slice(0, 10))}</span>
      <span>${esc(t.created_at_str.slice(11))}</span>
      <span class="muted">${esc(t.model_name)}</span>
      <span class="muted">${t.total_latency_ms}ms</span>
      <span class="tout out-${esc(t.outcome)}">${esc(t.outcome)}</span>
    </div>`).join('');
  $('#traceList').querySelectorAll('.trace-row').forEach((el) => {
    el.addEventListener('click', () => loadTrace(el.dataset.tid));
  });
}

async function loadTrace(tid) {
  const t = await api(`/api/trace/${tid}`, null, 'GET');
  $('#traceIdLabel').textContent = t.trace_id;
  $('#replayBtn').disabled = false;
  $('#replayBtn').dataset.tid = t.trace_id;
  S.lastTrace = t.trace_id;
  renderTraceSteps(t.steps || []);
  $('#traceSub').innerHTML = `${esc(t.created_at_str)}｜Prompt ${esc(t.prompt_version)}｜${esc(t.model_name)}（${esc(t.model_route)}）
    ｜tokens ${t.tokens_in}/${t.tokens_out}｜演示无模型调用成本｜终态 ${esc(t.outcome)}
    ${t.badcase_tags.length ? `<br>Badcase 标签：${esc(t.badcase_tags.join('、'))}` : ''}`;
}

function renderTraceSteps(steps) {
  if (!steps.length) { $('#traceSteps').innerHTML = '<span class="muted">暂无步骤</span>'; return; }
  $('#traceSteps').innerHTML = steps.map((s) => `
    <div class="tstep">
      <span class="tn">${s.seq}</span>
      <span class="tl">${esc(s.layer)}</span>
      <span class="tbody"><b>${esc(s.agent)}</b> · ${esc(s.action)}
        <span class="muted">（${s.latency_ms}ms）</span><br>${esc(s.detail)}</span>
    </div>`).join('');
}

async function replay() {
  const tid = $('#replayBtn').dataset.tid || S.lastTrace;
  if (!tid) return;
  const r = await api(`/api/replay/${tid}`, {});
  $('#traceSub').innerHTML = `<strong style="color:#7ee08a">L3 历史快照</strong>｜${esc(r.note)}
    <br>Prompt 版本：${esc(r.prompt_version)}｜${esc(r.replayed_at)}
    <br>差异提示：${esc(r.diff_hint)}`;
  renderTraceSteps(r.steps || []);
  showTab('trace');
}

/* ---------------- 回归 ---------------- */
async function runRegression() {
  const r = await api('/api/regression', null, 'GET');
  $('#regressionResult').innerHTML = r.cases.map((c) => `
    <div class="reg-line"><span>${esc(c.id)}</span><span style="color:${c.pass ? '#7ee08a' : '#ffaeaa'}">${c.pass ? '通过' : '失败'}</span></div>`).join('') +
    `<div class="verdict ${r.passed === r.total ? 'ok' : 'bad'}">
      回归 ${r.passed}/${r.total} · ${esc(r.verdict)} ｜ ${esc(r.ran_at)}</div>`;
}

/* ---------------- tabs & events ---------------- */
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === name));
}

function bindEvents() {
  $('#tabs').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  $('#promptSeg').querySelectorAll('button').forEach((b) => {
    b.addEventListener('click', async () => {
      $('#promptSeg').querySelectorAll('button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      S.promptVersion = b.dataset.v;
      if (S.userId) await selectUser(S.userId);
    });
  });
  $('#sendBtn').addEventListener('click', sendMessage);
  $('#msgInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $('#advBtn').addEventListener('click', runAdversarial);
  $('#replayBtn').addEventListener('click', replay);
  $('#regressionBtn').addEventListener('click', runRegression);
}

init().catch((e) => {
  document.body.insertAdjacentHTML('afterbegin',
    `<div style="padding:20px;color:#ffaeaa">初始化失败：${esc(e.message)}<br>`
    + `<span style="font-size:12px;color:#9fb0cc">本地模式请确认后端已启动（./run.sh）；`
    + `线上模式请检查浏览器能否访问 CDN（Python 运行时约 15MB）。</span></div>`);
});
