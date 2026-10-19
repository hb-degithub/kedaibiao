/* 课代表 kedaibiao · Web 控制台前端
 *
 * 约束（见 docs/web-console-api.md，本文件唯一的接口契约来源）：
 *   - 零构建、零依赖、零 CDN：只用原生 DOM API 与同源 fetch，页面不外发任何数据。
 *   - 导航：左侧栏一列两段 —— 上段切平台（全部平台 / pigai / pinco / unipus …），
 *           下段切「当前平台的功能」：概览 / 课程 / 作业 / 题库 / 工具 / 运行日志 /
 *           统计 / 账号。顶栏只放项目名与「当前平台 + 干跑/真提交」徽标。
 *           当前页 + 当前平台存在 URL hash（#/功能/平台）里，刷新后仍停在同一处。
 *   - 没有横跨所有平台的能力对比表：当前选哪个平台，就只看那个平台的能力
 *     （概览页的能力摘要、账号页该平台那一行）。跨平台对比只在「统计」页。
 *   - 没有任何行内快捷跑批按钮：跑批一律在概览页的触发区，且必须明确选择平台。
 *   - 课程 / 作业 = 通用只读动作 courses / assignments，会真的登录并访问平台；
 *     界面标明「只读」、给出加载状态、失败显示后端中文 error、成功按 kind 通用渲染。
 *   - 「全部平台」下：课程 / 作业 / 题库 / 工具 必须提示先选平台，不显示空表。
 *   - 任务运行中每 1 秒轮询 /api/jobs/<id>，结束即停；pagehide/切走不留悬挂定时器。
 *   - 所有后端文本一律用 textContent 写入，绝不拼接 HTML；界面不展示任何凭据。
 */
(function () {
  'use strict';

  var POLL_MS = 1000;        // 契约：运行中每 1 秒轮询一次
  var POLL_MAX_FAILS = 5;    // 连续失败达到该次数后停止轮询并报错
  var LIST_LIMIT = 50;       // 契约：limit 钳制到 1..500，默认 runs/submissions = 50
  var EVENT_LIMIT = 100;     // 契约：events 默认 100
  var TREND_DAYS = 14;       // 「最近趋势」最多展示多少天

  /* 功能页（顺序即左侧栏下段的顺序） */
  var PAGES = ['overview', 'courses', 'assignments', 'bank', 'tools', 'logs', 'stats', 'account'];
  var PAGE_LABEL = {
    overview: '概览', courses: '课程', assignments: '作业', bank: '题库',
    tools: '工具', logs: '运行日志', stats: '统计', account: '账号'
  };
  /* 这些功能必须有具体平台才有意义；「全部平台」下给提示而不是空表。 */
  var NEEDS_PLATFORM = { courses: 1, assignments: 1, bank: 1, tools: 1 };
  /* 旧 hash 名（上一版导航）→ 新名字，老书签不失效。 */
  var PAGE_ALIAS = { records: 'logs', settings: 'account' };

  var LIVE_CONFIRM =
    '这会真的提交作业，不可撤销。\n\n' +
    '确定要把「真提交（live）」打开吗？\n' +
    '建议先用干跑确认规划结果（哪些作业会被处理），再决定是否真提交。';

  /* 平台能力键的展示名；未知键原样显示，新平台不用改前端。 */
  var CAP_ORDER = ['login', 'list_courses', 'list_assignments',
                   'fetch_questions', 'submit', 'course_playback'];
  var CAP_LABEL = {
    login: '登录', list_courses: '课程', list_assignments: '作业',
    fetch_questions: '取题', submit: '提交', course_playback: '回放'
  };

  /* 可选依赖的用途说明（未知依赖原样列出）。 */
  var DEP_LABEL = {
    playwright: '浏览器自动化：需要它的平台才能登录 / 抓取',
    fastapi: '可选 Web 框架：备用外壳，当前未启用',
    uvicorn: '可选 ASGI 服务器：备用外壳，当前未启用'
  };

  /* 课程 / 作业两个任务页共用一套「通用动作页」实现。 */
  var ACTION_PAGES = {
    courses: { action: 'courses', label: '课程', dataName: '课程 / 班级' },
    assignments: { action: 'assignments', label: '作业', dataName: '作业 / 任务' }
  };

  var state = {
    page: 'overview',
    platform: null,          // null = 全部平台
    platforms: [],
    platformsLoaded: false,
    overview: null,
    bank: null,
    statistics: null,
    settings: null,
    runs: [],                // 当前作用域的跑批历史（运行日志页）
    statRuns: [],            // 全平台跑批历史（统计页的趋势）
    submissions: [],
    jobs: [],
    currentId: null,         // 正在跟踪的 job_id
    currentJob: null,
    pollTimer: null,
    pollFails: 0,
    eventsOff: false,
    submitting: false,
    runOptionsSig: '',       // 跑批下拉的选项指纹（变了才重建，避免打断用户选择）
    actions: { platform: null, list: null, active: null, result: null, busy: false },
    actionPages: {
      courses: { busy: false, loaded: false, error: '', result: null, params: {}, seq: 0 },
      assignments: { busy: false, loaded: false, error: '', result: null, params: {}, seq: 0 }
    },
    problems: {}             // key -> { message, level }
  };

  /* ------------------------------------------------------------------ 基础工具 */

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function isObj(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }

  function has(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  function dash(v) {
    return (v === null || v === undefined || v === '') ? '—' : String(v);
  }

  function setText(id, value) {
    var node = $(id);
    if (node) { node.textContent = dash(value); }
  }

  function clear(node) { if (node) { node.textContent = ''; } }

  function fill(node, children) {
    if (!node) { return; }
    node.textContent = '';
    for (var i = 0; i < children.length; i++) { node.appendChild(children[i]); }
  }

  function show(node, visible) { if (node) { node.hidden = !visible; } }

  function cell(text, cls) {
    return el('td', cls || '', dash(text));
  }

  function row(cells) {
    var r = el('tr');
    for (var i = 0; i < cells.length; i++) { r.appendChild(cells[i]); }
    return r;
  }

  /* 统一填充表格：无数据时隐藏整张表并显示空状态，绝不渲染空表。 */
  function fillTable(tableId, bodyId, emptyId, rows, emptyText) {
    var table = $(tableId), body = $(bodyId), empty = $(emptyId);
    clear(body);
    if (!rows || !rows.length) {
      if (table) { table.hidden = true; }
      if (empty) {
        if (emptyText) { empty.textContent = emptyText; }
        empty.hidden = false;
      }
      return;
    }
    for (var i = 0; i < rows.length; i++) { body.appendChild(rows[i]); }
    if (table) { table.hidden = false; }
    if (empty) { empty.hidden = true; }
  }

  function badge(text, cls) {
    return el('span', 'badge ' + (cls || 'badge-idle'), text);
  }

  function tdBadge(text, cls) {
    var c = el('td');
    c.appendChild(badge(text, cls));
    return c;
  }

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function fmtNum(v) {
    if (v === null || v === undefined || v === '') { return '—'; }
    return String(v);
  }

  function fmtScore(v) {
    if (v === null || v === undefined || v === '') { return '—'; }
    if (!isNum(v)) { return String(v); }
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
  }

  function fmtPct(v) {
    if (!isNum(v)) { return '—'; }
    return (v * 100).toFixed(1) + '%';
  }

  function fmtBool(v) {
    if (v === true) { return '是'; }
    if (v === false) { return '否'; }
    return '—';
  }

  /* 契约时间格式为无时区的 ISO 8601（2026-10-19T11:20:00），只做本地展示替换。 */
  function fmtTime(v) {
    if (!v) { return '—'; }
    var s = String(v).replace('T', ' ');
    if (s.charAt(s.length - 1) === 'Z') { s = s.slice(0, -1); }
    return s;
  }

  function dayOf(v) {
    if (!v) { return ''; }
    return String(v).slice(0, 10);
  }

  function parseTime(v) {
    if (!v) { return null; }
    var t = Date.parse(String(v));
    return isFinite(t) ? t : null;
  }

  function fmtElapsed(job) {
    if (!job || !job.started_at) { return '—'; }
    var from = parseTime(job.started_at);
    if (from === null) { return '—'; }
    var to = job.finished_at ? parseTime(job.finished_at) : Date.now();
    if (to === null) { to = Date.now(); }
    var sec = Math.max(0, Math.round((to - from) / 1000));
    if (sec < 60) { return sec + 's'; }
    var m = Math.floor(sec / 60);
    return m + 'm' + String(sec % 60).padStart(2, '0') + 's';
  }

  function truncate(s, n) {
    s = (s === null || s === undefined) ? '' : String(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function jsonText(v, max) {
    var raw = '';
    if (v === null || v === undefined) { return ''; }
    if (typeof v === 'object') {
      try { raw = JSON.stringify(v); } catch (e) { raw = String(v); }
    } else {
      raw = String(v);
    }
    return truncate(raw, max || 240);
  }

  /* 等宽用于 ID 与数字：纯数字/编号样式的值自动用等宽。 */
  function monoish(v) {
    var s = String(v === null || v === undefined ? '' : v);
    return s.length > 0 && s.length <= 40 && /^[0-9][0-9\s.,:%/+\-()]*$/.test(s);
  }

  /* 契约：job.status 只有 running/succeeded/failed；
     run.status 另有 pending/skipped —— 两套枚举各自上色，不做互相映射。 */
  var JOB_STATUS = {
    running:   { text: '运行中', cls: 'badge-run' },
    succeeded: { text: '成功',   cls: 'badge-ok' },
    failed:    { text: '失败',   cls: 'badge-fail' },
    pending:   { text: '待处理', cls: 'badge-run' },
    skipped:   { text: '跳过',   cls: 'badge-idle' }
  };

  function statusSpec(status) {
    return JOB_STATUS[status] || { text: status || '未知', cls: 'badge-idle' };
  }

  function liveBadge(live) {
    return live ? badge('真提交', 'badge-live') : badge('干跑', 'badge-dry');
  }

  function tdLive(live) {
    var c = el('td');
    c.appendChild(liveBadge(live));
    return c;
  }

  /* ------------------------------------------------------------------ 请求层 */

  function HttpError(kind, message, status, path, detail) {
    this.name = 'HttpError';
    this.kind = kind;
    this.error = message;
    this.status = status;
    this.path = path;
    this.detail = detail || '';
  }

  function api(path, options) {
    var opts = options || {};
    opts.cache = 'no-store';
    return fetch(path, opts).then(function (res) {
      var ct = res.headers.get('content-type') || '';
      var bodyPromise = ct.indexOf('json') >= 0
        ? res.json().catch(function () { return null; })
        : Promise.resolve(null);
      return bodyPromise.then(function (body) {
        /* 契约：成功判定用 2xx，不要写死 200。 */
        if (res.status < 200 || res.status >= 300) {
          var kind = (body && body.kind) || ('http_' + res.status);
          var msg = (body && body.error) || '';
          var detail = (body && body.detail) || '';
          throw new HttpError(kind, msg, res.status, path, detail);
        }
        return body;
      });
    }, function (err) {
      throw new HttpError('network', String((err && err.message) || err), 0, path);
    });
  }

  function postJson(path, payload) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
  }

  /* 契约：失败响应的 error 字段就是可直接展示的中文，前端不重写它；
     只在后面补一句「接下来能做什么」。认不出的 kind 就原样显示 error。 */
  var ERROR_HINTS = {
    no_credentials: '请在 .env 里补全该平台的账号配置（error 里写了期望的键名），重启服务后再试。',
    unknown_platform: '平台没注册或名字不对。点「刷新」重读平台列表。',
    unknown_action: '该平台没有声明这个只读动作。「刷新」会重读动作清单。',
    busy: '同一时刻只允许一个跑批任务，请等当前任务结束后再触发。',
    bad_request: '请检查请求参数（platform / live / limit / kinds 或动作参数）。',
    forbidden: 'Host 或 Origin 被安全策略拒绝：请通过 http://127.0.0.1:<端口> 访问本页，不要用别的域名或 file:// 打开。',
    not_found: '目标不存在或已被清理。',
    platform_error: '平台侧失败（未登录 / 找不到内容 / 网络异常）。先确认凭据与参数，再重试。',
    internal: '服务端自身异常，请查看服务端日志。'
  };

  function describeError(e, ctx) {
    var kind = e && e.kind;
    if (kind === 'network') {
      return '无法连接本地控制台服务（' + ((e && e.error) || '请求失败') +
             '）。请确认服务已启动，并通过 http://127.0.0.1:<端口> 访问本页（file:// 直开读不到接口）。';
    }
    var base = ((e && e.error) || '').trim() ||
               ('请求失败' + (e && e.status ? '（HTTP ' + e.status + '）' : ''));
    var platform = (ctx && ctx.platform) || '';
    if (platform && (kind === 'no_credentials' || kind === 'unknown_platform') &&
        base.indexOf(platform) === -1) {
      base = '平台「' + platform + '」：' + base;
    }
    /* platform_error 另带 detail（平台异常类名），排错时有用。 */
    if (kind === 'platform_error' && e && e.detail && base.indexOf(e.detail) === -1) {
      base += '（平台异常：' + e.detail + '）';
    }
    var hint = ERROR_HINTS[kind];
    if (hint && base.indexOf(hint) === -1) { base += '（' + hint + '）'; }
    return base;
  }

  /* ------------------------------------------------------------------ 顶部提示 */

  function setProblem(key, message, level) {
    state.problems[key] = { message: message, level: level || 'error' };
    renderProblems();
  }

  function clearProblem(key) {
    if (state.problems[key]) {
      delete state.problems[key];
      renderProblems();
    }
  }

  function clearProblemsWithPrefix(prefix) {
    var keys = Object.keys(state.problems);
    var hit = false;
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf(prefix) === 0) {
        delete state.problems[keys[i]];
        hit = true;
      }
    }
    if (hit) { renderProblems(); }
  }

  function renderProblems() {
    var banner = $('banner');
    var keys = Object.keys(state.problems);
    if (!keys.length) {
      banner.hidden = true;
      banner.textContent = '';
      return;
    }
    var worst = 'warn';
    for (var i = 0; i < keys.length; i++) {
      if (state.problems[keys[i]].level === 'error') { worst = 'error'; break; }
    }
    clear(banner);
    banner.className = 'banner banner-' + worst;
    banner.appendChild(el('div', 'banner-title', worst === 'error' ? '出错了' : '注意'));
    var ul = el('ul');
    for (var j = 0; j < keys.length; j++) {
      ul.appendChild(el('li', '', state.problems[keys[j]].message));
    }
    banner.appendChild(ul);
    banner.hidden = false;
  }

  var toastTimer = null;
  function showToast(message) {
    var toast = $('toast');
    toast.textContent = message;
    toast.hidden = false;
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { toast.hidden = true; }, 6000);
  }

  function setConn(ok, text) {
    var node = $('conn-state');
    node.className = 'conn ' + (ok ? 'conn-ok' : 'conn-err');
    node.textContent = text || (ok ? '已连接' : '未连接');
  }

  /* 页内错误框：给「正在真的访问平台」的页面用（课程 / 作业 / 题库）。 */
  function setBoxError(id, message) {
    var box = $(id);
    if (!box) { return; }
    if (!message) { box.hidden = true; box.textContent = ''; return; }
    box.hidden = false;
    box.textContent = message;
  }

  /* ------------------------------------------------------------------ 平台与能力 */

  function platformByName(name) {
    if (!name) { return null; }
    for (var i = 0; i < state.platforms.length; i++) {
      if (state.platforms[i].name === name) { return state.platforms[i]; }
    }
    return null;
  }

  function platformLabel(name) {
    var p = platformByName(name);
    if (p) { return p.display_name || p.name; }
    return name || '全部平台';
  }

  function canSubmit(platform) {
    return !!(platform && platform.capabilities && platform.capabilities.submit === true);
  }

  /* 能力键：已知的按固定顺序，未知的按键名排序接在后面（新平台不用改前端）。 */
  function capKeys(caps) {
    var keys = [], seen = {}, i, k;
    for (i = 0; i < CAP_ORDER.length; i++) {
      k = CAP_ORDER[i];
      if (has(caps, k)) { keys.push(k); seen[k] = true; }
    }
    var extra = [];
    for (k in caps) {
      if (has(caps, k) && !seen[k]) { extra.push(k); }
    }
    extra.sort();
    return keys.concat(extra);
  }

  function capChip(key, on) {
    var chip = el('span', 'cap-chip ' + (on ? 'is-on' : 'is-off'));
    chip.appendChild(el('span', 'cap-chip-label', CAP_LABEL[key] || key));
    chip.appendChild(el('span', 'cap-chip-mark mono', on ? '✓' : '—'));
    chip.title = key + ' = ' + (on ? 'true' : 'false');
    return chip;
  }

  /* 一个平台自己的能力摘要（不跨平台铺矩阵）。 */
  function capSummary(caps, cls) {
    var box = el('span', 'caps' + (cls ? ' ' + cls : ''));
    var keys = capKeys(caps || {});
    if (!keys.length) {
      box.appendChild(el('span', 'hint', '未声明能力'));
      return box;
    }
    for (var i = 0; i < keys.length; i++) {
      box.appendChild(capChip(keys[i], caps[keys[i]] === true));
    }
    return box;
  }

  /* 契约：只在这些接口上带 ?platform=。 */
  var SCOPED = { '/api/overview': 1, '/api/runs': 1, '/api/submissions': 1, '/api/bank': 1 };

  function scopedGet(path, params) {
    var plt = state.platform;
    var qs = [];
    if (params) {
      Object.keys(params).forEach(function (k) {
        if (params[k] !== undefined && params[k] !== null) {
          qs.push(k + '=' + encodeURIComponent(params[k]));
        }
      });
    }
    if (SCOPED[path] && plt) { qs.push('platform=' + encodeURIComponent(plt)); }
    var url = path + (qs.length ? '?' + qs.join('&') : '');
    return api(url).then(function (body) {
      return { platform: plt, body: body, url: url };
    });
  }

  /* 响应到达时平台是否还是同一个（切平台很快时丢弃过期响应）。 */
  function stillCurrent(res) { return !!res && res.platform === state.platform; }

  /* 契约：响应回带 platform 用于确认作用域；不带该字段的响应不做校验。 */
  function scopeOk(body, plt) {
    if (!isObj(body) || !('platform' in body)) { return true; }
    var got = body.platform === undefined ? null : body.platform;
    return (got === null ? null : String(got)) === (plt === null ? null : String(plt));
  }

  function adoptScope(res, path) {
    /* 每个接口一个问题键：并发请求之间不要互相清掉告警。 */
    var key = 'scope:' + path;
    if (!stillCurrent(res)) { return false; }
    if (!scopeOk(res.body, res.platform)) {
      setProblem(key, path + ' 返回的作用域（platform=' + dash(res.body.platform) +
                 '）与当前平台（' + dash(res.platform) + '）不一致，已忽略这次响应。', 'warn');
      return false;
    }
    clearProblem(key);
    return true;
  }

  /* ------------------------------------------------------------------ 路由 */

  function parseHash() {
    var h = String(location.hash || '').replace(/^#\/?/, '');
    var parts = h.split('/');
    var page = parts[0] || 'overview';
    if (PAGE_ALIAS[page]) { page = PAGE_ALIAS[page]; }
    if (PAGES.indexOf(page) === -1) { page = 'overview'; }
    var plt = '';
    if (parts.length > 1) {
      try { plt = decodeURIComponent(parts[1]); } catch (e) { plt = parts[1]; }
    }
    return { page: page, platform: plt || null };
  }

  function hashFor(page, platform) {
    return '#/' + (PAGES.indexOf(page) === -1 ? 'overview' : page) +
           (platform ? '/' + encodeURIComponent(platform) : '');
  }

  function replaceHash(page, platform) {
    var target = hashFor(page, platform);
    if (location.hash === target) { return; }
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, '', target);
    }
    /* 极端情况（无 history API）下不改写 URL，只保证状态正确。 */
  }

  function navigate(page, platform) {
    var target = hashFor(page, platform);
    if (location.hash === target) {
      applyRoute();
    } else {
      location.hash = target;   // 只有 hash 变化，不触发整页刷新
    }
  }

  function switchPlatform(name) {
    navigate(state.page, name || null);
  }

  /* 平台名不在清单里（例如手改链接）→ 退回「全部平台」并说明。 */
  function sanitizePlatform(plt) {
    if (!plt || !state.platformsLoaded) { return plt; }
    if (platformByName(plt)) { return plt; }
    setProblem('route', '平台「' + plt + '」不在平台列表里，已切回「全部平台」。', 'warn');
    return null;
  }

  function applyRoute() {
    var r = parseHash();
    var wanted = r.platform;
    r.platform = sanitizePlatform(r.platform);
    if (r.platform !== wanted) { replaceHash(r.page, null); }

    var changedPage = r.page !== state.page;
    var changedPlat = r.platform !== state.platform;
    state.page = r.page;
    state.platform = r.platform;

    if (changedPlat) {
      resetTools();
      resetActionPage('courses');
      resetActionPage('assignments');
      clearProblemsWithPrefix('scope:');
    }
    renderChrome();
    showPage();
    if (changedPage || changedPlat) { loadPageData(); }
  }

  function showPage() {
    for (var i = 0; i < PAGES.length; i++) {
      var sec = $('page-' + PAGES[i]);
      if (sec) { sec.hidden = (PAGES[i] !== state.page); }
    }
  }

  /* ------------------------------------------------------------------ 壳：左侧栏 / 顶栏 */

  function scopeLabel() {
    return state.platform
      ? platformLabel(state.platform) + '（' + state.platform + '）'
      : '全部平台（聚合视图）';
  }

  function renderChrome() {
    renderPlatformNav();
    renderFuncNav();
    renderTopbar();
    renderSideMeta();
    renderScopeHints();
    syncRunPlatformOptions();
    applyRunConstraints();
  }

  /* 上段：平台 */
  function renderPlatformNav() {
    var ul = $('platform-nav');
    var empty = $('platform-nav-empty');
    var items = [{ name: null, label: '全部平台', sub: '聚合视图' }];
    for (var i = 0; i < state.platforms.length; i++) {
      var p = state.platforms[i];
      items.push({ name: p.name, label: p.display_name || p.name, sub: p.name, platform: p });
    }
    var kids = [];
    for (var j = 0; j < items.length; j++) {
      var it = items[j];
      var a = el('a', 'plat-item' + (it.name === state.platform ? ' is-active' : ''));
      a.href = hashFor(state.page, it.name);
      a.setAttribute('data-platform', it.name === null ? '' : it.name);
      a.appendChild(el('span', 'plat-name', it.label));
      a.appendChild(el('span', 'plat-slug mono', it.sub));
      if (it.platform && it.platform.has_credentials === false) {
        a.appendChild(el('span', 'plat-flag', '无凭据'));
      }
      if (it.platform && canSubmit(it.platform)) {
        a.appendChild(el('span', 'plat-flag plat-flag-ok', '可提交'));
      }
      var li = el('li');
      li.appendChild(a);
      kids.push(li);
    }
    fill(ul, kids);
    ul.hidden = false;

    if (!state.platforms.length) {
      ul.hidden = true;
      empty.hidden = false;
      empty.textContent = state.platformsLoaded
        ? '没有任何已注册的平台。请先配置平台适配器，再点「刷新」。'
        : '平台列表还没取到（服务未连接或加载失败），点「刷新」重试。';
    } else {
      empty.hidden = true;
      empty.textContent = '';
    }
  }

  /* 下段：当前平台的功能 */
  function renderFuncNav() {
    var nodes = $('func-nav').querySelectorAll('.nav-item');
    var needsPlat = !state.platform;
    for (var i = 0; i < nodes.length; i++) {
      var page = nodes[i].getAttribute('data-page');
      nodes[i].href = hashFor(page, state.platform);
      var active = page === state.page;
      nodes[i].classList.toggle('is-active', active);
      if (active) {
        nodes[i].setAttribute('aria-current', 'page');
      } else {
        nodes[i].removeAttribute('aria-current');
      }
      var gated = needsPlat && !!NEEDS_PLATFORM[page];
      nodes[i].classList.toggle('needs-platform', gated);
      nodes[i].title = gated
        ? PAGE_LABEL[page] + '需要具体平台：请先在上面的「平台」里选一个'
        : PAGE_LABEL[page];
    }
    var scope = $('func-scope');
    scope.textContent = state.platform
      ? platformLabel(state.platform)
      : '全部平台 · 部分功能需先选平台';
    scope.title = state.platform
      ? '下面的功能都作用于 ' + state.platform
      : '课程 / 作业 / 题库 / 工具 需要先选具体平台';
  }

  function renderTopbar() {
    setText('cur-platform', state.platform ? platformLabel(state.platform) : '全部平台');

    /* 常驻徽标：环境是否干跑（契约 §5.1） */
    var env = $('top-env');
    var o = state.overview || {};
    if (o.dry_run === false) {
      env.className = 'badge badge-live';
      env.textContent = '真提交环境';
      env.title = 'dry_run=false：跑批默认仍是干跑，只有显式 live=true 才提交';
    } else if (o.dry_run === true) {
      env.className = 'badge badge-dry';
      env.textContent = '干跑';
      env.title = 'dry_run=true：默认只规划不提交';
    } else {
      env.className = 'badge badge-idle';
      env.textContent = '—';
      env.title = '尚未取到配置';
    }

    /* 本次跑批是否已武装真提交（live 开关打开且该平台支持提交） */
    var live = $('run-live');
    show($('top-live'), !!(live && live.checked && !live.disabled));
  }

  function renderSideMeta() {
    var o = state.overview || {};
    setText('meta-version', o.version);
    setText('meta-env', o.env);
    setText('meta-python', o.python);
    setText('meta-db', o.db_path);
  }

  /* 每个功能页头部的作用域提示（只依赖当前平台，路由一变就刷新）。 */
  function renderScopeHints() {
    var scope = scopeLabel();
    setText('ov-scope', '作用域：' + scope +
      (state.platform ? '' : ' · 概览 / 运行日志 / 统计 / 账号在聚合视图下仍可用'));

    var platHint = state.platform
      ? ('平台：' + platformLabel(state.platform) + '（' + state.platform + '）')
      : '未选择平台';
    setText('courses-scope', platHint);
    setText('assignments-scope', platHint);
    setText('bank-scope', platHint);
    setText('tools-scope', platHint);
    setText('logs-scope', '作用域：' + scope +
      ' · 跑批历史与提交记录按该作用域过滤，聚合视图下每行显示平台列');
    setText('stats-scope', '跨平台对比：不受当前平台过滤（' + scope + ' 只是你刚才看的位置）');
  }

  /* ------------------------------------------------------------------ 概览页 */

  function renderOverviewCards() {
    var o = state.overview || {};
    var bank = o.bank || {};
    var runs = o.runs || {};
    var p = state.platform ? platformByName(state.platform) : null;
    var scopeName = state.platform ? platformLabel(state.platform) : '全部平台';

    /* 卡片 1：当前平台 */
    setText('ov-plat', p ? (p.display_name || p.name) : scopeName);
    var sub1 = $('ov-plat-sub');
    clear(sub1);
    if (p) {
      sub1.appendChild(el('span', 'mono', p.name + ' '));
      sub1.appendChild(p.has_credentials
        ? badge('凭据已配置', 'badge-ok')
        : badge('凭据未配置', 'badge-warn'));
      sub1.appendChild(el('span', '', ' ' + (canSubmit(p)
        ? '支持真提交'
        : '不支持提交（只能干跑）')));
    } else {
      var creds = o.credentialed_platforms || [];
      sub1.textContent = '共 ' + fmtNum(o.platform_count) + ' 个平台 · ' +
        (creds.length ? ('已配凭据：' + creds.join('、')) : '没有任何平台配置了凭据');
    }

    /* 卡片 2：题库（契约：overview 带 ?platform= 时 bank 就是该平台口径） */
    $('ov-bank-label').textContent = state.platform ? ('题库 · ' + scopeName) : '题库（全部平台）';
    setText('ov-bank', fmtNum(bank.total));
    $('ov-bank-sub').textContent =
      '已判分 ' + fmtNum(bank.graded) + ' · 正确 ' + fmtNum(bank.correct) +
      ' · 准确率 ' + fmtPct(bank.accuracy) + ' · 未判分 ' + fmtNum(bank.ungraded);

    /* 卡片 3：跑批（契约：runs 的计数是全量的，不加「最近」字样） */
    $('ov-runs-label').textContent = state.platform ? ('跑批 · ' + scopeName) : '跑批记录（全部平台）';
    setText('ov-runs', fmtNum(runs.total));
    $('ov-runs-sub').textContent =
      '全量计数：成功 ' + fmtNum(runs.succeeded) + ' · 失败 ' + fmtNum(runs.failed) +
      ' · 跳过 ' + fmtNum(runs.skipped);

    /* 卡片 4：运行参数 */
    setText('ov-runtime', o.max_concurrency === undefined ? null : '并发 ' + fmtNum(o.max_concurrency));
    var deps = o.optional_deps || {};
    $('ov-runtime-sub').textContent =
      'headless ' + fmtBool(o.headless) +
      ' · 最低置信度 ' + fmtNum(o.min_confidence) +
      ' · playwright ' + (deps.playwright ? '可用' : '不可用') +
      ' · fastapi ' + (deps.fastapi ? '可用' : '不可用') +
      ' · uvicorn ' + (deps.uvicorn ? '可用' : '不可用');

    /* 环境明细 */
    var pairs = [
      ['version', o.version],
      ['python', o.python],
      ['repo_root', o.repo_root],
      ['db_path', o.db_path],
      ['db_exists', o.db_exists === undefined ? null : fmtBool(o.db_exists)],
      ['platform_count', o.platform_count],
      ['作用域 platform', state.platform === null ? 'null（全部）' : state.platform]
    ];
    var kids = [];
    for (var i = 0; i < pairs.length; i++) {
      var wrap = el('div');
      wrap.appendChild(el('dt', '', pairs[i][0]));
      wrap.appendChild(el('dd', 'mono', dash(pairs[i][1])));
      kids.push(wrap);
    }
    fill($('env-list'), kids);
    $('env-list').hidden = false;

    renderOverviewCaps(p);
  }

  /* 平台能力：只显示当前平台自己的能力（契约 §5.3）。 */
  function renderOverviewCaps(p) {
    var list = $('ov-cap-list');
    var empty = $('ov-cap-empty');
    var hint = $('ov-cap-hint');
    var note = $('ov-cap-note');
    clear(list);
    if (!p) {
      list.hidden = true;
      note.hidden = true;          // 提示已经说清楚了，不再重复静态说明
      empty.hidden = false;
      empty.textContent = '当前是「全部平台」（聚合视图）。平台能力按平台各自声明，' +
        '请在上面的「平台」里选一个，这里就显示它自己的能力 —— 不做横跨所有平台的对比表。' +
        '跨平台对比只在「统计」页。';
      hint.textContent = '';
      return;
    }
    empty.hidden = true;
    note.hidden = false;
    list.appendChild(capSummary(p.capabilities || {}, 'caps-lg'));
    hint.textContent = (p.display_name || p.name) + ' · ' + p.name +
      ' · 凭据' + (p.has_credentials ? '已配置' : '未配置');
    list.hidden = false;
  }

  function loadOverview() {
    return scopedGet('/api/overview').then(function (res) {
      if (!stillCurrent(res)) { return; }
      if (!scopeOk(res.body, res.platform)) {
        setProblem('scope', '/api/overview 返回的作用域与当前平台不一致，已忽略这次响应。', 'warn');
        return;
      }
      state.overview = isObj(res.body) ? res.body : {};
      renderSideMeta();
      renderOverviewCards();
      renderTopbar();
      clearProblem('overview');
      setConn(true, '已连接');
    }).catch(function (e) {
      if (e && e.kind === 'unknown_platform') {
        setProblem('overview', describeError(e, { platform: state.platform }), 'error');
        handleUnknownScope(e);
        return;
      }
      setConn(false, '未连接');
      setProblem('overview', describeError(e), e.kind === 'network' ? 'error' : 'warn');
    });
  }

  /* 后端说「未注册的平台」：把当前平台退回全部平台，别让界面卡在坏作用域上。 */
  function handleUnknownScope(e) {
    if (!state.platform) { return; }
    var bad = state.platform;
    state.platform = null;
    replaceHash(state.page, null);
    renderChrome();
    setProblem('route', '平台「' + bad + '」后端未注册（404 unknown_platform），已切回「全部平台」。', 'warn');
    loadPlatforms().then(function () { return loadPageData(true); });
  }

  function loadPlatforms() {
    return api('/api/platforms').then(function (body) {
      state.platforms = (body && body.platforms) || [];
      state.platformsLoaded = true;
      renderChrome();
      clearProblem('platforms');
      return state.platforms;
    }).catch(function (e) {
      state.platforms = [];
      state.platformsLoaded = false;
      renderChrome();
      setProblem('platforms', '平台列表加载失败：' + describeError(e), 'error');
    });
  }

  /* ------------------------------------------------------------------ 触发跑批 */

  function showLiveHint(text) {
    var hint = $('run-live-hint');
    hint.hidden = false;
    hint.textContent = text;
  }

  function hideLiveHint() {
    var hint = $('run-live-hint');
    hint.hidden = true;
    hint.textContent = '';
  }

  /* 跑批下拉：选项按平台清单重建；只有在「平台清单或当前平台」变化时才重建，
     免得用户手动选好的目标被无关的重绘改掉。 */
  function runOptionsSignature() {
    var names = [];
    for (var i = 0; i < state.platforms.length; i++) { names.push(state.platforms[i].name); }
    return (state.platform || '') + '|' + names.join(',');
  }

  function syncRunPlatformOptions(force) {
    var sel = $('run-platform');
    if (!sel) { return; }
    var sig = runOptionsSignature();
    if (!force && sig === state.runOptionsSig) { return; }
    state.runOptionsSig = sig;

    var kids = [el('option', '', '— 请选择平台 —')];
    kids[0].value = '';
    for (var i = 0; i < state.platforms.length; i++) {
      var p = state.platforms[i];
      var o = el('option', '', (p.display_name || p.name) + ' · ' + p.name);
      o.value = p.name;
      kids.push(o);
    }
    fill(sel, kids);
    sel.value = state.platform || '';
    applyRunConstraints();
  }

  function selectedRunPlatform() {
    var sel = $('run-platform');
    var name = sel ? String(sel.value || '') : '';
    return name || null;
  }

  /* 契约：capabilities.submit === false 时禁用 live 开关并说明原因。 */
  function applyRunConstraints() {
    var live = $('run-live');
    var name = selectedRunPlatform();
    var p = name ? platformByName(name) : null;

    var submitOk = canSubmit(p);
    live.disabled = !p || !submitOk;
    if (!submitOk && live.checked) { live.checked = false; }

    if (!name) {
      showLiveHint('跑批必须指定平台：在上面的「目标平台」下拉里选一个（或先在左侧选一个平台）。');
    } else if (!p) {
      showLiveHint('平台「' + name + '」不在平台列表里，无法跑批；点「刷新」重读平台列表。');
    } else if (!submitOk) {
      showLiveHint('该平台本阶段不支持提交（capabilities.submit = false），只能干跑：跑批不会向平台提交任何内容。');
    } else if (p.has_credentials === false) {
      showLiveHint('提示：该平台尚未配置凭据，跑批会返回 no_credentials，需要先在 .env 中补全。');
    } else {
      hideLiveHint();
    }
    updateRunButton();
  }

  function updateRunButton() {
    var btn = $('run-submit');
    var live = $('run-live').checked && !$('run-live').disabled;
    btn.classList.toggle('is-live', live);
    btn.textContent = state.submitting
      ? '触发中…'
      : (live ? '开始跑批（真提交 · 不可撤销）' : '开始跑批（干跑）');
    btn.disabled = state.submitting || !selectedRunPlatform();

    var liveBadgeEl = $('live-badge');
    if (live) {
      liveBadgeEl.className = 'badge badge-live';
      liveBadgeEl.textContent = '当前：真提交';
    } else {
      liveBadgeEl.className = 'badge badge-dry';
      liveBadgeEl.textContent = '当前：干跑';
    }
    $('run-state').textContent = state.submitting ? '正在请求 /api/run …' : '';
    renderTopbar();
  }

  function checkedKinds() {
    var nodes = document.querySelectorAll('#run-form input[name="kind"]:checked');
    var out = [];
    for (var i = 0; i < nodes.length; i++) { out.push(nodes[i].value); }
    return out;
  }

  /* 契约字段固定为这四个，不额外发明字段。 */
  function buildRunPayload(platform, live, limit, kinds) {
    return { platform: platform, live: live, limit: limit, kinds: kinds };
  }

  function onRunSubmit(ev) {
    ev.preventDefault();
    if (state.submitting) { return; }

    var platform = selectedRunPlatform();
    if (!platform) {
      setProblem('run', '跑批必须明确指定平台：请在上面的「目标平台」下拉里选一个。', 'error');
      return;
    }
    var live = $('run-live').checked && !$('run-live').disabled;
    if (live && !canSubmit(platformByName(platform))) {
      setProblem('run', '平台「' + platform + '」不支持提交（capabilities.submit = false），无法真提交，只能干跑。', 'error');
      return;
    }
    if (live && !window.confirm('这会真的提交作业，不可撤销。\n\n确认以「真提交」模式跑一遍 ' + platform + '？')) {
      return;
    }

    var limit = parseInt($('run-limit').value, 10);
    if (!isFinite(limit) || limit < 0) { limit = 0; }

    state.submitting = true;
    updateRunButton();
    clearProblem('run');

    postJson('/api/run', buildRunPayload(platform, live, limit, checkedKinds())).then(function (body) {
      var jobId = body && body.job_id;
      showToast('已触发' + (live ? '真提交' : '干跑') + '跑批：' + platform +
                (jobId ? '（' + jobId + '）' : ''));
      if (jobId) {
        trackJob(jobId);
      } else {
        setProblem('run', '服务响应里没有 job_id，无法跟踪任务进度。', 'warn');
      }
      loadJobs();
    }).catch(function (e) {
      setProblem('run', describeError(e, { platform: platform }),
                 e.kind === 'busy' ? 'warn' : 'error');
      if (e.kind === 'busy') {
        /* 忙的时候直接接管正在跑的那个任务，别让界面停在原地。 */
        loadJobs().then(resumeRunningJob);
      }
      if (e.kind === 'unknown_platform') { handleUnknownScope(e); }
    }).then(function () {
      state.submitting = false;
      updateRunButton();
    });
  }

  /* ------------------------------------------------------------------ 任务轮询 */

  function cancelTimer() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function stopPolling() {
    cancelTimer();
    state.currentId = null;
    state.pollFails = 0;
    $('job-poll').textContent = '';
  }

  function trackJob(jobId) {
    cancelTimer();
    state.currentId = jobId;
    state.pollFails = 0;
    pollJob();
  }

  function pollJob() {
    var id = state.currentId;
    if (!id) { return; }
    var url = '/api/jobs/' + encodeURIComponent(id);
    api(url).then(function (body) {
      state.pollFails = 0;
      /* 契约：GET /api/jobs/<id> 返回裸 job 对象（与列表元素同结构）。 */
      var job = isObj(body) ? body : {};
      renderCurrentJob(job);
      clearProblem('job');
      if (job.status === 'running') {
        $('job-poll').textContent = '每 1 秒轮询 ' + url + '（进行中）';
        loadEvents();   // 事件流跟着任务轮询走，任务结束就不再请求
        state.pollTimer = setTimeout(pollJob, POLL_MS);
      } else {
        state.pollTimer = null;
        state.currentId = null;
        state.currentJob = job;
        $('job-poll').textContent = '任务已结束（' + statusSpec(job.status).text + '），停止轮询';
        finishRefresh();
      }
    }).catch(function (e) {
      if (e.kind === 'not_found') {
        setProblem('job', '任务「' + id + '」不存在或已被清理（404），已停止轮询。', 'warn');
        stopPolling();
        return;
      }
      state.pollFails++;
      if (state.pollFails >= POLL_MAX_FAILS) {
        setProblem('job', '轮询任务「' + id + '」连续失败 ' + state.pollFails + ' 次，已停止：' + describeError(e), 'error');
        stopPolling();
      } else {
        setProblem('job', '轮询失败（第 ' + state.pollFails + ' 次，将重试）：' + describeError(e), 'warn');
        state.pollTimer = setTimeout(pollJob, POLL_MS);
      }
    });
  }

  /* 任务结束后刷新当前页数据（只重取，不新增轮询）。 */
  function finishRefresh() {
    loadEvents();
    loadJobs();
    loadOverview();
    if (state.page === 'logs') {
      loadRuns();
      loadSubmissions();
    } else if (state.page === 'stats') {
      loadStatistics();
      loadStatRuns();
    }
  }

  function renderCurrentJob(job) {
    state.currentJob = job;
    $('job-empty').hidden = true;
    $('job-body').hidden = false;

    setText('job-id', job.job_id);
    setText('job-platform', job.platform);
    setText('job-started', fmtTime(job.started_at));
    setText('job-finished', fmtTime(job.finished_at));
    setText('job-elapsed', fmtElapsed(job));

    var liveCell = $('job-live');
    clear(liveCell);
    liveCell.appendChild(liveBadge(job.live === true));

    var spec = statusSpec(job.status);
    var statusCell = $('job-status');
    clear(statusCell);
    statusCell.appendChild(badge(spec.text, spec.cls));

    var summary = $('job-summary');
    if (job.summary) {
      summary.hidden = false;
      summary.textContent = '摘要：' + job.summary;
    } else {
      summary.hidden = true;
      summary.textContent = '';
    }

    var errNode = $('job-error');
    if (job.error) {
      errNode.hidden = false;
      errNode.textContent = '任务错误：' + job.error;
    } else {
      errNode.hidden = true;
      errNode.textContent = '';
    }

    var records = job.records || [];
    var tally = {};
    for (var i = 0; i < records.length; i++) {
      var st = records[i].status || 'unknown';
      tally[st] = (tally[st] || 0) + 1;
    }
    var counts = $('job-counts');
    clear(counts);
    var order = ['succeeded', 'failed', 'skipped', 'running', 'pending'];
    var shown = [];
    for (var k = 0; k < order.length; k++) {
      if (tally[order[k]]) { shown.push(order[k]); }
    }
    for (var key in tally) {
      if (order.indexOf(key) === -1) { shown.push(key); }
    }
    if (!shown.length) {
      counts.appendChild(el('span', 'muted', '暂无记录明细' +
        (job.status === 'running' ? '（任务刚开始或尚未产出记录）' : '')));
    } else {
      for (var m = 0; m < shown.length; m++) {
        var item = el('span', 'stat');
        item.appendChild(el('span', 'stat-label', statusSpec(shown[m]).text));
        item.appendChild(el('span', 'stat-value mono', String(tally[shown[m]])));
        counts.appendChild(item);
      }
    }
    $('job-records-count').textContent = records.length ? ('共 ' + records.length + ' 条') : '';

    var rows = records.map(function (rec) {
      var s = statusSpec(rec.status);
      return row([
        cell(rec.assignment_id, 'mono'),
        tdBadge(s.text, s.cls),
        cell(rec.note, 'wrap')
      ]);
    });
    fillTable('job-records-table', 'job-records-body', 'job-records-empty', rows,
      job.status === 'running'
        ? '任务还在运行，暂时没有产出记录。'
        : '该任务没有产生任何记录。');
  }

  function resumeRunningJob() {
    for (var i = 0; i < state.jobs.length; i++) {
      if (state.jobs[i].status === 'running') {
        trackJob(state.jobs[i].job_id);
        return true;
      }
    }
    return false;
  }

  /* ==================================================================
     通用动作页：课程（courses） / 作业（assignments）
     契约 §3：这两个动作会真的登录并访问平台（只读 GET），
     返回统一的展示信封，按 kind 通用渲染。
     ================================================================== */

  function resetActionPage(key) {
    var ps = state.actionPages[key];
    ps.busy = false;
    ps.loaded = false;
    ps.error = '';
    ps.result = null;
    ps.params = {};
    ps.seq++;
  }

  function renderActionPage(key) {
    var cfg = ACTION_PAGES[key];
    var ps = state.actionPages[key];
    var gate = $(key + '-gate');
    var body = $(key + '-body');
    var hint = $(key + '-hint');

    if (!state.platform) {
      /* 契约 §5.2：「全部平台」时给提示，不要静默显示空表。 */
      if (gate) {
        gate.hidden = false;
        gate.textContent = '请先在左侧「平台」里选择一个平台：' + cfg.label +
          '是某个平台的具体数据（' + cfg.action +
          ' 会真的登录并读取该平台），「全部平台」聚合视图下没有可展示的内容。';
      }
      if (body) { body.hidden = true; }
      setText(key + '-hint', '');
      return;
    }
    if (gate) { gate.hidden = true; gate.textContent = ''; }
    if (body) { body.hidden = false; }

    var label = platformLabel(state.platform);
    if (hint) {
      hint.textContent = label + ' · 动作 ' + cfg.action +
        '（只读；会真的登录平台）' + (ps.loaded && !ps.busy ? ' · 已加载' : '');
    }

    var stateNode = $(key + '-state');
    if (stateNode) {
      stateNode.textContent = ps.busy
        ? ('正在请求 /api/platforms/' + state.platform + '/actions/' + cfg.action +
           ' …（会真的登录平台，只读）')
        : '';
    }

    setBoxError(key + '-error', ps.error);
    renderEnvelope($(key + '-result'), ps.result, {
      emptyText: '该动作没有返回任何' + cfg.dataName + '（0 行）。'
    });

    var reload = $(key + '-reload');
    if (reload) { reload.disabled = !!ps.busy; }
    var submit = $(key + '-submit');
    if (submit) { submit.disabled = !!ps.busy; }
  }

  function loadActionPage(key, params) {
    var cfg = ACTION_PAGES[key];
    var ps = state.actionPages[key];
    var plt = state.platform;

    if (!plt) { renderActionPage(key); return Promise.resolve(); }
    if (params) { ps.params = params; }

    ps.seq++;
    var seq = ps.seq;
    ps.busy = true;
    ps.error = '';
    renderActionPage(key);

    var url = '/api/platforms/' + encodeURIComponent(plt) + '/actions/' + encodeURIComponent(cfg.action);
    return postJson(url, ps.params).then(function (body) {
      if (seq !== ps.seq || plt !== state.platform) { return; }
      ps.busy = false;
      ps.loaded = true;
      ps.result = isObj(body) ? body : null;
      ps.error = '';
      renderActionPage(key);
      showToast(cfg.label + '数据已加载（' + plt + '，只读）');
    }).catch(function (e) {
      if (seq !== ps.seq || plt !== state.platform) { return; }
      ps.busy = false;
      ps.loaded = true;
      ps.result = null;
      var msg = describeError(e, { platform: plt });
      if (e && e.kind === 'unknown_action') {
        msg += '（后端当前没有给「' + plt + '」声明 ' + cfg.action +
               ' 这个通用动作；契约 §3 要求所有平台都提供 courses / assignments。）';
      }
      ps.error = msg;
      renderActionPage(key);
      if (e && e.kind === 'unknown_platform') { handleUnknownScope(e); }
    });
  }

  /* ------------------------------------------------------------------ 题库页 */

  function loadBank() {
    return scopedGet('/api/bank').then(function (res) {
      if (!adoptScope(res, '/api/bank')) { return; }
      state.bank = { platform: res.platform, data: isObj(res.body) ? res.body : {} };
      renderBank();
      clearProblem('bank');
    }).catch(function (e) {
      state.bank = null;
      renderBank();
      setProblem('bank', '题库统计加载失败：' + describeError(e, { platform: state.platform }), 'error');
      if (e && e.kind === 'unknown_platform') { handleUnknownScope(e); }
    });
  }

  function renderBank() {
    var gate = $('bank-gate');
    var body = $('bank-body');
    if (!state.platform) {
      gate.hidden = false;
      gate.textContent = '请先在左侧「平台」里选择一个平台：题库可以按平台统计，' +
        '「全部平台」聚合视图请到「统计」页看全平台题库总览。';
      body.hidden = true;
      setBoxError('bank-error', '');
      return;
    }
    gate.hidden = true;
    gate.textContent = '';
    body.hidden = false;

    var bank = (state.bank && state.bank.data) || {};
    var total = bank.total;
    var p = platformByName(state.platform);
    var label = p ? (p.display_name || p.name) : state.platform;

    $('bank-hint').textContent = label + '（' + state.platform + '）' +
      (isNum(total) ? ' · 共 ' + total + ' 题' : '');
    setBoxError('bank-error', '');

    setText('bank-total', fmtNum(bank.total));
    setText('bank-graded', fmtNum(bank.graded));
    setText('bank-correct', fmtNum(bank.correct));
    setText('bank-accuracy', fmtPct(bank.accuracy));
    setText('bank-ungraded', fmtNum(bank.ungraded));
    setText('bank-dbpath', bank.db_path || (state.overview || {}).db_path);

    var isEmpty = isNum(total) && total === 0;
    $('bank-empty').hidden = !isEmpty;
    $('bank-stats').hidden = isEmpty;
    if (isEmpty) {
      $('bank-empty').textContent = '平台「' + label + '」的题库还是空的：跑批命中题目后会自动入库。';
    }

    var meter = $('bank-acc-meter');
    var fillNode = $('bank-acc-fill');
    if (!isEmpty && isNum(bank.accuracy)) {
      meter.hidden = false;
      fillNode.style.width = (Math.max(0, Math.min(1, bank.accuracy)) * 100).toFixed(1) + '%';
      $('bank-acc-note').textContent =
        '正确 ' + fmtNum(bank.correct) + ' / 已判分 ' + fmtNum(bank.graded) + ' = ' + fmtPct(bank.accuracy);
    } else {
      meter.hidden = true;
      fillNode.style.width = '0%';
      $('bank-acc-note').textContent = '还没有已判分的题目，准确率暂不可计算（—）。';
    }

    renderSourceTable(bank.by_source, 'bank-source-table', 'bank-source-body',
      'bank-source-empty', 'bank-source-hint', label);
  }

  /* 题库构成（按来源）：题库页与统计页共用。 */
  function renderSourceTable(bySource, tableId, bodyId, emptyId, hintId, scopeName) {
    var src = isObj(bySource) ? bySource : {};
    var keys = Object.keys(src);
    var sum = 0;
    for (var i = 0; i < keys.length; i++) {
      if (isNum(src[keys[i]])) { sum += src[keys[i]]; }
    }
    var hint = $(hintId);
    if (hint) {
      hint.textContent = keys.length
        ? (keys.length + ' 个来源 · ' + scopeName)
        : '';
    }
    var rows = keys.map(function (k) {
      var v = src[k];
      var ratio = (sum > 0 && isNum(v)) ? (v / sum) : null;
      return row([
        cell(k, 'mono'),
        cell(fmtNum(v), 'num mono'),
        cell(ratio === null ? '—' : fmtPct(ratio), 'num mono'),
        barCell([{ cls: 'seg-ok', value: ratio === null ? 0 : ratio }])
      ]);
    });
    fillTable(tableId, bodyId, emptyId, rows,
      '没有来源统计（题库里还没有题目）。');
  }

  /* 一行里的比例条：宽度用百分比，纯 CSS 元素，不用 innerHTML。 */
  function barCell(segs) {
    var c = el('td');
    var track = el('div', 'meter-bar');
    for (var i = 0; i < segs.length; i++) {
      var w = Math.max(0, Math.min(1, segs[i].value || 0)) * 100;
      var seg = el('span', 'meter-fill ' + segs[i].cls);
      seg.style.width = w.toFixed(1) + '%';
      seg.title = segs[i].title || '';
      track.appendChild(seg);
    }
    c.appendChild(track);
    return c;
  }

  /* ------------------------------------------------------------------ 工具页（平台只读动作通用渲染） */

  function resetTools() {
    state.actions = { platform: null, list: null, active: null, result: null, busy: false };
    clearToolsError();
    renderTools();
  }

  function clearToolsError() {
    var box = $('action-error');
    if (!box) { return; }
    box.hidden = true;
    box.textContent = '';
  }

  function setToolsError(message) {
    var box = $('action-error');
    box.hidden = false;
    box.textContent = message;
  }

  function toolsNote(text, asEmpty) {
    var n = $('actions-note');
    if (!text) {
      n.hidden = true;
      n.textContent = '';
      return;
    }
    n.hidden = false;
    n.className = (asEmpty === false) ? 'hint' : 'empty';
    n.textContent = text;
  }

  function renderTools() {
    var gate = $('tools-gate');
    var body = $('tools-body');
    if (!state.platform) {
      /* 契约 §5.2：「全部平台」时给提示，不要静默显示空表。 */
      gate.hidden = false;
      gate.textContent = '请先在左侧「平台」里选择一个平台：只读动作清单按平台提供' +
        '（例如批改网有「待办作业 / 历史作文 / 作文详情 / 按作业号查询」），' +
        '「全部平台」下没有统一的动作清单。';
      body.hidden = true;
      return;
    }
    gate.hidden = true;
    gate.textContent = '';
    body.hidden = false;

    var plt = state.platform;
    var list = (state.actions && state.actions.platform === plt) ? state.actions.list : null;
    if (list !== null) {
      /* 契约 §3：builtin 动作（courses / assignments）有专属功能页，
         工具页过滤掉，免得同一个能力在两处出现。 */
      list = list.filter(function (a) { return !a.builtin; });
    }
    var nav = $('actions-nav');
    var form = $('action-form');
    var result = $('action-result');

    if (list === null) {
      toolsNote('正在读取平台「' + plt + '」的动作清单…', false);
      nav.hidden = true;
      clear(nav);
      form.hidden = true;
      clear(form);
      result.hidden = true;
      clear(result);
      return;
    }
    if (!list.length) {
      toolsNote('平台「' + plt + '」没有声明任何只读动作。', true);
      nav.hidden = true;
      clear(nav);
      form.hidden = true;
      clear(form);
      result.hidden = true;
      clear(result);
      return;
    }

    if (!state.actions.active) {
      toolsNote('选择一个动作：带参数的动作会生成表单，结果按响应 kind（table / detail / message）通用渲染。', false);
    } else {
      toolsNote('');
    }

    var kids = [];
    for (var i = 0; i < list.length; i++) {
      var act = list[i];
      var btn = el('button', 'act-btn' + (state.actions.active && state.actions.active.name === act.name ? ' is-active' : ''), null);
      btn.type = 'button';
      btn.setAttribute('data-action', act.name);
      btn.appendChild(el('span', 'act-label', act.label || act.name));
      btn.appendChild(el('span', 'act-name mono', act.name));
      if ((act.params || []).length) {
        btn.appendChild(el('span', 'act-flag', '需参数'));
      }
      btn.title = act.description || act.label || act.name;
      kids.push(btn);
    }
    fill(nav, kids);
    nav.hidden = false;

    renderActionForm();
    renderEnvelope(result, state.actions.result, {});
  }

  function loadActions(keep) {
    var plt = state.platform;
    if (!plt) {
      state.actions = { platform: null, list: null, active: null, result: null, busy: false };
      clearToolsError();
      renderTools();
      return Promise.resolve();
    }
    var url = '/api/platforms/' + encodeURIComponent(plt) + '/actions';
    return api(url).then(function (body) {
      if (plt !== state.platform) { return; }
      var list = (body && body.actions) || [];
      var active = null;
      if (keep && state.actions.active) {
        /* 清单过期自愈时：选中的动作若还在就保留，否则清空。 */
        for (var i = 0; i < list.length; i++) {
          if (list[i].name === state.actions.active.name) { active = list[i]; }
        }
      }
      state.actions = {
        platform: plt, list: list, active: active,
        result: keep ? state.actions.result : null, busy: false
      };
      if (!keep) { clearToolsError(); }
      renderTools();
      clearProblem('actions');
    }).catch(function (e) {
      if (plt !== state.platform) { return; }
      state.actions = { platform: plt, list: [], active: null, result: null, busy: false };
      renderTools();
      setProblem('actions', '平台「' + plt + '」的只读动作清单加载失败：' +
        describeError(e, { platform: plt }), e.kind === 'unknown_platform' ? 'warn' : 'error');
    });
  }

  function renderActionForm() {
    var form = $('action-form');
    clear(form);
    var act = state.actions.active;
    if (!act) {
      form.hidden = true;
      return;
    }
    form.hidden = false;

    var head = el('div', 'action-head');
    head.appendChild(el('span', 'action-title', act.label || act.name));
    head.appendChild(el('span', 'action-name mono', act.name));
    head.appendChild(badge('只读', 'badge-ro'));
    if (act.description) { head.appendChild(el('span', 'hint', act.description)); }
    form.appendChild(head);

    var params = act.params || [];
    if (params.length) {
      var grid = el('div', 'action-params');
      for (var i = 0; i < params.length; i++) {
        var spec = params[i] || {};
        var wrap = el('label', 'field');
        wrap.setAttribute('for', 'ap-' + spec.name);
        wrap.appendChild(el('span', 'field-label',
          (spec.label || spec.name) + (spec.required ? ' *' : '')));
        var input = el('input', 'mono');
        input.id = 'ap-' + spec.name;
        input.name = spec.name;
        input.autocomplete = 'off';
        if (spec.type === 'int') {
          input.type = 'number';
          input.step = '1';
          input.inputMode = 'numeric';
        } else {
          input.type = 'text';
        }
        if (spec.required) { input.required = true; }
        wrap.appendChild(input);
        var hintText = spec.name + ' · ' + (spec.type || 'string') +
          (spec.required ? ' · 必填' : ' · 可选');
        if (spec.hint) { hintText += ' · ' + spec.hint; }
        wrap.appendChild(el('span', 'field-hint', hintText));
        grid.appendChild(wrap);
      }
      form.appendChild(grid);
    } else {
      form.appendChild(el('p', 'field-hint', '该动作不需要参数，直接执行即可（只读，不会改动平台数据）。'));
    }

    var rowNode = el('div', 'action-submit');
    var btn = el('button', 'btn btn-run', state.actions.busy ? '执行中…' : '执行');
    btn.type = 'submit';
    btn.id = 'action-submit';
    btn.disabled = !!state.actions.busy;
    rowNode.appendChild(btn);
    var st = el('span', 'field-hint', '');
    st.id = 'action-state';
    rowNode.appendChild(st);
    form.appendChild(rowNode);
  }

  function onActionSubmit(ev) {
    ev.preventDefault();
    var act = state.actions.active;
    var plt = state.platform;
    if (!act || !plt || state.actions.busy) { return; }

    var payload = {};
    var params = act.params || [];
    for (var i = 0; i < params.length; i++) {
      var spec = params[i] || {};
      var input = $('ap-' + spec.name);
      var raw = input ? String(input.value).trim() : '';
      var label = spec.label || spec.name;
      if (!raw) {
        if (spec.required) {
          setToolsError('「' + label + '」是必填参数，请先填写。');
          if (input) { input.focus(); }
          return;
        }
        continue;
      }
      if (spec.type === 'int') {
        if (!/^-?\d+$/.test(raw)) {
          setToolsError('「' + label + '」需要整数，收到「' + raw + '」。');
          if (input) { input.focus(); }
          return;
        }
        payload[spec.name] = parseInt(raw, 10);   // 契约：int 发 JSON number
      } else {
        payload[spec.name] = raw;
      }
    }

    var url = '/api/platforms/' + encodeURIComponent(plt) + '/actions/' + encodeURIComponent(act.name);
    state.actions.busy = true;
    clearToolsError();
    renderActionForm();

    postJson(url, payload).then(function (body) {
      if (plt !== state.platform) { return; }
      state.actions.result = isObj(body) ? body : null;
      clearToolsError();
      renderEnvelope($('action-result'), state.actions.result, {});
      showToast('动作「' + (act.label || act.name) + '」执行完成（只读）');
    }).catch(function (e) {
      if (plt !== state.platform) { return; }
      setToolsError(describeError(e, { platform: plt }));
      if (e && e.kind === 'unknown_action') {
        /* 清单可能过期：重取一次（保留报错与选中项），界面能自愈。 */
        loadActions(true);
      }
      if (e && e.kind === 'unknown_platform') { handleUnknownScope(e); }
    }).then(function () {
      state.actions.busy = false;
      renderActionForm();
    });
  }

  /* ---- 展示信封（kind: table / detail / message）通用渲染 ----
     课程 / 作业 / 工具 三个页面共用；容器为空时隐藏容器。 */
  function renderEnvelope(box, env, opts) {
    if (!box) { return; }
    clear(box);
    var o = opts || {};
    if (!env) {
      box.hidden = true;
      return;
    }
    box.hidden = false;

    var head = el('div', 'res-head');
    head.appendChild(el('div', 'res-title',
      env.title || env.label || env.action || '结果'));
    var meta = el('div', 'res-meta');
    meta.appendChild(el('span', 'hint', '平台 '));
    meta.appendChild(el('span', 'mono', dash(env.platform)));
    if (env.action) {
      meta.appendChild(el('span', 'hint', ' · 动作 '));
      meta.appendChild(el('span', 'mono', env.action));
    }
    var kindCls = env.kind === 'detail' ? 'badge-ok'
      : (env.kind === 'table' ? 'badge-run' : (env.kind === 'message' ? 'badge-idle' : 'badge-warn'));
    meta.appendChild(el('span', 'hint', ' · '));
    meta.appendChild(badge('kind: ' + (env.kind || '未声明'), kindCls));
    meta.appendChild(badge('只读', 'badge-ro'));
    head.appendChild(meta);
    if (env.note) { head.appendChild(el('p', 'res-note', env.note)); }
    box.appendChild(head);

    var kind = env.kind;
    if (kind === 'table') {
      box.appendChild(tableBlock(env.columns, env.rows, o.emptyText || '该动作返回 0 行。'));
      return;
    }
    if (kind === 'detail') {
      detailBlock(box, env);
      return;
    }
    if (kind === 'message') {
      box.appendChild(el('p', 'res-message', env.message || env.note || '（空消息）'));
      return;
    }
    /* 没声明 kind（或声明了未知 kind）：按结构猜，再不行原样展示，绝不白屏。 */
    if (env.columns || env.rows) {
      box.appendChild(tableBlock(env.columns, env.rows, o.emptyText || '该动作返回 0 行。'));
      return;
    }
    if (env.fields || env.sections) {
      detailBlock(box, env);
      return;
    }
    if (env.message) {
      box.appendChild(el('p', 'res-message', env.message));
      return;
    }
    box.appendChild(el('p', 'empty',
      '这个响应没有可渲染的结构（kind=' + dash(env.kind) + '）。原始响应对照：'));
    var d = el('details');
    d.appendChild(el('summary', '', '展开原始 JSON'));
    d.appendChild(el('pre', 'text-block mono', jsonText(env, 4000) || '（空）'));
    box.appendChild(d);
  }

  function tableBlock(columns, rows, emptyText) {
    var cols = (columns || []).filter(isObj);
    var list = (rows || []).filter(isObj);
    var wrap = el('div', 'table-wrap');
    if (!cols.length) {
      wrap.appendChild(el('p', 'empty', '响应没有给出 columns，无法渲染表格。'));
      return wrap;
    }
    if (!list.length) {
      wrap.appendChild(el('p', 'empty', emptyText || '没有数据。'));
      return wrap;
    }
    var table = el('table');
    var thead = el('thead');
    var headRow = el('tr');
    for (var i = 0; i < cols.length; i++) {
      var th = el('th', '', cols[i].label || cols[i].key || ('列 ' + (i + 1)));
      if (cols[i].key) { th.title = cols[i].key; }
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el('tbody');
    for (var r = 0; r < list.length; r++) {
      var cells = [];
      for (var c = 0; c < cols.length; c++) {
        var v = list[r][cols[c].key];
        var text = (v === null || v === undefined || v === '') ? '—'
          : (isObj(v) || Array.isArray(v) ? jsonText(v, 160) : String(v));
        cells.push(cell(text, cols[c].mono ? 'mono' : 'wrap'));
      }
      tbody.appendChild(row(cells));
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function detailBlock(box, env) {
    var fields = (env.fields || []).filter(isObj);
    var sections = (env.sections || []).filter(isObj);
    if (fields.length) {
      var dl = el('dl', 'env-list res-fields');
      for (var i = 0; i < fields.length; i++) {
        var wrap = el('div');
        wrap.appendChild(el('dt', '', dash(fields[i].label)));
        var raw = fields[i].value;
        var text = (raw === null || raw === undefined || raw === '') ? '—'
          : (isObj(raw) || Array.isArray(raw) ? jsonText(raw, 200) : String(raw));
        wrap.appendChild(el('dd', monoish(text) ? 'mono' : '', text));
        dl.appendChild(wrap);
      }
      box.appendChild(dl);
    }
    for (var s = 0; s < sections.length; s++) {
      var sec = sections[s];
      var secBox = el('div', 'res-section');
      secBox.appendChild(el('h3', '', sec.label || ('段落 ' + (s + 1))));
      if (sec.kind === 'table' || (!sec.kind && sec.columns)) {
        secBox.appendChild(tableBlock(sec.columns, sec.rows, '该段没有行。'));
      } else if (sec.kind === 'text' || (!sec.kind && typeof sec.text === 'string')) {
        secBox.appendChild(el('pre', 'text-block', sec.text === undefined || sec.text === null ? '（空）' : String(sec.text)));
      } else if (sec.message) {
        secBox.appendChild(el('p', 'res-message', sec.message));
      } else {
        secBox.appendChild(el('pre', 'text-block mono', jsonText(sec, 2000)));
      }
      box.appendChild(secBox);
    }
    if (!fields.length && !sections.length) {
      box.appendChild(el('p', 'empty', '详情响应里没有 fields / sections。'));
    }
  }

  /* ------------------------------------------------------------------ 运行日志页 */

  function loadJobs() {
    /* 契约允许 ?platform=，但这里刻意拉全局：任务轮询与自动接管不该受平台切换影响，
       列表按当前平台在前端筛行。 */
    return api('/api/jobs').then(function (body) {
      var list = (body && body.jobs) || [];
      list.sort(function (a, b) {
        var ta = parseTime(a.started_at), tb = parseTime(b.started_at);
        if (ta === null && tb === null) { return 0; }
        if (ta === null) { return 1; }
        if (tb === null) { return -1; }
        return tb - ta;
      });
      state.jobs = list;
      renderJobs();
      clearProblem('jobs');
      if (list.length && !state.currentId) {
        /* 页面刚打开时若已有任务在跑，自动接管轮询。 */
        for (var i = 0; i < list.length; i++) {
          if (list[i].status === 'running') {
            trackJob(list[i].job_id);
            break;
          }
        }
      }
      return list;
    }).catch(function (e) {
      setProblem('jobs', '任务列表加载失败：' + describeError(e), 'error');
    });
  }

  /* 按当前平台筛行：后端若已过滤则等于原样，未过滤则兜底。 */
  function scopeRows(list) {
    if (!state.platform) { return list; }
    return list.filter(function (x) {
      return !x || x.platform === undefined || x.platform === state.platform;
    });
  }

  function platformCell(value) {
    var c = cell(value, 'mono');
    c.hidden = !!state.platform;   // 聚合视图下每行显示平台列
    return c;
  }

  function renderJobs() {
    var list = scopeRows(state.jobs);
    var rows = list.map(function (job) {
      var spec = statusSpec(job.status);
      var actions = el('td', 'ctr');
      var btn = el('button', 'btn btn-sm', job.status === 'running' ? '跟踪' : '查看');
      btn.type = 'button';
      btn.addEventListener('click', function () { viewJob(job.job_id); });
      actions.appendChild(btn);
      return row([
        cell(job.job_id, 'mono'),
        platformCell(job.platform),
        tdLive(job.live === true),
        tdBadge(spec.text, spec.cls),
        cell(fmtTime(job.started_at), 'mono'),
        cell(fmtTime(job.finished_at), 'mono'),
        cell(job.summary, 'wrap'),
        cell(job.error, 'err'),
        actions
      ]);
    });
    $('jobs-filter').textContent = state.platform
      ? '（按当前平台前端筛选：' + state.platform + '；轮询仍是全局任务）'
      : '';
    $('jobs-count').textContent = list.length ? ('共 ' + list.length + ' 个') : '';
    setColumnVisible('jobs-table', 'platform', !state.platform);
    fillTable('jobs-table', 'jobs-body', 'jobs-empty', rows,
      state.platform
        ? '平台「' + state.platform + '」还没有任务记录。'
        : '还没有任务记录。');
  }

  function setColumnVisible(tableId, colKey, visible) {
    var table = $(tableId);
    if (!table) { return; }
    var th = table.querySelector('th[data-col="' + colKey + '"]');
    if (th) { th.hidden = !visible; }
  }

  function viewJob(jobId) {
    clearProblem('job');
    api('/api/jobs/' + encodeURIComponent(jobId)).then(function (body) {
      var job = isObj(body) ? body : {};
      renderCurrentJob(job);
      if (job.status === 'running') {
        trackJob(jobId);
        if (state.page !== 'overview') {
          showToast('任务 ' + jobId + ' 正在运行，已接管轮询（进度见「概览 · 当前任务」）');
        }
      } else {
        cancelTimer();
        state.currentId = null;
        $('job-poll').textContent = '任务已结束（' + statusSpec(job.status).text + '）';
      }
    }).catch(function (e) {
      setProblem('job', describeError(e), 'error');
    });
  }

  function loadRuns() {
    return scopedGet('/api/runs', { limit: LIST_LIMIT }).then(function (res) {
      if (!adoptScope(res, '/api/runs')) { return; }
      state.runs = scopeRows((res.body && res.body.runs) || []);
      renderRuns();
      clearProblem('runs');
    }).catch(function (e) {
      setProblem('runs', '跑批历史加载失败：' + describeError(e, { platform: state.platform }), 'error');
      if (e && e.kind === 'unknown_platform') { handleUnknownScope(e); }
    });
  }

  function renderRuns() {
    var rows = state.runs.map(function (r) {
      var spec = statusSpec(r.status);
      var detailText = '';
      if (isObj(r.detail) && Object.keys(r.detail).length) {
        detailText = jsonText(r.detail, 120);
      }
      return row([
        cell(r.id, 'mono'),
        platformCell(r.platform),
        cell(r.assignment_id, 'mono'),
        tdBadge(spec.text, spec.cls),
        cell(fmtNum(r.attempts), 'num mono'),
        cell(fmtTime(r.started_at), 'mono'),
        cell(fmtTime(r.finished_at), 'mono'),
        cell(r.error, 'err'),
        cell(detailText, 'wrap muted mono')
      ]);
    });
    $('runs-count').textContent = state.runs.length
      ? ('共 ' + state.runs.length + ' 条（limit=' + LIST_LIMIT + '）')
      : '';
    setColumnVisible('runs-table', 'platform', !state.platform);
    fillTable('runs-table', 'runs-body', 'runs-empty', rows,
      state.platform
        ? '平台「' + platformLabel(state.platform) + '」还没有跑批记录。用「概览 · 触发跑批」发起一次干跑试试。'
        : '还没有跑批记录。用「概览 · 触发跑批」发起一次干跑试试。');
  }

  function loadSubmissions() {
    return scopedGet('/api/submissions', { limit: LIST_LIMIT }).then(function (res) {
      if (!adoptScope(res, '/api/submissions')) { return; }
      state.submissions = scopeRows((res.body && res.body.submissions) || []);
      renderSubmissions();
      clearProblem('subs');
    }).catch(function (e) {
      setProblem('subs', '提交记录加载失败：' + describeError(e, { platform: state.platform }), 'error');
      if (e && e.kind === 'unknown_platform') { handleUnknownScope(e); }
    });
  }

  function renderSubmissions() {
    var rows = state.submissions.map(function (s) {
      return row([
        cell(s.id, 'mono'),
        platformCell(s.platform),
        cell(s.assignment_id, 'mono'),
        tdBadge(s.ok === true ? '成功' : '失败', s.ok === true ? 'badge-ok' : 'badge-fail'),
        cell(fmtScore(s.score), 'num mono'),
        cell(s.message, 'wrap'),
        cell(fmtTime(s.created_at), 'mono')
      ]);
    });
    $('subs-count').textContent = state.submissions.length
      ? ('共 ' + state.submissions.length + ' 条（limit=' + LIST_LIMIT + '）')
      : '';
    setColumnVisible('subs-table', 'platform', !state.platform);
    fillTable('subs-table', 'subs-body', 'subs-empty', rows,
      '还没有提交记录。干跑不会产生提交，只有真提交（live=true）才会有。');
  }

  /* ---- 事件流 ---- */

  function loadEvents() {
    if (state.eventsOff) { return Promise.resolve(); }
    return api('/api/events?limit=' + EVENT_LIMIT).then(function (body) {
      var events = (body && body.events) || [];
      renderEvents(events);
      clearProblem('events');
    }).catch(function (e) {
      /* 失败要说清楚，并暂停 1 秒一次的重试，避免每秒刷错误；点「刷新」会重置。 */
      state.eventsOff = true;
      $('events-hint').textContent = '已暂停轮询';
      setProblem('events', '事件流读取失败：' + describeError(e) + '（已暂停轮询，点「刷新」重试）', 'warn');
    });
  }

  function renderEvents(events) {
    var list = $('events-list');
    clear(list);
    var recent = events.slice(-EVENT_LIMIT).reverse();
    if (!recent.length) {
      list.hidden = true;
      $('events-empty').hidden = false;
      $('events-empty').textContent = '暂无事件。跑批开始后这里会出现实时日志。';
      return;
    }
    $('events-empty').hidden = true;
    list.hidden = false;
    for (var i = 0; i < recent.length; i++) {
      var ev = isObj(recent[i]) ? recent[i] : {};
      var li = el('li');
      li.appendChild(el('span', 'ev-at mono', fmtTime(ev.at)));
      li.appendChild(el('span', 'ev-name mono', ev.name || '—'));
      var raw = '';
      if (isObj(ev.payload) || Array.isArray(ev.payload)) {
        raw = jsonText(ev.payload, 500);
      } else if (ev.payload !== undefined && ev.payload !== null) {
        raw = String(ev.payload);
      }
      var p = el('span', 'ev-payload mono', truncate(raw, 160));
      p.title = truncate(raw, 500);
      li.appendChild(p);
      list.appendChild(li);
    }
  }

  /* ------------------------------------------------------------------ 统计页（跨平台） */

  function loadStatistics() {
    return api('/api/statistics').then(function (body) {
      state.statistics = isObj(body) ? body : {};
      renderStatistics();
      renderStatBank();
      clearProblem('statistics');
    }).catch(function (e) {
      setProblem('statistics', '统计对比加载失败：' + describeError(e), 'error');
    });
  }

  /* 统计页的趋势刻意取「全部平台」口径：它本身就是横向视图。 */
  function loadStatRuns() {
    return api('/api/runs?limit=' + LIST_LIMIT).then(function (body) {
      var list = (body && body.runs) || [];
      state.statRuns = list;
      renderTrend();
      clearProblem('statruns');
    }).catch(function (e) {
      setProblem('statruns', '全平台跑批历史加载失败：' + describeError(e), 'error');
    });
  }

  function renderStatistics() {
    var stats = state.statistics || {};
    var list = stats.platforms || [];
    var rows = list.map(function (p) {
      var total = isNum(p.total) ? p.total : null;
      var ok = isNum(p.succeeded) ? p.succeeded : null;
      var rate = (total && total > 0 && ok !== null) ? (ok / total) : null;
      var r = row([
        cell(p.platform, 'mono'),
        cell(fmtNum(p.total), 'num mono'),
        cell(fmtNum(p.succeeded), 'num mono'),
        cell(fmtNum(p.failed), 'num mono'),
        cell(fmtNum(p.skipped), 'num mono'),
        cell(rate === null ? '—' : fmtPct(rate), 'num mono'),
        cell(fmtNum(p.submissions), 'num mono'),
        cell(fmtNum(p.submit_ok), 'num mono'),
        cell(fmtNum(p.questions), 'num mono'),
        cell(fmtPct(p.accuracy), 'num mono')
      ]);
      if (p.platform === state.platform) { r.className = 'row-current'; }
      return r;
    });
    fillTable('stats-cmp-table', 'stats-cmp-body', 'stats-cmp-empty', rows,
      '统计接口没有返回任何平台分组。');
  }

  function renderStatBank() {
    var bank = ((state.statistics || {}).bank) || {};
    var total = bank.total;
    var isEmpty = isNum(total) && total === 0;
    $('stats-bank-hint').textContent = isNum(total) ? ('共 ' + total + ' 题（全部平台）') : '';
    $('stats-bank-empty').hidden = !isEmpty;
    $('stats-bank-body').hidden = isEmpty;
    if (isEmpty) {
      $('stats-bank-empty').textContent = '题库为空（全部平台合计 0 题）。跑批命中题目后会自动入库。';
      fillTable('stats-source-table', 'stats-source-body', 'stats-source-empty', [],
        '题库为空，没有来源构成可展示。');
      return;
    }
    setText('stats-total', fmtNum(bank.total));
    setText('stats-graded', fmtNum(bank.graded));
    setText('stats-correct', fmtNum(bank.correct));
    setText('stats-accuracy', fmtPct(bank.accuracy));
    setText('stats-ungraded', fmtNum(bank.ungraded));
    renderSourceTable(bank.by_source, 'stats-source-table', 'stats-source-body',
      'stats-source-empty', 'stats-source-hint', '全部平台');
  }

  function renderTrend() {
    var hint = $('stats-trend-hint');
    if (!state.statRuns.length) {
      hint.textContent = '';
      fillTable('stats-trend-table', 'stats-trend-body', 'stats-trend-empty', [],
        '没有任何跑批记录（全部平台），画不出趋势。');
      return;
    }
    var byDay = {};
    for (var i = 0; i < state.statRuns.length; i++) {
      var r = state.statRuns[i] || {};
      var d = dayOf(r.started_at);
      if (!d) { continue; }
      if (!byDay[d]) { byDay[d] = { total: 0, succeeded: 0, failed: 0, skipped: 0, other: 0 }; }
      var bucket = byDay[d];
      bucket.total++;
      if (r.status === 'succeeded') { bucket.succeeded++; }
      else if (r.status === 'failed') { bucket.failed++; }
      else if (r.status === 'skipped') { bucket.skipped++; }
      else { bucket.other++; }
    }
    var days = Object.keys(byDay).sort().reverse().slice(0, TREND_DAYS);
    hint.textContent = '全部平台 · 按 started_at 聚合 · 最近 ' + days.length +
      ' 天 · 窗口 limit=' + LIST_LIMIT;
    var rows = days.map(function (d) {
      var b = byDay[d];
      return row([
        cell(d, 'mono'),
        cell(fmtNum(b.total), 'num mono'),
        cell(fmtNum(b.succeeded), 'num mono'),
        cell(fmtNum(b.failed), 'num mono'),
        cell(fmtNum(b.skipped), 'num mono'),
        barCell([
          { cls: 'seg-ok', value: b.total ? b.succeeded / b.total : 0, title: '成功 ' + b.succeeded },
          { cls: 'seg-fail', value: b.total ? b.failed / b.total : 0, title: '失败 ' + b.failed },
          { cls: 'seg-skip', value: b.total ? (b.skipped + b.other) / b.total : 0, title: '跳过 ' + b.skipped }
        ])
      ]);
    });
    fillTable('stats-trend-table', 'stats-trend-body', 'stats-trend-empty', rows,
      '没有带时间的跑批记录。');
  }

  /* ------------------------------------------------------------------ 账号页 */

  function loadSettings() {
    return api('/api/settings').then(function (body) {
      state.settings = isObj(body) ? body : {};
      renderSettings();
      clearProblem('settings');
    }).catch(function (e) {
      setProblem('settings', '账号配置加载失败：' + describeError(e), 'error');
    });
  }

  function renderSettings() {
    var s = state.settings;
    var grid = $('settings-grid');
    var empty = $('settings-empty');
    if (!isObj(s)) {
      grid.hidden = true;
      empty.hidden = false;
      empty.textContent = '还没取到配置数据（服务未连接或加载失败），点「刷新」重试。';
      $('set-hint').textContent = '';
      renderSettingsKeys(null);
      renderSettingsPlatforms(null);
      renderSettingsDeps(null);
      return;
    }
    empty.hidden = true;
    $('set-hint').textContent = '来自 GET /api/settings（全局，不随当前平台过滤）';

    var pairs = [
      ['env', s.env],
      ['dry_run', s.dry_run === undefined ? null : fmtBool(s.dry_run) + '（true = 跑批默认干跑）'],
      ['headless', s.headless === undefined ? null : fmtBool(s.headless)],
      ['max_concurrency', s.max_concurrency],
      ['min_interval', s.min_interval],
      ['max_attempts', s.max_attempts],
      ['min_confidence', s.min_confidence],
      ['notify', s.notify],
      ['db_path', s.db_path],
      ['db_exists', s.db_exists === undefined ? null : fmtBool(s.db_exists)],
      ['session_dir', s.session_dir],
      ['dotenv_present', s.dotenv_present === undefined ? null : fmtBool(s.dotenv_present)]
    ];
    var kids = [];
    for (var i = 0; i < pairs.length; i++) {
      var wrap = el('div');
      wrap.appendChild(el('dt', '', pairs[i][0]));
      wrap.appendChild(el('dd', 'mono', dash(pairs[i][1])));
      kids.push(wrap);
    }
    fill(grid, kids);
    grid.hidden = false;

    renderSettingsKeys(s.env_keys_seen);
    renderSettingsPlatforms(s.platforms);
    renderSettingsDeps(s.optional_deps);

    $('settings-dotenv').textContent = s.dotenv_present === undefined
      ? '—'
      : (s.dotenv_present ? '已找到' : '未找到（.env 不存在或不在服务启动目录）');
  }

  /* env_keys_seen：只列键名，绝不列值。 */
  function renderSettingsKeys(keys) {
    var list = $('settings-keys');
    var empty = $('settings-keys-empty');
    var arr = keys || [];
    if (!arr.length) {
      list.hidden = true;
      clear(list);
      empty.hidden = false;
      empty.textContent = isObj(state.settings)
        ? '没有读到任何环境变量键名。若已在 .env 里配了凭据，检查 .env 的位置与服务启动目录。'
        : '还没取到数据。';
      return;
    }
    empty.hidden = true;
    var kids = [];
    for (var i = 0; i < arr.length; i++) {
      kids.push(el('li', 'key-item mono', String(arr[i])));
    }
    fill(list, kids);
    list.hidden = false;
  }

  /* 每个平台一行：凭据状态 + 期望键名 + 它自己的能力（不横向铺矩阵）。 */
  function renderSettingsPlatforms(platforms) {
    var arr = platforms || [];
    $('settings-plat-hint').textContent = isObj(state.settings)
      ? ('共 ' + arr.length + ' 个平台 · 凭据值后端不返回')
      : '';
    var rows = arr.map(function (p) {
      var hintCell = cell(p.credentials_hint, 'mono wrap');
      if (!p.credentials_hint) {
        hintCell.textContent = '—（该适配器没声明期望键名）';
      }
      var credCell = el('td');
      credCell.appendChild(p.has_credentials
        ? badge('已配置', 'badge-ok')
        : badge('未配置', 'badge-warn'));
      var capCell2 = el('td');
      capCell2.appendChild(capSummary(p.capabilities || {}));
      var r = row([
        cell(p.name, 'mono'),
        cell(p.display_name || p.name),
        credCell,
        hintCell,
        capCell2
      ]);
      if (p.name === state.platform) { r.className = 'row-current'; }
      return r;
    });
    fillTable('settings-plat-table', 'settings-plat-body', 'settings-plat-empty', rows,
      '没有平台信息。');
  }

  function renderSettingsDeps(deps) {
    if (!isObj(deps)) {
      fillTable('settings-deps-table', 'settings-deps-body', 'settings-deps-empty', [],
        '还没取到可选依赖状态。');
      return;
    }
    var keys = Object.keys(deps);
    var rows = keys.map(function (k) {
      var c = el('td');
      c.appendChild(deps[k] ? badge('可用', 'badge-ok') : badge('不可用', 'badge-warn'));
      return row([
        cell(k, 'mono'),
        c,
        cell(DEP_LABEL[k] || '—', 'wrap muted')
      ]);
    });
    fillTable('settings-deps-table', 'settings-deps-body', 'settings-deps-empty', rows,
      '没有可选依赖信息。');
  }

  /* ------------------------------------------------------------------ 启动 */

  function loadPageData(noPlatformRetry) {
    /* 先保证平台清单在（左侧栏与跑批下拉要用），再按当前页取数。
       平台清单取失败时只重试一次，避免无限循环。 */
    if (!state.platformsLoaded && !noPlatformRetry) {
      return loadPlatforms().then(function () { return loadPageData(true); });
    }

    var jobs = [loadOverview()];
    if (state.page === 'courses') {
      jobs.push(loadActionPage('courses'));
    } else if (state.page === 'assignments') {
      jobs.push(loadActionPage('assignments'));
    } else if (state.page === 'bank') {
      jobs.push(loadBank());
    } else if (state.page === 'tools') {
      jobs.push(loadActions());
    } else if (state.page === 'logs') {
      jobs.push(loadRuns(), loadSubmissions(), loadJobs(), loadEvents());
    } else if (state.page === 'stats') {
      jobs.push(loadStatistics(), loadStatRuns());
    } else if (state.page === 'account') {
      jobs.push(loadSettings());
    }
    return Promise.all(jobs);
  }

  function refreshAll() {
    clearProblem('events');
    state.eventsOff = false;
    $('events-hint').textContent = '任务运行中每 1 秒刷新';
    return loadPlatforms().then(function () {
      var r = parseHash();
      r.platform = sanitizePlatform(r.platform);
      if (r.platform !== state.platform) {
        state.platform = r.platform;
        replaceHash(r.page, null);
        resetTools();
        resetActionPage('courses');
        resetActionPage('assignments');
        clearProblemsWithPrefix('scope:');
      }
      syncRunPlatformOptions(true);
      renderChrome();
      return loadPageData(true);
    });
  }

  function bindEvents() {
    $('refresh-btn').addEventListener('click', function () { refreshAll(); });

    /* 左侧栏下段：功能导航，只用 hash，不触发整页刷新。 */
    $('func-nav').addEventListener('click', function (ev) {
      var a = ev.target.closest ? ev.target.closest('a[data-page]') : null;
      if (!a) { return; }
      ev.preventDefault();
      navigate(a.getAttribute('data-page'), state.platform);
    });

    /* 左侧栏上段：平台列表，切「当前平台」，页面不动、数据立即重取。 */
    $('platform-nav').addEventListener('click', function (ev) {
      var a = ev.target.closest ? ev.target.closest('a[data-platform]') : null;
      if (!a) { return; }
      ev.preventDefault();
      var name = a.getAttribute('data-platform');
      navigate(state.page, name || null);
    });

    window.addEventListener('hashchange', function () { applyRoute(); });

    $('run-form').addEventListener('submit', onRunSubmit);
    $('run-platform').addEventListener('change', function () { applyRunConstraints(); });

    /* live 打开必须二次确认；取消则回到关闭状态。 */
    $('run-live').addEventListener('change', function () {
      var live = $('run-live');
      if (live.checked && !window.confirm(LIVE_CONFIRM)) {
        live.checked = false;
      }
      updateRunButton();
    });

    $('run-limit').addEventListener('change', function () {
      var node = $('run-limit');
      var v = parseInt(node.value, 10);
      if (!isFinite(v) || v < 0) { node.value = '0'; }
      if (v > 500) { node.value = '500'; }   // 契约：limit 钳制到 1..500
    });

    /* 课程 / 作业：进入页面自动加载一次（只读），也能手动重载。 */
    $('courses-reload').addEventListener('click', function () { loadActionPage('courses'); });
    $('assignments-reload').addEventListener('click', function () {
      $('assign-course').value = '';
      loadActionPage('assignments', {});
    });
    $('assignments-form').addEventListener('submit', function (ev) {
      ev.preventDefault();
      var course = String($('assign-course').value || '').trim();
      loadActionPage('assignments', course ? { course: course } : {});
    });

    /* 工具页：动作清单 + 参数表单 + 结果 */
    $('actions-nav').addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button[data-action]') : null;
      if (!btn) { return; }
      var name = btn.getAttribute('data-action');
      var list = state.actions.list || [];
      for (var i = 0; i < list.length; i++) {
        if (list[i].name === name) {
          state.actions.active = list[i];
          state.actions.result = null;
          clearToolsError();
          renderTools();
          var first = $('ap-' + (((list[i].params || [])[0]) || {}).name);
          if (first) { first.focus(); }
          return;
        }
      }
    });
    $('action-form').addEventListener('submit', onActionSubmit);

    /* 页面切走时暂停轮询，切回来再续；关闭/离开时立即清掉定时器，不留悬挂。 */
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        cancelTimer();
      } else if (state.currentId) {
        pollJob();
      }
    });
    window.addEventListener('pagehide', cancelTimer);
    window.addEventListener('beforeunload', cancelTimer);
  }

  function boot() {
    window.__kedaibiao = true;
    bindEvents();
    $('run-live').checked = false;   // 契约：live 默认必须关闭

    var r = parseHash();
    state.page = r.page;
    state.platform = r.platform;
    renderChrome();
    showPage();
    renderTools();
    renderActionPage('courses');
    renderActionPage('assignments');
    renderBank();
    applyRunConstraints();

    if (location.protocol === 'file:') {
      /* file:// 直开：不发任何请求，页面不崩，给出人话提示。 */
      $('job-empty').textContent = '尚未连接服务：file:// 直开无法访问本地接口。';
      $('platform-nav-empty').hidden = false;
      $('platform-nav-empty').textContent = '尚未连接服务：file:// 直开读不到平台列表。';
      $('courses-gate').hidden = false;
      $('courses-gate').textContent = '尚未连接服务：file:// 直开读不到接口。';
      $('assignments-gate').hidden = false;
      $('assignments-gate').textContent = '尚未连接服务：file:// 直开读不到接口。';
      $('bank-gate').hidden = false;
      $('bank-gate').textContent = '尚未连接服务：file:// 直开读不到接口。';
      $('tools-gate').hidden = false;
      $('tools-gate').textContent = '尚未连接服务：file:// 直开读不到接口。';
      $('settings-empty').hidden = false;
      $('settings-empty').textContent = '尚未连接服务：file:// 直开读不到配置。';
      return;
    }

    /* 先取平台清单（用于校验 hash 里的平台），再取概览与当前页数据。 */
    loadPlatforms().then(function () {
      var r2 = parseHash();
      var plt = sanitizePlatform(r2.platform);
      if (plt !== state.platform) {
        state.platform = plt;
        replaceHash(state.page, plt);
        renderChrome();
      }
      renderPlatformNav();
      syncRunPlatformOptions(true);
      applyRunConstraints();
      return loadPageData(true);
    });
    /* 此后只有「有任务在跑」时才存在定时器：每秒拉一次 /api/jobs/<id>，
       顺带拉一次事件流，任务结束即全部停掉。 */
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
