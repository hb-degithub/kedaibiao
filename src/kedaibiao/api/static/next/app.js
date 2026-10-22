/* 课代表 kedaibiao · 控制台 v2（demo）
 *
 * 排版逻辑的三个变化：
 *   1. 平台切换从「左侧栏第二段」升到顶栏药丸组 —— 作用域常驻可见，左轨只放功能；
 *   2. 详情一律进右侧检查器（不跳页、不弹 modal），主列表保持可扫读；
 *   3. 说明文字收进页脚折叠块，表格只用于横向对比，主内容用行式列表。
 *
 * 约束沿用 v1：零构建、零依赖、零 CDN；动态文本一律 textContent 写入；
 * 令牌由 HttpOnly Cookie 承载，本文件不读写任何凭据到 localStorage。
 */
(function () {
  'use strict';

  /* ================================================================ 工具 */

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function append(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid) { parent.appendChild(kid); }
    }
    return parent;
  }

  function clear(node) { if (node) { node.textContent = ''; } return node; }

  function isObj(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }
  function has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function dash(v) { return (v === null || v === undefined || v === '') ? '—' : String(v); }

  function num(v) {
    if (v === null || v === undefined || v === '') { return '—'; }
    var n = Number(v);
    return isFinite(n) ? String(n) : String(v);
  }

  function pct(v, digits) {
    var n = Number(v);
    if (v === null || v === undefined || !isFinite(n)) { return '—'; }
    return (n * 100).toFixed(digits === undefined ? 0 : digits) + '%';
  }

  function time(v) {
    if (!v) { return '—'; }
    return String(v).replace('T', ' ').replace(/Z$/, '');
  }

  function elapsed(start, end) {
    if (!start) { return '—'; }
    var a = Date.parse(String(start));
    if (!isFinite(a)) { return '—'; }
    var b = end ? Date.parse(String(end)) : Date.now();
    if (!isFinite(b)) { b = Date.now(); }
    var s = Math.max(0, Math.round((b - a) / 1000));
    if (s < 60) { return s + ' 秒'; }
    var m = Math.floor(s / 60);
    if (m < 60) { return m + ' 分 ' + String(s % 60).padStart(2, '0') + ' 秒'; }
    var h = Math.floor(m / 60);
    return h + ' 小时 ' + String(m % 60).padStart(2, '0') + ' 分';
  }

  function truncate(s, n) {
    s = (s === null || s === undefined) ? '' : String(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function jsonText(v, max) {
    if (v === null || v === undefined) { return ''; }
    var raw;
    if (typeof v === 'object') {
      try { raw = JSON.stringify(v); } catch (e) { raw = String(v); }
    } else {
      raw = String(v);
    }
    return truncate(raw, max || 200);
  }

  function pill(text, kind) {
    return el('span', 'pill' + (kind ? ' ' + kind : ''), text);
  }

  function dotPill(text, kind) {
    var p = pill(text, kind);
    p.insertBefore(el('span', 'dot'), p.firstChild);
    return p;
  }

  function btn(label, cls, onClick) {
    var b = el('button', 'btn' + (cls ? ' ' + cls : ''), label);
    b.type = 'button';
    if (onClick) { b.addEventListener('click', onClick); }
    return b;
  }

  /* 静态图标（本地字面量，不含任何后端数据） */
  var ICONS = {
    overview: 'M3 10.5 12 3l9 7.5M5.5 9.5V21h13V9.5',
    tasks: 'M4 6h16M4 12h10M4 18h7M17 15l2 2 3-4',
    courses: 'M4 5h7v15H4zM13 5h7v15h-7z',
    assignments: 'M7 3h10v3H7zM5 6h14v15H5zM8 11h8M8 15h5',
    bank: 'M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
    logs: 'M4 5h16v14H4zM8 9l3 3-3 3M13 15h4',
    stats: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
    accounts: 'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4.5 21a7.5 7.5 0 0 1 15 0',
    tools: 'M14.5 4.5a4 4 0 0 0 5 5L11 18l-5 1 1-5 7.5-9.5zM4 4l6 6',
    users: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2 20a7 7 0 0 1 14 0M17 4.5a3.5 3.5 0 0 1 0 7M18 20a6 6 0 0 0-2-4.5'
  };

  function icon(name) {
    var wrap = document.createElement('span');
    wrap.className = 'ico';
    wrap.setAttribute('aria-hidden', 'true');
    wrap.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" ' +
      'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round"><path d="' + (ICONS[name] || ICONS.overview) + '"/></svg>';
    return wrap;
  }

  /* ================================================================ 状态 */

  var state = {
    page: 'overview',
    platform: null,
    platforms: [],
    platformsLoaded: false,
    user: null,
    isSuper: false,
    users: [],
    ownerId: null,
    overview: null,
    settings: null,
    jobs: [],
    /* 当前作用域下的平台账号（顶栏切换器与账号简介都用它） */
    accounts: [],
    /* 工具页是否显示「已有专属入口」的动作 */
    toolsShowAll: false,
    /* 每个平台的动作名集合：能力型页面据此决定出不出现在左轨 */
    platformActions: {},
    /* 提交成功且回读 finish=True 的作业：列表里直接显示「已完成」，
       不用为了一行状态去逐组回读 progress/v2（那样要几十次请求）。 */
    doneOverrides: {},
    accountsLoaded: false,
    accountsSeq: 0,
    connected: false,
    pageSeq: 0,
    timers: [],
    returnHash: '',
    theme: 'dark',
    railMin: false
  };

  /* ================================================================ 请求 */

  var AUTH_PATHS = { '/api/auth/login': 1, '/api/auth/logout': 1, '/api/auth/me': 1 };
  var OWNER_SCOPED = {
    '/api/overview': 1, '/api/runs': 1, '/api/submissions': 1, '/api/bank': 1,
    '/api/statistics': 1, '/api/jobs': 1, '/api/events': 1, '/api/accounts': 1
  };

  function pathOnly(p) { var i = String(p).indexOf('?'); return i < 0 ? String(p) : String(p).slice(0, i); }

  function setConn(ok) {
    state.connected = ok;
    var node = $('conn');
    if (!node) { return; }
    node.className = 'conn ' + (ok ? 'ok' : 'err');
    node.textContent = ok ? '已连接' : '连接中断';
  }

  function api(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {}
    };
    if (init.method === 'POST') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body || {});
    }
    return fetch(path, init).then(function (res) {
      var ct = res.headers.get('content-type') || '';
      var parsed = ct.indexOf('json') >= 0
        ? res.json().catch(function () { return null; })
        : Promise.resolve(null);
      return parsed.then(function (body) {
        if (!res.ok) {
          var err = new Error((body && body.error) || ('HTTP ' + res.status));
          err.kind = (body && body.kind) || ('http_' + res.status);
          err.status = res.status;
          err.detail = (body && body.detail) || '';
          if (res.status === 401 && !AUTH_PATHS[pathOnly(path)]) { onUnauthorized(); }
          throw err;
        }
        setConn(true);
        return body;
      });
    }, function (e) {
      var err = new Error(String((e && e.message) || e));
      err.kind = 'network';
      err.status = 0;
      throw err;
    });
  }

  function get(path, params) {
    var qs = [];
    if (params) {
      Object.keys(params).forEach(function (k) {
        if (params[k] !== undefined && params[k] !== null && params[k] !== '') {
          qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
      });
    }
    if (OWNER_SCOPED[path] && state.isSuper && state.ownerId !== null) {
      qs.push('owner_id=' + encodeURIComponent(state.ownerId));
    }
    return api(path + (qs.length ? '?' + qs.join('&') : ''));
  }

  function post(path, body) { return api(path, { method: 'POST', body: body || {} }); }

  var ERROR_HINT = {
    unauthorized: '登录状态已失效，请重新登录。',
    no_credentials: '这个平台还没有可用的账号：请到「账号」页添加并登录。',
    unknown_platform: '平台名不对或适配器没注册，点顶栏「刷新」重读平台列表。',
    unknown_action: '该平台没有声明这个动作，刷新后会重新读取动作清单。',
    busy: '同一时刻只允许一个后台任务，等当前任务结束后再试。',
    bad_request: '请求参数不符合要求，具体原因见上面的原文。',
    forbidden: '权限不足，或访问来源被安全策略拒绝。',
    not_found: '目标不存在，可能已被清理。',
    not_supported: '该平台还没有实现这个能力（后端返回 501），按钮会保持禁用。',
    platform_error: '平台侧失败：先确认账号会话是否还有效，再重试。',
    internal: '服务端自身异常，请看服务端日志。'
  };

  function describe(e) {
    if (!e) { return '未知错误'; }
    if (e.kind === 'network') {
      return '连不上控制台服务（' + (e.message || '请求失败') +
        '）。请确认服务在运行，并且是通过 http://127.0.0.1:<端口> 打开的。';
    }
    var base = String(e.message || '').trim() || ('请求失败（HTTP ' + e.status + '）');
    if (e.kind === 'platform_error' && e.detail && base.indexOf(e.detail) < 0) {
      base += '（平台异常：' + e.detail + '）';
    }
    var hint = ERROR_HINT[e.kind];
    if (hint && base.indexOf(hint) < 0) { base += ' · ' + hint; }
    return base;
  }

  function toast(msg) {
    var node = $('toast');
    if (!node) { return; }
    node.textContent = msg;
    node.hidden = false;
    if (toast._t) { clearTimeout(toast._t); }
    toast._t = setTimeout(function () { node.hidden = true; }, 5200);
  }

  /* ================================================================ 状态块 */

  function stateBlock(kind, title, detail, actions) {
    var box = el('div', 'state' + (kind ? ' state-' + kind : ''));
    var glyph = kind === 'error' ? '!' : (kind === 'gate' ? '⌘' : '·');
    box.appendChild(el('div', 'state-icon', glyph));
    if (title) { box.appendChild(el('strong', '', title)); }
    if (detail) { box.appendChild(el('span', '', detail)); }
    if (actions && actions.length) {
      var row = el('div', 'state-actions');
      actions.forEach(function (a) { row.appendChild(a); });
      box.appendChild(row);
    }
    return box;
  }

  function loadingBlock() {
    var box = el('div', 'skel');
    append(box, el('span'), el('span'), el('span'));
    return box;
  }

  function errorBlock(e, retry) {
    var acts = [];
    /* 会话类错误光说「登录失败」没用 —— 直接给出口：去账号页重新登录。
       东软 / U校园这类 bearer token 平台，会话过期是每天都会遇到的事。 */
    var authish = e && (e.kind === 'platform_error' || e.kind === 'no_credentials'
      || e.kind === 'unauthorized');
    if (authish) {
      acts.push(btn('去账号页重新登录', 'btn-sm btn-primary', function () {
        go('accounts', state.platform || null);
      }));
    }
    if (retry) { acts.push(btn('重试', 'btn-sm', retry)); }
    var detail = describe(e);
    if (authish) {
      detail += ' · 账号库不保存平台密码，会话过期后需要在这个平台重新登录一次。';
    }
    return stateBlock('error', authish ? '平台会话失效' : '这一页没取到数据', detail, acts);
  }

  function gateBlock(text, actions) {
    return stateBlock('gate', '需要先选一个平台', text || '顶栏切换到一个具体平台，这个功能才有意义。', actions);
  }

  /* ================================================================ 主题 */

  function applyTheme(name) {
    state.theme = name === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', state.theme);
    try { localStorage.setItem('kdb.theme', state.theme); } catch (e) { /* 隐私模式下忽略 */ }
    var b = $('theme-btn');
    if (b) { b.textContent = state.theme === 'light' ? '☾' : '☀'; }
  }

  /* ================================================================ 路由 */

  var PAGE_ALIAS = { records: 'logs', settings: 'accounts', home: 'overview' };

  var PAGES = {
    overview: { label: '概览', group: '工作台', needsPlatform: false },
    tasks: { label: '任务', group: '工作台', needsPlatform: false },
    courses: { label: '课程', group: '内容', needsPlatform: true,
               requiresCapability: 'list_courses' },
    assignments: { label: '作业', group: '内容', needsPlatform: true,
                   requiresCapability: 'list_assignments' },
    /* 能力型页面：只有平台声明了对应动作才出现（iTEST 没考勤，就不该有考勤页） */
    /* 考勤有两种模型：东软的「场次列表 + 逐场签到」，品课的「签到码」。
       任一动作存在就显示这一页。 */
    attendance: { label: '考勤', group: '内容', needsPlatform: true,
                  requiresAnyAction: ['attendance', 'signin'] },
    bank: { label: '题库', group: '内容', needsPlatform: true },
    logs: { label: '运行日志', group: '分析', needsPlatform: false },
    stats: { label: '统计', group: '分析', needsPlatform: false },
    accounts: { label: '账号', group: '系统', needsPlatform: false },
    tools: { label: '工具', group: '系统', needsPlatform: true },
    roadmap: { label: '计划', group: '系统', needsPlatform: false },
    users: { label: '用户', group: '系统', needsPlatform: false, admin: true }
  };
  var PAGE_ORDER = ['overview', 'tasks', 'courses', 'assignments', 'attendance', 'bank',
                    'logs', 'stats', 'accounts', 'tools', 'roadmap', 'users'];
  var GROUP_ORDER = ['工作台', '内容', '分析', '系统'];

  function parseHash() {
    var raw = String(location.hash || '').replace(/^#\/?/, '');
    var parts = raw.split('/');
    var page = parts[0] || 'overview';
    if (PAGE_ALIAS[page]) { page = PAGE_ALIAS[page]; }
    if (!PAGES[page]) { page = 'overview'; }
    var plat = '';
    if (parts.length > 1) {
      try { plat = decodeURIComponent(parts[1]); } catch (e) { plat = parts[1]; }
    }
    return { page: page, platform: plat || null };
  }

  function hashFor(page, platform) {
    return '#/' + (PAGES[page] ? page : 'overview') +
      (platform ? '/' + encodeURIComponent(platform) : '');
  }

  function go(page, platform) {
    var target = hashFor(page, platform === undefined ? state.platform : platform);
    if (location.hash === target) { route(); return; }
    location.hash = target;
  }

  function platformOf(name) {
    for (var i = 0; i < state.platforms.length; i++) {
      if (state.platforms[i].name === name) { return state.platforms[i]; }
    }
    return null;
  }

  function platformLabel(name) {
    var p = platformOf(name);
    return p ? (p.display_name || p.name) : (name || '全部平台');
  }

  /* 某个平台到底有哪些动作 —— 能力型页面（如考勤）据此裁剪。
     失败不抛：拿不到清单时按「没有任何额外能力」处理，页面不出现，
     好过出现一个点了必然 501 的入口。 */
  function loadPlatformActions(platform) {
    if (!platform) { return Promise.resolve(); }
    if (state.platformActions[platform]) { return Promise.resolve(); }
    return get('/api/platforms/' + encodeURIComponent(platform) + '/actions')
      .then(function (body) {
        state.platformActions[platform] = ((body && body.actions) || [])
          .map(function (a) { return a.name; });
        renderRail();
      })
      .catch(function () {
        state.platformActions[platform] = [];
        renderRail();
      });
  }

  /* 当前页在当前平台下能不能用 */
  function pageAvailable(name) {
    var def = PAGES[name];
    if (!def) { return false; }
    if (def.admin && !state.isSuper) { return false; }
    if (def.requiresCapability) {
      var plat = state.platform ? platformOf(state.platform) : null;
      /* 「全部平台」下按能力裁剪的页面照常显示（聚合视图里它们给 gate） */
      if (plat && !(plat.capabilities || {})[def.requiresCapability]) { return false; }
    }
    var wanted = def.requiresAnyAction
      || (def.requiresAction ? [def.requiresAction] : null);
    if (wanted) {
      if (!state.platform) { return false; }
      var names = state.platformActions[state.platform];
      /* 清单还没到手：先不出入口，等它到了再渲染（避免闪现又消失） */
      if (!names) { return false; }
      return wanted.some(function (n) { return names.indexOf(n) >= 0; });
    }
    return true;
  }

  function route() {
    var r = parseHash();
    if (r.platform && state.platformsLoaded && !platformOf(r.platform)) {
      toast('平台「' + r.platform + '」不存在，已回到全部平台');
      r.platform = null;
      if (history.replaceState) { history.replaceState(null, '', hashFor(r.page, null)); }
    }
    if (r.platform !== state.platform) { state.toolsShowAll = false; }
    state.page = r.page;
    state.platform = r.platform;
    closeInspector();
    closeMoreMenu();
    clearTimers();
    assignmentsReload = null;
    /* 动作清单是异步拉的：等它到手再判断「这页能不能用」。
       否则直接打开 #/attendance/pinco 会被误判成不支持、立刻弹回概览。 */
    loadPlatformActions(state.platform).then(function () {
      var wanted = PAGES[state.page] ? PAGES[state.page].label : state.page;
      if (!pageAvailable(state.page)) {
        state.page = 'overview';
        if (history.replaceState) {
          history.replaceState(null, '', hashFor('overview', state.platform));
        }
        toast('「' + wanted + '」在这个平台上不可用，已回到概览');
        renderRail();
        renderPage();
      } else {
        renderRail();
      }
    });
    renderRail();
    renderTopbar();
    loadAccounts();
    renderPage();
  }

  /* ================================================================ 定时器 */

  function every(ms, fn) {
    var id = setInterval(fn, ms);
    state.timers.push(id);
    return id;
  }

  function clearTimers() {
    state.timers.forEach(function (id) { clearInterval(id); });
    state.timers = [];
  }

  /* 统一的后台任务轮询：GET /api/jobs/<id>，拿到快照就回调。
   *
   * handlers（除 intervalMs / guard 外都是可选的）：
   *   onJob(job)       每次快照都回调，含第一次；
   *   onLog(line)      增量日志，按 job.logs 的行数只回调新增的几行；
   *   onDone(job)      status 不再是 running 时回调一次，回调前已经停表；
   *   onError(e, w)    单次请求失败；不自动停，要停就调 w.stop()；
   *   guard()          返回 false 就跳过这一拍（面板已经收起 / 切走）；
   *   intervalMs       默认 1200。
   * 定时器走 every()，所以 clearTimers() 切页时能一并清掉；返回的句柄可以 stop()。
   */
  function watchJob(jobId, handlers) {
    var opts = handlers || {};
    var intervalMs = opts.intervalMs || 1200;
    var seen = 0;
    var running = true;
    var inFlight = false;
    var timer = null;
    var handle = { stop: stop };

    function stop() {
      if (!running) { return; }
      running = false;
      var at = timer === null ? -1 : state.timers.indexOf(timer);
      if (at >= 0) {
        state.timers.splice(at, 1);
        clearInterval(timer);
      }
      timer = null;
    }

    function poll() {
      if (!running || inFlight) { return; }
      if (opts.guard && !opts.guard()) { return; }
      inFlight = true;
      get('/api/jobs/' + encodeURIComponent(jobId)).then(function (job) {
        inFlight = false;
        if (!running || !job) { return; }
        var logs = job.logs || [];
        for (var i = seen; i < logs.length; i++) {
          if (opts.onLog) { opts.onLog(logs[i]); }
        }
        seen = logs.length;
        if (opts.onJob) { opts.onJob(job); }
        if (job.status !== 'running') {
          stop();
          if (opts.onDone) { opts.onDone(job); }
        }
      }).catch(function (e) {
        inFlight = false;
        if (!running) { return; }
        if (opts.onError) { opts.onError(e, handle); }
      });
    }

    timer = every(intervalMs, poll);
    poll();
    return handle;
  }

  /* ================================================================ 认证 */

  function adoptIdentity(body) {
    var u = body && body.user;
    state.user = isObj(u) ? u : null;
    var flag = body ? body.is_superadmin : undefined;
    if (flag !== true && flag !== false) {
      flag = !!(state.user && state.user.role === 'superadmin');
    }
    state.isSuper = flag === true;
    if (!state.isSuper) { state.ownerId = null; }
  }

  function showLogin(reason, message) {
    $('app').hidden = true;
    $('login').hidden = false;
    var err = $('login-err');
    if (message) { err.hidden = false; err.textContent = message; }
    else { err.hidden = true; err.textContent = ''; }
    $('login-note').textContent = reason === 'expired'
      ? '登录状态已失效，请重新登录；你原来的位置会保留。'
      : (reason === 'offline'
        ? '取不到会话状态：确认服务已启动，然后重试登录。'
        : '登录后所有数据按当前用户隔离；超管可切换「归属」查看他人数据。');
    var u = $('login-user');
    if (u && !u.value) { u.focus(); } else if (u) { $('login-pass').focus(); }
  }

  function hideLogin() {
    $('login').hidden = true;
    $('app').hidden = false;
    $('login-pass').value = '';
    $('login-err').hidden = true;
  }

  function onUnauthorized() {
    if (!$('login').hidden) { return; }
    clearTimers();
    state.returnHash = location.hash || state.returnHash;
    state.user = null;
    state.isSuper = false;
    state.ownerId = null;
    state.overview = null;
    showLogin('expired');
  }

  function doLogin(ev) {
    if (ev) { ev.preventDefault(); }
    var username = String($('login-user').value || '').trim();
    var password = String($('login-pass').value || '');
    if (!username || !password) {
      showLogin('boot', '请填写用户名和密码。');
      return;
    }
    var submit = $('login-submit');
    submit.disabled = true;
    submit.textContent = '登录中…';
    post('/api/auth/login', { username: username, password: password }).then(function (body) {
      adoptIdentity({ user: body.user, is_superadmin: body.is_superadmin });
      return enterApp();
    }).catch(function (e) {
      showLogin('boot', e && e.status === 401
        ? '账号或密码不对。'
        : ('登录失败：' + describe(e)));
    }).then(function () {
      submit.disabled = false;
      submit.textContent = '登录';
    });
  }

  function doLogout() {
    post('/api/auth/logout', {}).catch(function () { /* 失败也照样切走 */ }).then(function () {
      clearTimers();
      state.user = null;
      state.isSuper = false;
      state.ownerId = null;
      state.overview = null;
      state.returnHash = location.hash;
      showLogin('logout');
    });
  }

  /* ================================================================ 外壳渲染 */

  function renderPlatformSwitch() {
    var nav = $('plat-switch');
    if (!nav) { return; }
    var items = [{ name: null, label: '全部平台', hint: '聚合视图' }]
      .concat(state.platforms.map(function (p) {
        return { name: p.name, label: p.display_name || p.name, hint: p.name, raw: p };
      }));
    var kids = items.map(function (it) {
      var b = el('button', 'plat-pill');
      b.type = 'button';
      b.setAttribute('aria-pressed', String(it.name === state.platform));
      b.title = it.hint;
      if (it.raw) {
        var dot = el('span', 'dot ' + (it.raw.has_credentials ? 'on' : 'off'));
        dot.title = it.raw.has_credentials ? '已配置账号' : '还没有账号';
        b.appendChild(dot);
      }
      b.appendChild(el('span', '', it.label));
      b.addEventListener('click', function () { go(state.page, it.name); });
      return b;
    });
    clear(nav);
    append.apply(null, [nav].concat(kids));
  }

  function renderTopbar() {
    renderPlatformSwitch();
    renderAccountBar();
    var u = state.user || {};
    setText('user-name', u.username || '—');
    setText('user-role', state.isSuper ? '超管' : (u.role === 'user' ? '普通用户' : dash(u.role)));
    setText('user-avatar', (u.username ? String(u.username).slice(0, 1).toUpperCase() : '—'));
  }

  /* --------------------------------------------------- 顶栏：平台账号 --- */

  function loadAccounts() {
    var seq = ++state.accountsSeq;
    var params = state.platform ? { platform: state.platform } : null;
    return get('/api/accounts', params).then(function (body) {
      if (seq !== state.accountsSeq) { return; }
      state.accounts = (body && body.accounts) || [];
      state.accountsLoaded = true;
      renderAccountBar();
    }).catch(function (e) {
      if (seq !== state.accountsSeq) { return; }
      state.accounts = [];
      state.accountsLoaded = true;
      renderAccountBar();
    });
  }

  /* 账号有任何增删改：账号列表、平台 has_credentials、概览都必须重读 ——
     否则「已经登录了」但首页还说「还没有账号」，就是因为这些数据是启动时缓存的。 */
  function refreshAfterAccountChange() {
    var seq = state.pageSeq;
    return Promise.all([loadAccounts(), loadGlobals()]).then(function () {
      renderTopbar();
      renderRail();
      if (state.pageSeq !== seq) { return; }
      if (state.page === 'overview' || state.page === 'accounts') { renderPage(); }
    });
  }

  function activeAccount() {
    for (var i = 0; i < state.accounts.length; i++) {
      if (state.accounts[i].is_active) { return state.accounts[i]; }
    }
    return null;
  }

  function accountsOf(platform) {
    return state.accounts.filter(function (a) { return a.platform === platform; });
  }

  function accountName(a) { return a.display || a.label || a.username || ('#' + a.id); }

  /* 顶栏用的短平台名：去掉括号里的英文/别名，省宽度 */
  function shortPlatform(name) {
    var label = String(platformLabel(name) || name || '');
    return label.replace(/[（(].*?[）)]/g, '').trim() || label;
  }

  /* 一行账号标签：显示名 + 用户名（相同就不重复写） */
  function accountLabel(a) {
    var name = accountName(a);
    return a.username && a.username !== name ? (name + ' · ' + a.username) : name;
  }

  /* 账号切换：当前平台下换号（换号 = 换 cookie）。
     「全部平台」时按平台分组，选中就把界面切到那个平台。 */
  function renderAccountBar() {
    var wrap = $('acct-switch-wrap');
    var sel = $('acct-switch');
    var chip = $('acct-info');
    var nameEl = $('acct-info-name');
    var userEl = $('acct-info-user');
    if (!wrap || !sel || !chip) { return; }

    var scoped = state.platform
      ? accountsOf(state.platform)
      : state.accounts.slice();
    var enabled = state.accountsLoaded && scoped.length > 0;

    /* 只有「一个平台 + 至少两个号」时才需要切换器；其余情况由信息 chip 承担 */
    wrap.hidden = !state.accountsLoaded || !state.platform || scoped.length < 2;
    sel.disabled = !enabled;
    chip.hidden = !state.accountsLoaded;
    closeAcctMenu();

    clear(sel);
    if (!state.platform) {
      sel.title = '先在顶栏选一个平台，才能切换该平台的账号';
    } else if (!scoped.length) {
      var empty = el('option', '', '还没有账号');
      empty.value = '';
      sel.appendChild(empty);
      sel.title = '这个平台还没有账号，去「账号」页添加';
    } else {
      scoped.forEach(function (a) {
        var opt = el('option', '', accountLabel(a));
        opt.value = String(a.id);
        sel.appendChild(opt);
      });
      var act = activeAccount();
      sel.value = act ? String(act.id) : String(scoped[0].id);
      sel.title = '切换当前平台使用的账号';
    }

    var active = activeAccount();
    var fallback = !active && scoped.length ? scoped[0] : null;
    if (fallback) { active = fallback; }
    var hasSwitcher = !wrap.hidden;
    if (state.platform) {
      /* 有切换器时 chip 只说「哪个平台」；没有切换器时它负责报账号 */
      nameEl.textContent = shortPlatform(state.platform);
      userEl.textContent = hasSwitcher ? '' : (active ? accountLabel(active) : '未登录');
      chip.className = 'acct-chip' +
        (active && active.last_login_ok === false ? ' is-stale' : '') +
        (fallback ? ' is-unset' : '');
      chip.title = active
        ? ((fallback ? '账号未设为当前号（' + accountLabel(active) + '）· 点开切换；' : '当前账号：' + accountLabel(active) + ' · 点开看详情 / 切换'))
        : '这个平台还没有账号：去「账号」页添加并登录';
    } else {
      nameEl.textContent = scoped.length + ' 个账号';
      userEl.textContent = '';
      chip.className = 'acct-chip';
      chip.title = '点开看各平台的账号详情';
    }
    var dot = chip.querySelector('.dot');
    if (dot) {
      dot.className = 'dot ' + (active
        ? (active.last_login_ok === false ? 'warn' : 'ok')
        : (scoped.length ? 'warn' : 'off'));
    }
    chip.setAttribute('aria-expanded', String(!$('acct-menu').hidden));
  }

  function switchAccount(id) {
    if (!id) { return; }
    var target = null;
    state.accounts.forEach(function (a) { if (String(a.id) === String(id)) { target = a; } });
    if (!target) { return; }
    post('/api/accounts/' + encodeURIComponent(id) + '/activate', {}).then(function () {
      toast('已切到 ' + accountName(target) +
        (target.platform !== state.platform ? '（' + platformLabel(target.platform) + '）' : ''));
      var needPlatform = target.platform !== state.platform;
      if (needPlatform) {
        go(state.page, target.platform);
        refreshAfterAccountChange();
      } else {
        refreshAfterAccountChange();
      }
    }).catch(function (e) { toast(describe(e)); });
  }

  /* 账号详情：一个下拉面板，列出当前作用域下的账号，点一行看完整信息。 */
  function openAcctMenu() {
    var menu = $('acct-menu');
    if (!menu) { return; }
    var scoped = state.platform ? accountsOf(state.platform) : state.accounts.slice();
    clear(menu);

    var head = el('div', 'menu-head');
    head.appendChild(el('span', '', state.platform ? platformLabel(state.platform) + ' · 平台账号'
      : '全部平台 · 账号'));
    head.appendChild(btn('管理账号', 'btn-sm btn-ghost', function () {
      closeAcctMenu();
      go('accounts');
    }));
    menu.appendChild(head);

    if (!scoped.length) {
      menu.appendChild(stateBlock('', '还没有账号',
        state.platform ? '去「账号」页加一个，课程 / 作业才有会话可用。'
          : '先选一个平台，或去「账号」页添加。'));
    } else {
      var list = el('div', 'menu-list');
      scoped.forEach(function (a) {
        var row = el('div', 'menu-row' + (a.is_active ? ' is-active' : ''));
        var main = el('div', 'menu-row-main');
        var title = el('div', 'rowx-title');
        title.appendChild(el('span', '', accountName(a)));
        if (a.is_active) { title.appendChild(pill('当前', 'pill-accent')); }
        if (a.last_login_ok === false) { title.appendChild(pill('会话失效', 'pill-danger')); }
        main.appendChild(title);
        var meta = el('div', 'rowx-meta');
        if (!state.platform) { meta.appendChild(el('span', '', platformLabel(a.platform))); }
        meta.appendChild(el('span', 'mono', a.username || '—'));
        meta.appendChild(el('span', '', '上次登录 ' + time(a.last_login_at)));
        main.appendChild(meta);
        row.appendChild(main);

        var side = el('div', 'menu-row-side');
        if (!a.is_active) {
          side.appendChild(btn('切到', 'btn-sm', function () { switchAccount(a.id); }));
        }
        side.appendChild(btn('详情', 'btn-sm btn-ghost', function () {
          closeAcctMenu();
          openAccountDetail(a);
        }));
        row.appendChild(side);
        list.appendChild(row);
      });
      menu.appendChild(list);
    }
    menu.hidden = false;
    $('acct-info').setAttribute('aria-expanded', 'true');
  }

  function closeAcctMenu() {
    var menu = $('acct-menu');
    if (menu) { menu.hidden = true; }
    var chip = $('acct-info');
    if (chip) { chip.setAttribute('aria-expanded', 'false'); }
  }

  function openAccountDetail(a) {
    var active = a.is_active;
    var body = el('div', '');
    body.appendChild(defs([
      ['账号', accountName(a), 'strong'],
      ['平台', platformLabel(a.platform) + '（' + a.platform + '）'],
      ['用户名', a.username || '—'],
      ['当前号', active ? '是' : '否'],
      ['创建时间', time(a.created_at)],
      ['上次登录', time(a.last_login_at)],
      ['登录结果', a.last_login_ok === false ? '失败' : (a.last_login_ok ? '成功' : '—')],
      ['备注', a.last_login_note || '—'],
      ['account id', a.id]
    ]));
    var foot = [];
    if (!active) {
      foot.push(btn('设为当前号', 'btn-primary btn-sm', function () {
        closeInspector();
        switchAccount(a.id);
      }));
    }
    foot.push(btn('重新登录', 'btn-sm', function () { openRelogin(a); }));
    foot.push(btn('关闭', 'btn-sm btn-ghost', closeInspector));
    openInspector({
      title: '平台账号详情',
      sub: platformLabel(a.platform) + ' · ' + accountName(a),
      body: body,
      foot: foot
    });
  }

  function setText(id, text) {
    var node = $(id);
    if (node) { node.textContent = dash(text); }
  }

  /* 超管的归属筛选：从顶栏移到概览页的动作区，仍然影响全局数据作用域。 */
  function ownerSelect() {
    if (!state.isSuper) { return null; }
    var sel = el('select', 'owner-select mono');
    sel.title = '超管：按归属用户过滤数据；留空看全部';
    var value = state.ownerId === null ? '' : String(state.ownerId);
    var all = el('option', '', '全部用户');
    all.value = '';
    sel.appendChild(all);
    state.users.forEach(function (u) {
      var o = el('option', '', u.username || ('#' + u.id));
      o.value = String(u.id);
      sel.appendChild(o);
    });
    sel.value = value;
    sel.disabled = !state.users.length;
    sel.addEventListener('change', function () {
      state.ownerId = sel.value === '' ? null : sel.value;
      state.overview = null;
      toast(sel.value === '' ? '归属：全部用户'
        : ('归属：' + sel.options[sel.selectedIndex].text));
      loadGlobals().then(renderPage);
    });
    return sel;
  }

  /* 手机端「更多」菜单：顶栏放不下主题、身份、退出，收进这里。
     桌面端这个按钮是隐藏的，菜单也不会被打开。 */
  function openMoreMenu() {
    var menu = $('more-menu');
    if (!menu) { return; }
    clear(menu);
    var u = state.user || {};

    var head = el('div', 'menu-head');
    head.appendChild(el('span', '', u.username || '未登录'));
    head.appendChild(pill(state.isSuper ? '超管' : (u.role === 'user' ? '普通用户' : dash(u.role)),
      state.isSuper ? 'pill-accent' : 'pill-mute'));
    menu.appendChild(head);

    var list = el('div', 'menu-list');
    function item(label, hint, onClick) {
      var row = el('button', 'menu-row');
      row.type = 'button';
      row.setAttribute('role', 'menuitem');
      var main = el('div', 'menu-row-main');
      main.appendChild(el('div', 'rowx-title', label));
      if (hint) { main.appendChild(el('div', 'rowx-meta', hint)); }
      row.appendChild(main);
      row.addEventListener('click', onClick);
      list.appendChild(row);
    }
    item('切换深浅色', '当前：' + (state.theme === 'light' ? '浅色' : '深色'), function () {
      closeMoreMenu();
      toggleTheme();
    });
    item('刷新', '重读平台与运行状态', function () {
      closeMoreMenu();
      loadGlobals().then(function () { toast('已刷新'); renderPage(); });
    });
    if (state.superadmin !== false && state.isSuper) {
      item('用户管理', '超管', function () { closeMoreMenu(); go('users'); });
    }
    item('退出登录', '清会话', function () { closeMoreMenu(); doLogout(); });
    menu.appendChild(list);

    menu.hidden = false;
    var btn = $('more-btn');
    if (btn) { btn.setAttribute('aria-expanded', 'true'); }
  }

  function closeMoreMenu() {
    var menu = $('more-menu');
    if (menu) { menu.hidden = true; }
    var btn = $('more-btn');
    if (btn) { btn.setAttribute('aria-expanded', 'false'); }
  }

  function renderRail() {
    var rail = $('rail');
    if (!rail) { return; }
    var kids = [];
    GROUP_ORDER.forEach(function (group) {
      var pages = PAGE_ORDER.filter(function (name) {
        return PAGES[name].group === group && pageAvailable(name);
      });
      if (!pages.length) { return; }
      var box = el('div', 'rail-group');
      box.appendChild(el('div', 'rail-label', group));
      pages.forEach(function (name) {
        var def = PAGES[name];
        var item = el('button', 'nav-item');
        item.type = 'button';
        item.title = def.label + (def.needsPlatform && !state.platform ? '（需要先选平台）' : '');
        if (name === state.page) { item.setAttribute('aria-current', 'page'); }
        if (def.needsPlatform && !state.platform) { item.classList.add('needs-plat'); }
        item.appendChild(icon(name));
        item.appendChild(el('span', 'lbl', def.label));
        var count = navCount(name);
        if (count !== null) { item.appendChild(el('span', 'count', count)); }
        item.addEventListener('click', function () { go(name); });
        box.appendChild(item);
      });
      kids.push(box);
    });

    var foot = el('div', 'rail-foot');
    var refresh = btn('刷新', 'btn-ghost btn-sm', function () {
      loadGlobals().then(function () { toast('已重新读取平台与运行状态'); renderPage(); });
    });
    refresh.title = '重新拉取平台列表与运行状态';
    foot.appendChild(refresh);
    var tip = el('div', 'rail-tip');
    tip.appendChild(el('span', '', '命令面板'));
    tip.appendChild(el('span', 'kbd', '⌘K'));
    foot.appendChild(tip);
    kids.push(foot);

    clear(rail);
    append.apply(null, [rail].concat(kids));
  }

  function navCount(name) {
    if (name === 'tasks') {
      var running = state.jobs.filter(function (j) { return j.status === 'running'; }).length;
      return running ? String(running) : null;
    }
    return null;
  }

  /* ================================================================ 页头 */

  function pageHead(opts) {
    var head = el('div', 'page-head');
    var titles = el('div', 'titles');
    var title = el('h1', 'page-title', opts.title);
    if (opts.badge) { title.appendChild(opts.badge); }
    titles.appendChild(title);

    var sub = el('div', 'page-sub');
    var parts = (opts.sub || []).filter(function (part) {
      return part instanceof Node || (part !== null && part !== undefined && String(part).trim() !== '');
    });
    parts.forEach(function (part, i) {
      if (i) { sub.appendChild(el('span', 'crumb-sep', '/')); }
      if (part instanceof Node) { sub.appendChild(part); }
      else { sub.appendChild(el('span', '', String(part))); }
    });
    titles.appendChild(sub);
    head.appendChild(titles);

    if (opts.actions && opts.actions.length) {
      var acts = el('div', 'page-actions');
      opts.actions.forEach(function (a) { acts.appendChild(a); });
      head.appendChild(acts);
    }
    return head;
  }

  function scopeChip() {
    if (!state.platform) { return dotPill('全部平台 · 聚合视图', 'pill-mute'); }
    var p = platformOf(state.platform);
    var kind = p && p.has_credentials ? 'pill-ok' : 'pill-warn';
    return dotPill(platformLabel(state.platform), kind);
  }

  function hintBlock(lines) {
    var details = el('details', 'hints');
    details.appendChild(el('summary', '', '页面说明'));
    var body = el('div', 'hints-body');
    body.appendChild(Array.isArray(lines) ? listOf(lines) : el('p', '', lines));
    details.appendChild(body);
    return details;
  }

  function listOf(items) {
    var ul = el('ul');
    (items || []).filter(function (t) {
      return t instanceof Node || (t !== null && t !== undefined && String(t).trim() !== '');
    }).forEach(function (t) {
      var li = el('li');
      if (t instanceof Node) { li.appendChild(t); } else { li.textContent = String(t); }
      ul.appendChild(li);
    });
    return ul;
  }

  function code(text) { return el('code', '', text); }

  /* ================================================================ 信封渲染 */

  /* 通用动作响应：kind = table / detail / message */
  function renderEnvelope(env, opts) {
    var box = el('div', 'envelope');
    if (!isObj(env)) { return el('div', 'state', '没有可展示的结果'); }
    opts = opts || {};

    if (!opts.hideHead && (env.title || env.note)) {
      var head = el('div', 'env-head');
      if (env.title) { head.appendChild(el('span', 'env-title', env.title)); }
      if (env.label) { head.appendChild(pill(env.label, 'pill-mute')); }
      if (env.platform) { head.appendChild(pill(env.platform, 'pill-accent')); }
      box.appendChild(head);
    }
    if (env.note) { box.appendChild(el('p', 'env-note', env.note)); }

    if (env.kind === 'message' || (!env.rows && !env.fields && env.message)) {
      box.appendChild(el('div', 'env-msg', env.message || '（无内容）'));
      return box;
    }
    if (env.kind === 'detail') {
      if (env.fields && env.fields.length) {
        box.appendChild(renderFields(env.fields));
      }
      (env.sections || []).forEach(function (section) {
        box.appendChild(renderSection(section));
      });
      return box;
    }
    if (env.rows) {
      box.appendChild(renderTable(env.columns || [], env.rows, opts));
      return box;
    }
    if (env.fields) { box.appendChild(renderFields(env.fields)); return box; }
    box.appendChild(el('div', 'env-msg', '这个动作没有返回结构化内容。'));
    return box;
  }

  function renderFields(fields) {
    var dl = el('dl', 'fields-grid');
    fields.forEach(function (f) {
      var wrap = el('div', 'def');
      wrap.appendChild(el('dt', '', dash(f.label)));
      var dd = el('dd', '', dash(f.value));
      wrap.appendChild(dd);
      dl.appendChild(wrap);
    });
    return dl;
  }

  function renderSection(section) {
    var box = el('div', 'section');
    if (section.label) { box.appendChild(el('div', 'section-title', section.label)); }
    if (section.kind === 'text') {
      box.appendChild(el('pre', 'code', section.text || ''));
    } else if (section.kind === 'table') {
      box.appendChild(renderTable(section.columns || [], section.rows || []));
    } else if (section.text) {
      box.appendChild(el('pre', 'code', section.text));
    } else if (section.fields) {
      box.appendChild(renderFields(section.fields));
    }
    return box;
  }

  /* 表格：列声明里 mono 的用等宽；超长文本优先换行而不是撑破 */
  function renderTable(columns, rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) {
      return stateBlock('', '没有数据', opts.empty || '这个动作返回了 0 行。');
    }
    var wrap = el('div', 'tbl-wrap');
    var table = el('table', 'tbl');
    var thead = el('thead');
    var htr = el('tr');
    columns.forEach(function (c) {
      var th = el('th', c.mono ? 'nowrap' : '', c.label || c.key);
      htr.appendChild(th);
    });
    if (opts.rowAction) { htr.appendChild(el('th', '', '')); }
    thead.appendChild(htr);
    table.appendChild(thead);

    /* 大列表先只渲染前 N 行：U校园一个学期的作业能有 2900+ 行，
       全量建 DOM 要 4 秒、还占内存。前 N 行足够扫读，需要时再展开。 */
    var LIMIT = opts.maxRows === undefined ? 200 : opts.maxRows;
    var shown = rows;
    var rest = 0;
    if (LIMIT > 0 && rows.length > LIMIT) {
      shown = rows.slice(0, LIMIT);
      rest = rows.length - LIMIT;
    }

    var tbody = el('tbody');
    shown.forEach(function (row) {
      if (opts.rowPatch) { row = opts.rowPatch(row) || row; }
      var tr = el('tr');
      if (opts.onRow) {
        tr.className = 'row-click';
        tr.addEventListener('click', function (ev) {
          if (ev.target.closest('button')) { return; }
          opts.onRow(row);
        });
      }
      columns.forEach(function (c) {
        var cell = el('td', (c.mono ? 'nowrap mono' : 'wrap'));
        var v = row[c.key];
        if (c.key === 'status' || c.key === 'result') {
          cell.appendChild(statusPill(v));
        } else {
          cell.textContent = dash(v);
        }
        tr.appendChild(cell);
      });
      if (opts.rowAction) {
        var td = el('td', 'nowrap');
        td.appendChild(opts.rowAction(row));
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    if (rest > 0) {
      var more = el('div', 'pills');
      more.appendChild(pill('已显示前 ' + LIMIT + ' 行，还有 ' + rest + ' 行', 'pill-warn'));
      more.appendChild(btn('显示全部 ' + rows.length + ' 行', 'btn-sm', function () {
        var expanded = renderTable(columns, rows,
          Object.assign({}, opts, { maxRows: 0 }));
        if (wrap.parentNode) { wrap.parentNode.replaceChild(expanded, wrap); }
      }));
      var bar = el('div', 'panel-foot');
      bar.appendChild(more);
      var box = el('div', '');
      box.appendChild(wrap);
      box.appendChild(bar);
      box.__tableWrap = wrap;
      return box;
    }
    return wrap;
  }

  var STATUS_MAP = {
    running: ['运行中', 'pill-accent'],
    succeeded: ['成功', 'pill-ok'],
    failed: ['失败', 'pill-danger'],
    pending: ['待处理', 'pill-warn'],
    skipped: ['跳过', 'pill-mute'],
    ok: ['成功', 'pill-ok']
  };

  function statusPill(value) {
    var spec = STATUS_MAP[value];
    if (!spec) { return pill(dash(value), 'pill-mute'); }
    return dotPill(spec[0], spec[1]);
  }

  /* ================================================================ 检查器 */

  function openInspector(opts) {
    setText('inspector-title', opts.title || '详情');
    setText('inspector-sub', opts.sub || '');
    var body = clear($('inspector-body'));
    if (opts.body) { body.appendChild(opts.body); }
    var foot = clear($('inspector-foot'));
    if (opts.foot && opts.foot.length) {
      opts.foot.forEach(function (n) { foot.appendChild(n); });
      foot.hidden = false;
    } else {
      foot.hidden = true;
    }
    $('inspector').hidden = false;
    var close = $('inspector-close');
    if (close) { close.focus(); }
  }

  function closeInspector() {
    var node = $('inspector');
    if (node) { node.hidden = true; }
  }

  /* ================================================================ 命令面板 */

  function paletteItems() {
    var items = [];
    PAGE_ORDER.forEach(function (name) {
      var def = PAGES[name];
      if (!pageAvailable(name)) { return; }
      items.push({
        label: def.label,
        kind: '功能',
        hint: def.group,
        run: function () { go(name); }
      });
    });
    state.platforms.forEach(function (p) {
      items.push({
        label: p.display_name || p.name,
        kind: '平台',
        hint: p.name + (p.has_credentials ? '' : ' · 无账号'),
        run: function () { go(state.page, p.name); }
      });
    });
    items.push({
      label: '全部平台（聚合视图）',
      kind: '平台',
      hint: '清除平台筛选',
      run: function () { go(state.page, null); }
    });
    items.push({ label: '切换深浅色', kind: '界面', hint: state.theme, run: toggleTheme });
    items.push({
      label: '刷新平台与运行状态',
      kind: '动作',
      hint: '重新拉取',
      run: function () { loadGlobals().then(function () { toast('已刷新'); renderPage(); }); }
    });
    items.push({ label: '退出登录', kind: '账号', hint: '清会话', run: doLogout });
    return items;
  }

  var paletteState = { items: [], active: 0 };

  function openPalette() {
    $('palette').hidden = false;
    var input = $('palette-input');
    input.value = '';
    paletteState.items = paletteItems();
    paletteState.active = 0;
    renderPalette('');
    input.focus();
  }

  function closePalette() {
    $('palette').hidden = true;
  }

  function renderPalette(query) {
    var list = clear($('palette-list'));
    var q = String(query || '').trim().toLowerCase();
    var items = paletteState.items.filter(function (it) {
      return !q || (it.label + ' ' + it.kind + ' ' + (it.hint || '')).toLowerCase().indexOf(q) >= 0;
    });
    paletteState.filtered = items;
    if (!items.length) {
      list.appendChild(el('div', 'palette-empty', '没有匹配项'));
      return;
    }
    if (paletteState.active >= items.length) { paletteState.active = 0; }
    items.forEach(function (it, i) {
      var b = el('button', 'palette-item' + (i === paletteState.active ? ' active' : ''));
      b.type = 'button';
      b.appendChild(el('span', '', it.label));
      b.appendChild(el('span', 'kind', it.kind + (it.hint ? ' · ' + it.hint : '')));
      b.addEventListener('click', function () { closePalette(); it.run(); });
      list.appendChild(b);
    });
  }

  function toggleTheme() {
    applyTheme(state.theme === 'light' ? 'dark' : 'light');
  }

  /* ================================================================ 全局数据 */

  function loadGlobals() {
    var tasks = [];
    tasks.push(get('/api/platforms').then(function (body) {
      state.platforms = (body && body.platforms) || [];
      state.platformsLoaded = true;
      setConn(true);
    }));
    tasks.push(get('/api/overview').then(function (body) {
      state.overview = body;
    }));
    tasks.push(get('/api/jobs', state.platform ? { platform: state.platform } : null)
      .then(function (body) {
        state.jobs = (body && body.jobs) || [];
      }).catch(function () { /* 任务列表失败不该挡住整页 */ }));
    if (state.isSuper) {
      tasks.push(get('/api/users').then(function (body) {
        state.users = (body && body.users) || [];
      }).catch(function () { state.users = []; }));
    }
    return Promise.all(tasks.map(function (p) {
      return p.catch(function (e) {
        if (e && e.status === 401) { return null; }
        setConn(false);
        return null;
      });
    }));
  }

  function enterApp() {
    hideLogin();
    return loadGlobals().then(function () {
      var back = state.returnHash;
      state.returnHash = '';
      if (back && back !== location.hash && history.replaceState) {
        history.replaceState(null, '', back);
      }
      renderRail();
      renderTopbar();
      loadAccounts();
      loadPlatformActions(state.platform);
      route();
      if (window.innerWidth <= 1080) { $('app').classList.add('rail-min'); }
    });
  }

  /* ================================================================ 通用块 */

  function panel(title, body, opts) {
    opts = opts || {};
    var box = el('section', 'panel');
    if (title !== null && title !== undefined) {
      var head = el('div', 'panel-head');
      if (title instanceof Node) { head.appendChild(title); }
      else { head.appendChild(el('h2', '', String(title))); }
      if (opts.aside) { head.appendChild(opts.aside); }
      box.appendChild(head);
    }
    var bodyWrap = el('div', 'panel-body' + (opts.flush ? ' flush' : ''));
    if (body) { bodyWrap.appendChild(body); }
    box.appendChild(bodyWrap);
    if (opts.foot) {
      var foot = el('div', 'panel-foot');
      /* 这里要判断的是传进来的 opts.foot —— 判断刚创建的 foot 永远为真，
         传字符串时会 appendChild 一个字符串并抛错。 */
      if (opts.foot instanceof Node) { foot.appendChild(opts.foot); }
      else { foot.textContent = String(opts.foot); }
      box.appendChild(foot);
    }
    return box;
  }

  function kpis(items) {
    var grid = el('div', 'kpis');
    items.forEach(function (it) {
      var card = el('div', 'kpi');
      card.appendChild(el('div', 'kpi-label', it.label));
      var value = el('div', 'kpi-value');
      value.appendChild(document.createTextNode(String(it.value)));
      if (it.unit) { value.appendChild(el('small', '', it.unit)); }
      card.appendChild(value);
      if (it.sub) {
        card.appendChild(el('div', 'kpi-sub' + (it.muted ? ' muted' : ''), it.sub));
      }
      grid.appendChild(card);
    });
    return grid;
  }

  function defs(pairs) {
    var dl = el('dl', 'defs');
    pairs.forEach(function (pair) {
      if (!pair) { return; }
      var row = el('div', 'def');
      row.appendChild(el('dt', '', pair[0]));
      var dd = el('dd', pair[2] || '');
      if (pair[1] instanceof Node) { dd.appendChild(pair[1]); } else { dd.textContent = dash(pair[1]); }
      row.appendChild(dd);
      dl.appendChild(row);
    });
    return dl;
  }

  function bars(items, opts) {
    opts = opts || {};
    var total = items.reduce(function (sum, it) { return sum + (Number(it.value) || 0); }, 0);
    var box = el('div', 'bars');
    if (!items.length || total <= 0) {
      box.appendChild(el('div', 'state', '暂无分布数据'));
      return box;
    }
    items.forEach(function (it) {
      var row = el('div', 'bar-item');
      row.appendChild(el('div', 'bar-label', it.label));
      var track = el('div', 'bar-track');
      var fill = el('span', 'bar-fill' + (it.kind ? ' ' + it.kind : ''));
      var ratio = Math.max(2, Math.round(((Number(it.value) || 0) / total) * 100));
      fill.classList.add('w-' + Math.min(100, Math.max(0, Math.round(ratio / 2) * 2)));
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('div', 'bar-num', opts.percent
        ? pct((Number(it.value) || 0) / total, 0)
        : String(it.value)));
      box.appendChild(row);
    });
    return box;
  }

  /* 完成度进度条：宽度只能靠 .w-XX 阶梯类（CSP 下没有内联 style） */
  function completionProgress(label, done, total, caption, unit) {
    var totalNum = Number(total) || 0;
    var doneNum = Number(done) || 0;
    var pct = totalNum > 0 ? Math.round((doneNum / totalNum) * 100) : 0;
    pct = Math.max(0, Math.min(100, pct));
    var step = Math.round(pct / 2) * 2;

    var box = el('div', 'progress ' +
      (pct >= 80 ? 'is-ok' : (pct >= 40 ? 'is-warn' : 'is-low')));
    var head = el('div', 'progress-head');
    head.appendChild(el('span', 'progress-label', label));
    var value = el('span', 'progress-value', String(doneNum));
    value.appendChild(el('span', 'unit', '/ ' + totalNum + ' ' + (unit || '点')));
    head.appendChild(value);
    box.appendChild(head);

    var track = el('div', 'progress-track');
    track.appendChild(el('span', 'progress-fill w-' + step));
    box.appendChild(track);

    if (caption) { box.appendChild(el('div', 'progress-caption', caption)); }
    return box;
  }

  var CAP_LABEL = {
    login: '登录', list_courses: '课程', list_assignments: '作业',
    fetch_questions: '取题', submit: '提交', course_playback: '回放'
  };
  var CAP_ORDER = ['login', 'list_courses', 'list_assignments', 'fetch_questions', 'submit', 'course_playback'];

  function capChips(caps) {
    var box = el('div', 'pills');
    var keys = CAP_ORDER.filter(function (k) { return has(caps || {}, k); });
    Object.keys(caps || {}).forEach(function (k) {
      if (keys.indexOf(k) < 0) { keys.push(k); }
    });
    if (!keys.length) { box.appendChild(pill('未声明能力', 'pill-mute')); return box; }
    keys.forEach(function (k) {
      var on = caps[k] === true;
      box.appendChild(dotPill(CAP_LABEL[k] || k, on ? 'pill-ok' : 'pill-mute'));
    });
    return box;
  }

  function actionRow(actions) {
    var row = el('div', 'field-actions');
    actions.forEach(function (a) { if (a) { row.appendChild(a); } });
    return row;
  }

  function needsPlatformPage(page, seq) {
    if (state.platform) { return false; }
    page.appendChild(pageHead({
      title: PAGES[state.page].label,
      sub: ['这个功能需要具体平台'],
      actions: []
    }));
    var picks = el('div', 'state-actions');
    state.platforms.slice(0, 4).forEach(function (p) {
      picks.appendChild(btn(p.display_name || p.name, 'btn-sm', function () {
        go(state.page, p.name);
      }));
    });
    page.appendChild(gateBlock('顶栏切换到一个平台后，这里会显示它的' + PAGES[state.page].label + '数据。', []));
    var box = page.querySelector('.state');
    if (box && picks.childNodes.length) {
      box.appendChild(el('div', 'state-actions')).appendChild(picks);
    }
    return true;
  }

  /* ================================================================ 概览 */

  function pageOverview(page, seq) {
    var o = state.overview;
    var scopeName = state.platform ? platformLabel(state.platform) : '全部平台';

    var actions = [];
    var owner = ownerSelect();
    if (owner) { actions.push(owner); }
    actions.push(btn('刷新', 'btn-sm', function () {
      loadGlobals().then(function () { renderPage(); });
    }));

    page.appendChild(pageHead({
      title: '概览',
      badge: pill('v2 DEMO', 'pill-accent'),
      sub: [scopeChip(), '运行参数与当前任务'],
      actions: actions
    }));

    if (!o) {
      page.appendChild(panel(null, loadingBlock()));
      return;
    }

    var bank = o.bank || {};
    var runs = o.runs || {};
    var plat = state.platform ? platformOf(state.platform) : null;
    var caps = plat ? (plat.capabilities || {}) : {};
    var onCaps = Object.keys(caps).filter(function (k) { return caps[k] === true; }).length;

    var current = state.jobs.filter(function (j) { return j.status === 'running'; })[0] || null;

    var items = [
      { label: '平台', value: state.platform ? platformLabel(state.platform) : o.platform_count,
        unit: state.platform ? '' : ' 个',
        sub: state.platform
          ? (plat && plat.has_credentials ? '已配置账号' : '还没有账号')
          : ((o.credentialed_platforms || []).length + ' 个已配置账号') },
      { label: '题库', value: num(bank.total),
        sub: bank.accuracy === null || bank.accuracy === undefined
          ? '还没有判分记录'
          : ('准确率 ' + pct(bank.accuracy, 1) + ' · 已判分 ' + num(bank.graded)) },
      { label: '跑批记录', value: num(runs.total),
        sub: '成功 ' + num(runs.succeeded) + ' · 失败 ' + num(runs.failed) + ' · 跳过 ' + num(runs.skipped) },
      { label: '当前任务', value: current ? '运行中' : '空闲', muted: !current,
        sub: current ? (current.job_id + ' · ' + platformLabel(current.platform)) : '没有进行中的后台任务' }
    ];
    page.appendChild(kpis(items));

    var split = el('div', 'split');
    var main = el('div', 'main-col');
    var aside = el('div', 'aside');

    /* --- 当前任务 / 最近任务 --- */
    var taskBody;
    if (!state.jobs.length) {
      taskBody = stateBlock('', '还没有后台任务',
        '「作业」页的一键提交与刷课任务会出现在这里；任务在服务端跑，切页不会中断。',
        [btn('去作业页', 'btn-sm', function () { go('assignments'); })]);
    } else {
      var list = el('div', 'list');
      state.jobs.slice(0, 4).forEach(function (job) {
        list.appendChild(jobRow(job));
      });
      taskBody = list;
    }
    main.appendChild(panel('任务', taskBody, {
      flush: !state.jobs.length,
      aside: btn('全部任务', 'btn-sm btn-ghost', function () { go('tasks'); })
    }));

    /* --- 计划任务：明确标出「还没做」的功能，避免看起来像坏了 --- */
    var planList = el('div', 'list');
    ROADMAP.slice(0, 3).forEach(function (item) { planList.appendChild(roadmapRow(item)); });
    main.appendChild(panel('计划任务', planList, {
      flush: true,
      aside: btn('全部 ' + ROADMAP.length + ' 项', 'btn-sm btn-ghost', function () { go('roadmap'); })
    }));

    /* --- 能力摘要（只显示当前平台） --- */
    var capBody = state.platform
      ? append(el('div', ''), capChips(caps),
          el('p', 'env-note', '能力由适配器声明；' + (onCaps || 0) + ' / ' + Object.keys(caps).length + ' 项可用。'))
      : stateBlock('', '先选一个平台', '能力按平台各自声明，概览只显示当前平台的那一份。');

    main.appendChild(panel('平台能力', capBody, { aside: pill(state.platform || '全部平台', 'pill-mute') }));


    /* --- 右侧：运行参数 / 依赖 / 题库摘要 --- */
    aside.appendChild(panel('运行参数', defs([
      ['版本', o.version, 'strong'],
      ['环境', o.env],
      ['干跑', o.dry_run === false ? '否（真提交环境）' : (o.dry_run ? '是' : '—')],
      ['Python', o.python],
      ['并发上限', o.max_concurrency],
      ['最低置信度', o.min_confidence],
      ['数据库', o.db_path],
      ['仓库', o.repo_root]
    ])));

    var deps = o.optional_deps || {};
    var depBox = el('div', 'pills');
    Object.keys(deps).forEach(function (k) {
      depBox.appendChild(dotPill(k, deps[k] ? 'pill-ok' : 'pill-mute'));
    });
    aside.appendChild(panel('可选依赖', depBox));

    var llm = o.llm || {};
    aside.appendChild(panel('大模型', defs([
      ['状态', llm.enabled && llm.configured ? '已启用' : '未配置', 'strong'],
      ['模型', llm.model || '—'],
      ['地址', llm.api_base || '—'],
      ['来源', llm.configured_via || '—']
    ])));

    split.appendChild(main);
    split.appendChild(aside);
    page.appendChild(split);

    var oldLink = document.createElement('a');
    oldLink.href = '/';
    oldLink.textContent = '打开原控制台';
    page.appendChild(hintBlock([
      '这是控制台 v2 的排版 demo：平台切换移到顶栏药丸组，详情统一进右侧检查器，说明收进页脚折叠块。',
      append(el('span', '', '数据接口与 v1 完全共用，后端契约没有任何改动。原控制台仍在：'), oldLink),
      '偏好在浏览器本地记住的只有深浅色，不存任何凭据。'
    ]));
  }

  /* ==================================================== 计划任务（未实现） */

  /* 这些功能**还没做**，界面上必须标出来，不能让它看起来像已经可用。
     数据写在前端、不走后端：它们是「计划」，不是运行状态。
     来源：docs/audit-2026-10-20.md、docs/research/voice-eval.md。 */
  var ROADMAP = [
    {
      id: 'voice-eval',
      title: 'U校园口语评测',
      status: '计划中',
      tone: 'pill-warn',
      platform: 'unipus',
      summary: '口语 / 角色扮演题现在只交空录音，平台记 0 分。',
      why: '语音评测模块（platforms/unipus/speech.py）已写但没接进提交链；'
        + '提交时 self_report_scores 默认关闭，record 里没有分数。',
      plan: [
        '阶段 1 · 干跑：补 WAV→PCM 规范化与 200ms 分片、认证帧嵌套、结果结构化解析；'
          + '只算分落盘，不建连接、不需要登录态（签名在本地）。',
        '阶段 2 · 真评：接 WSS 真打分，仍不上报平台；用真实题目核对量纲。',
        '阶段 3 · 回填：把 record 分数写进提交体，首次提交就带上（见下条硬点）。'
      ],
      gaps: [
        '音频规范化：现在只读原始 bytes，没做 16k/mono/16bit 校验（speech.py 音频入口）',
        '认证帧：我们顶层平铺，参考实现是 {sdk:{…}, app:{…}} 且带 userId',
        '分片：整段单帧发出去服务端不回结果，必须 200ms/片（6400 字节）',
        '结束帧：{"stop":true} 默认没发',
        '结果解析：现在拿不到 audioUrl，也没校验 finalResult.result.total',
        '按题注入：提交体里只有 bool self_report_scores，没有逐题 record 的通道',
        '超时：默认 15 秒，参考实现用 120 秒，长题会假失败'
      ],
      risks: [
        '留存策略：record_every_submit=false 时一组只留首次记录 → '
          + '「先交空录音、后补真实分」无效，真实分数必须在首次提交就带上。',
        '量纲待确认：参考项目内部自相矛盾（一处 0~100、一处 0~1），'
          + '必须抓一条真实提交快照确认，别照抄。'
      ],
      refs: ['docs/research/voice-eval.md', 'platforms/unipus/speech.py'],
      decision: '按你的决定：这一步只做前端标注，不接评测。'
    },
    {
      id: 'pinco-live-submit',
      title: '品课真提交（放开写闸门）',
      status: '待抓包确认',
      tone: 'pill-warn',
      platform: 'pinco',
      summary: '复核链已经能走通，但最后一步仍被「提交报文未确认」挡住。',
      why: 'adapter.submit_payload_confirmed = False：守卫放行后仍然拒绝发写请求，'
        + '因为交错的答案比不交更糟。',
      plan: [
        '用真实会话提交一次，抓下 ACTION_SUBMIT_HOMEWORK 的请求体结构。',
        '把字段对齐到 _post_submit 里构造的载荷，确认后把开关设为 True。',
        '开启后先跑一次「计划 → 复核 → 提交」，核对平台侧确实落库。'
      ],
      gaps: ['提交报文结构未由真实样本确认'],
      risks: ['一旦开错，交出去的答案可能是错的；必须先抓包再开。'],
      refs: ['platforms/pinco/adapter.py', 'docs/audit-2026-10-20.md'],
      decision: '等你抓包或授权后再开。'
    },
    {
      id: 'neusoft-images',
      title: '东软图片类资源刷课',
      status: '待排期',
      tone: 'pill-mute',
      platform: 'neusoft',
      summary: '图片（res_type=4）被当成「可完成」，但没有完成链路。',
      why: '刷课时每张图片计一次失败，还会吃掉 limit，完成度永远到不了 100%。',
      plan: [
        '把 IMAGE 移出 COMPLETABLE_RES_TYPES，或在 play_course 里记 skipped、不占 limit。'
      ],
      gaps: ['图片资源的完成链路未实现'],
      risks: ['移除后图片不再计入待完成，课程完成度口径会变。'],
      refs: ['platforms/neusoft/endpoints.py', 'platforms/neusoft/study.py'],
      decision: '小改动，等你确认口径。'
    },
    {
      id: 'neusoft-paging',
      title: '东软列表翻页',
      status: '待排期',
      tone: 'pill-mute',
      platform: 'neusoft',
      summary: '课程 100 条封顶、学习任务 200 条、考勤没有分页。',
      why: '超出部分静默缺失，表现为「没有作业 / 没有考勤」。',
      plan: [
        '按 total 循环取页（或尾页判断）；至少把 total 与已取条数写进日志。'
      ],
      gaps: ['三处列表都只取第一页'],
      risks: ['翻页会显著增加请求数，需要限速。'],
      refs: ['platforms/neusoft/resources.py', 'platforms/neusoft/homework.py'],
      decision: '等你有大批量课程时再做。'
    },
    {
      id: 'dead-routes',
      title: '清理死接口',
      status: '待排期',
      tone: 'pill-mute',
      platform: '',
      summary: '几条没有任何调用方的历史接口。',
      why: 'GET /api/questions、POST /api/run（仅测试在调）、'
        + 'GET /api/essays/jobs/<id>（与 /api/jobs/<id> 同实现）、service.itest_job、/next 别名。',
      plan: [
        '先删 /api/questions 与 itest_job（零风险），再评估 /api/run（要改 15 处测试）。'
      ],
      gaps: ['接口文档与代码对不上'],
      risks: ['/api/run 有测试依赖，删之前要把测试改到 batch-submit。'],
      refs: ['docs/audit-2026-10-20.md'],
      decision: '纯清理，不影响功能。'
    }
  ];

  function roadmapRow(item) {
    var row = el('div', 'rowx');
    row.appendChild(el('span', 'mark ' + (
      item.status === '计划中' ? 'warn' : '')));

    var main = el('div', 'rowx-main');
    var title = el('div', 'rowx-title');
    title.appendChild(el('span', '', item.title));
    title.appendChild(pill(item.status, item.tone || 'pill-mute'));
    if (item.platform) { title.appendChild(pill(item.platform, 'pill-mute')); }
    main.appendChild(title);
    main.appendChild(el('div', 'rowx-meta', item.summary));
    row.appendChild(main);

    var side = el('div', 'rowx-side');
    side.appendChild(btn('看计划', 'btn-sm btn-ghost', function () {
      openRoadmapDetail(item);
    }));
    row.appendChild(side);
    return row;
  }

  function openRoadmapDetail(item) {
    var body = el('div', '');

    body.appendChild(el('p', 'env-note', item.why || ''));

    var planSec = el('div', 'section');
    planSec.appendChild(el('div', 'section-title', '计划'));
    planSec.appendChild(listOf(item.plan || []));
    body.appendChild(planSec);

    if ((item.gaps || []).length) {
      var gapSec = el('div', 'section');
      gapSec.appendChild(el('div', 'section-title', '还差的步骤'));
      gapSec.appendChild(listOf(item.gaps));
      body.appendChild(gapSec);
    }

    if ((item.risks || []).length) {
      var riskSec = el('div', 'section');
      riskSec.appendChild(el('div', 'section-title', '风险 / 必须先确认的事'));
      riskSec.appendChild(listOf(item.risks));
      body.appendChild(riskSec);
    }

    var refSec = el('div', 'section');
    refSec.appendChild(el('div', 'section-title', '出处'));
    var refBox = el('div', 'pills');
    (item.refs || []).forEach(function (r) { refBox.appendChild(pill(r, 'pill-mute')); });
    refSec.appendChild(refBox);
    body.appendChild(refSec);

    if (item.decision) {
      body.appendChild(el('p', 'env-note', item.decision));
    }

    openInspector({
      title: item.title,
      sub: '计划任务 · ' + item.status + (item.platform ? ' · ' + item.platform : ''),
      body: body,
      foot: [btn('关闭', 'btn-sm btn-ghost', closeInspector)]
    });
  }

  /* ======================================================== 计划页 */

  function pageRoadmap(page, seq) {
    var counts = { '计划中': 0, '待抓包确认': 0, '待排期': 0 };
    ROADMAP.forEach(function (item) {
      if (has(counts, item.status)) { counts[item.status] += 1; }
    });

    page.appendChild(pageHead({
      title: '计划',
      badge: pill('未实现 · 仅标注', 'pill-warn'),
      sub: ['这些功能还没做', '界面上标出来，是为了不让人以为「坏了」']
    }));

    page.appendChild(kpis([
      { label: '计划任务', value: ROADMAP.length, sub: '全部未完成' },
      { label: '计划中', value: counts['计划中'], sub: '已定方案、等排期' },
      { label: '待抓包确认', value: counts['待抓包确认'], sub: '卡在真实样本' },
      { label: '待排期', value: counts['待排期'], muted: true }
    ]));

    /* 按状态分组：计划中 → 待抓包确认 → 待排期 */
    ['计划中', '待抓包确认', '待排期'].forEach(function (status) {
      var items = ROADMAP.filter(function (item) { return item.status === status; });
      if (!items.length) { return; }
      var list = el('div', 'list');
      items.forEach(function (item) { list.appendChild(roadmapRow(item)); });
      page.appendChild(panel(status, list, {
        flush: true,
        aside: pill(items.length + ' 项', 'pill-mute')
      }));
    });

    page.appendChild(hintBlock([
      '这一页只是清单：每项都能点开看「为什么没做、计划怎么做、还差哪几步、有什么风险」。',
      '口语评测按当前决定**只做前端标注**，不接评测链路 —— 口语题仍然只交空录音。',
      '其余各项的后端现状见审计报告 docs/audit-2026-10-20.md。'
    ]));
  }

  function jobRow(job) {
    var row = el('div', 'rowx');
    var mark = el('span', 'mark ' + (job.status === 'succeeded' ? 'ok'
      : job.status === 'failed' ? 'err'
      : job.status === 'running' ? 'run' : ''));
    row.appendChild(mark);

    var main = el('div', 'rowx-main');
    var title = el('div', 'rowx-title');
    title.appendChild(el('span', 'mono', job.job_id || '—'));
    title.appendChild(el('span', 'sub', platformLabel(job.platform)));
    if (job.live) { title.appendChild(pill('真提交', 'pill-danger')); }
    else { title.appendChild(pill('干跑', 'pill-warn')); }
    main.appendChild(title);

    var meta = el('div', 'rowx-meta');
    meta.appendChild(el('span', '', '开始 ' + time(job.started_at)));
    meta.appendChild(el('span', '', '耗时 ' + elapsed(job.started_at, job.finished_at)));
    if (job.records && job.records.length) {
      meta.appendChild(el('span', 'mono', '记录 ' + job.records.length));
    }
    if (job.error) { meta.appendChild(el('span', '', truncate(job.error, 60))); }
    main.appendChild(meta);
    row.appendChild(main);

    var side = el('div', 'rowx-side');
    side.appendChild(statusPill(job.status));
    if (job.status === 'running') {
      side.appendChild(btn('停止', 'btn-sm btn-danger', function () {
        post('/api/jobs/' + encodeURIComponent(job.job_id) + '/cancel', {}).then(function () {
          toast('已请求停止 ' + job.job_id);
          renderPage();
        }).catch(function (e) { toast(describe(e)); });
      }));
    }
    if (job.records && job.records.length) {
      var toggle = btn('记录 ' + job.records.length, 'btn-sm btn-ghost', function () {
        var detail = row.querySelector('.rowx-detail');
        if (detail) { detail.hidden = !detail.hidden; return; }
        detail = el('div', 'rowx-detail');
        var inner = el('div', 'list');
        job.records.forEach(function (rec) {
          var r = el('div', 'rowx');
          r.appendChild(el('span', 'mark ' + (rec.status === 'succeeded' ? 'ok'
            : rec.status === 'failed' ? 'err' : rec.status === 'running' ? 'run' : '')));
          var m = el('div', 'rowx-main');
          m.appendChild(el('div', 'rowx-title mono', rec.assignment_id || '—'));
          m.appendChild(el('div', 'rowx-meta', rec.note || ''));
          r.appendChild(m);
          var s = el('div', 'rowx-side');
          s.appendChild(statusPill(rec.status));
          r.appendChild(s);
          inner.appendChild(r);
        });
        detail.appendChild(inner);
        row.appendChild(detail);
      });
      side.appendChild(toggle);
    }
    row.appendChild(side);
    return row;
  }

  /* ================================================================ 任务 */

  function pageTasks(page, seq) {
    page.appendChild(pageHead({
      title: '任务',
      sub: [scopeChip(), '后台任务在服务端运行，切页不中断'],
      actions: [btn('刷新', 'btn-sm', function () { renderPage(); })]
    }));

    var host = el('div', '');
    page.appendChild(host);

    function load() {
      return get('/api/jobs', state.platform ? { platform: state.platform } : null).then(function (body) {
        if (seq !== state.pageSeq) { return; }
        state.jobs = (body && body.jobs) || [];
        renderRail();
        clear(host);
        if (!state.jobs.length) {
          host.appendChild(panel(null, stateBlock('', '还没有后台任务',
            '作业页的「一键全部提交」与刷课任务会在这里出现，包含每次的记录与失败原因。')));
          return;
        }
        var list = el('div', 'list');
        state.jobs.forEach(function (job) { list.appendChild(jobRow(job)); });
        host.appendChild(panel('任务历史', list, {
          flush: true,
          aside: pill(String(state.jobs.length) + ' 条', 'pill-mute')
        }));
        if (state.jobs.some(function (j) { return j.status === 'running'; })) {
          every(1500, function () { load(); });
        }
      }).catch(function (e) {
        if (seq !== state.pageSeq) { return; }
        clear(host);
        host.appendChild(panel(null, errorBlock(e, load)));
      });
    }

    host.appendChild(panel(null, loadingBlock()));
    load();
  }
  /* ============================================================ 刷课工作区 */

  /* 两步按钮：第一次点变成「确认」，第二次才真的执行。 */
  function btnArmored(label, armedLabel, cls, onFire) {
    var armed = false;
    var node = btn(label, cls, function () {
      if (!armed) {
        armed = true;
        node.textContent = armedLabel;
        node.classList.add('btn-primary');
        return;
      }
      armed = false;
      node.disabled = true;
      node.textContent = '执行中…';
      Promise.resolve(onFire()).catch(function () { /* 调用方自己显示错误 */ });
    });
    return node;
  }

  function rowsOfEnvelope(env) {
    if (!isObj(env)) { return []; }
    if (Array.isArray(env.rows)) { return env.rows.filter(isObj); }
    var sections = Array.isArray(env.sections) ? env.sections : [];
    for (var i = 0; i < sections.length; i++) {
      if (isObj(sections[i]) && Array.isArray(sections[i].rows)) {
        return sections[i].rows.filter(isObj);
      }
    }
    return [];
  }

  function pickColumn(row, keys) {
    for (var i = 0; i < keys.length; i++) {
      var v = row[keys[i]];
      if (v !== undefined && v !== null && v !== '') { return String(v); }
    }
    return '';
  }

  function brushWorkspace(platName, host) {
    clear(host);
    var box = el('section', 'panel workspace');

    var head = el('div', 'workspace-head');
    head.appendChild(el('h2', '', '刷课'));
    head.appendChild(pill('写操作', 'pill-danger'));
    head.appendChild(el('span', 'hint-text', '先干跑看计划，确认后再真跑'));
    head.appendChild(el('span', 'spacer'));
    head.appendChild(btn('收起', 'btn-sm btn-ghost', function () { clear(host); }));
    box.appendChild(head);

    var body = el('div', 'panel-body');
    box.appendChild(body);
    host.appendChild(box);

    /* --- 教材列表 --- */
    var booksBox = el('div', 'books');
    var sec = el('div', 'section');
    sec.appendChild(el('div', 'section-title', '教材'));
    sec.appendChild(booksBox);
    body.appendChild(sec);

    var treeHost = el('div', '');
    body.appendChild(treeHost);

    /* --- 参数 --- */
    var minutes = input('number', 10, '');
    minutes.min = '0.5'; minutes.max = '240'; minutes.step = '0.5';
    var rooms = input('number', 6, '');
    rooms.min = '2'; rooms.max = '60'; rooms.step = '1';
    var maxGroups = input('number', 60, '');
    maxGroups.min = '1'; maxGroups.max = '500'; maxGroups.step = '1';
    var submitPending = checkbox(true);
    var onlyRequired = checkbox(true);
    var llmSubjective = checkbox(false);
    var placeholder = checkbox(false);
    var resubmit = checkbox(false);

    var params = el('div', 'form-grid');
    params.appendChild(fieldWrap('刷多长时间（分钟）', minutes, '任务模式 1× 封顶'));
    params.appendChild(fieldWrap('刷时长并发数', rooms, '单房间不记账，6 约 4 倍；>20 可能握手被拒'));
    params.appendChild(fieldWrap('本轮最多提交几组', maxGroups, '防止一口气跑两小时'));
    params.appendChild(fieldWrap('提交未完成任务', submitPending, '只提交没做过的组'));
    params.appendChild(fieldWrap('只处理必修', onlyRequired, '门户进度按必修算'));
    params.appendChild(fieldWrap('翻译/写作用大模型', llmSubjective, '生成内容会写进日志'));
    params.appendChild(fieldWrap('翻译/写作填占位文本', placeholder, '能得完成分，但不是人写的'));
    params.appendChild(fieldWrap('已完成未满分也重交', resubmit, '平台留存首条记录，实测不改分'));
    var secParams = el('div', 'section');
    secParams.appendChild(el('div', 'section-title', '参数'));
    secParams.appendChild(params);
    body.appendChild(secParams);

    /* --- 状态与日志 --- */
    var stateText = el('span', 'hint-text', '还没有选教材');
    var countsText = el('span', 'hint-text', '');
    var logView = el('pre', 'log-view');
    logView.textContent = '刷课日志会出现在这里。';

    var actions = el('div', 'auto-foot');
    var stopBtn = btn('停止任务', 'btn-sm btn-danger', function () {
      var jobId = currentJobId;
      if (!jobId) { return; }
      post('/api/jobs/' + encodeURIComponent(jobId) + '/cancel', {}).then(function () {
        toast('已请求停止 ' + jobId);
      }).catch(function (e) { toast(describe(e)); });
    });
    stopBtn.hidden = true;

    var submitDry = btn('提交未完成（干跑）', 'btn-primary btn-sm', function () {
      start('submit', false);
    });
    var submitLive = btnArmored('提交未完成（真交）', '确认：真交未完成', 'btn-danger btn-sm', function () {
      return start('submit', true);
    });
    var durationDry = btn('刷时长（干跑）', 'btn-primary btn-sm', function () {
      start('duration', false);
    });
    var durationLive = btnArmored('刷时长（真跑）', '确认：真刷时长', 'btn-danger btn-sm', function () {
      return start('duration', true);
    });
    [submitDry, submitLive, durationDry, durationLive, stopBtn].forEach(function (n) {
      actions.appendChild(n);
    });
    body.appendChild(actions);
    stateText.className = 'pill pill-mute';
    countsText.className = 'pill pill-mute';
    var statusRow = el('div', 'pills');
    statusRow.appendChild(stateText);
    statusRow.appendChild(countsText);
    var statusSec = el('div', 'section');
    statusSec.appendChild(el('div', 'section-title', '状态'));
    statusSec.appendChild(statusRow);
    body.appendChild(statusSec);

    var logSec = el('div', 'section');
    logSec.appendChild(el('div', 'section-title', '日志'));
    logSec.appendChild(logView);
    body.appendChild(logSec);

    var selectedId = '';
    var currentJobId = '';
    var alive = true;
    var watcher = null;

    function stopPolling() {
      if (watcher) { watcher.stop(); watcher = null; }
    }

    function setButtonsBusy(busy) {
      [submitDry, submitLive, durationDry, durationLive].forEach(function (b) {
        b.disabled = busy;
        if (!busy) { b.classList.remove('btn-primary'); }
      });
      stopBtn.hidden = !busy;
    }

    function appendLog(line) {
      var row = el('div');
      row.appendChild(el('span', 't', time(line.at).slice(11) + '  '));
      row.appendChild(el('span', line.level || 'info', line.text || ''));
      logView.appendChild(row);
      logView.scrollTop = logView.scrollHeight;
    }

    function watchCurrent(jobId) {
      watcher = watchJob(jobId, {
        intervalMs: 1200,
        guard: function () { return alive && host.isConnected; },
        onLog: appendLog,
        onJob: function (job) {
          var c = job.counts || {};
          countsText.textContent = '提交 ' + (c.submitted || 0) +
            ' · 跳过 ' + (c.skipped || 0) +
            ' · 失败 ' + (c.failed || 0) +
            ' · 时长目标 ' + (c.targets || 0) +
            ' · 记账 ' + (c.credited_seconds || 0) + ' 秒';
          if (job.status === 'running') {
            stateText.textContent = '任务 ' + jobId + ' 运行中…';
            setButtonsBusy(true);
            return;
          }
          setButtonsBusy(false);
        },
        onDone: function (job) {
          if (job.status === 'failed') {
            stateText.textContent = '失败：' + (job.error || '未知错误');
          } else {
            stateText.textContent = job.summary || '完成';
            if (!job.confirm) {
              appendLog({ level: 'info', text: '→ 干跑完成。确认无误后再点对应的真交 / 真跑按钮。' });
            }
          }
        },
        onError: function (e) {
          stateText.textContent = '轮询失败：' + describe(e);
        }
      });
    }

    function start(mode, confirm) {
      if (!selectedId) {
        toast('先选一本教材');
        return undefined;
      }
      currentJobId = '';
      clear(logView);
      logView.textContent = '';
      stateText.textContent = confirm ? '真跑中…' : '干跑中…';
      countsText.textContent = '';
      setButtonsBusy(true);
      var payload = {
        course: selectedId,
        mode: mode,
        minutes: Number(minutes.value) || 10,
        confirm: !!confirm,
        submit_pending: mode === 'submit' ? submitPending.querySelector('input').checked : false,
        only_required: onlyRequired.querySelector('input').checked,
        max_groups: Number(maxGroups.value) || 60,
        rooms: Number(rooms.value) || 6,
        interval: 20,
        llm_subjective: llmSubjective.querySelector('input').checked,
        subjective_placeholder: placeholder.querySelector('input').checked ? '[已提交]' : '',
        resubmit_incomplete: resubmit.querySelector('input').checked
      };
      appendLog({ level: 'info', text: '$ POST /api/platforms/' + platName + '/brush ' +
        truncate(JSON.stringify(payload), 180) });
      return post('/api/platforms/' + encodeURIComponent(platName) + '/brush', payload)
        .then(function (job) {
          currentJobId = job.job_id;
          stateText.textContent = '任务 ' + currentJobId + ' 已启动';
          stopPolling();
          watchCurrent(job.job_id);
        })
        .catch(function (e) {
          setButtonsBusy(false);
          stateText.textContent = '启动失败：' + describe(e);
          appendLog({ level: 'error', text: '✗ ' + describe(e) });
        });
    }

    function renderTree(tree) {
      clear(treeHost);
      if (!tree) { return; }
      var sec = el('div', 'section');
      sec.appendChild(el('div', 'section-title', '教材结构'));
      if (tree.total_points) {
        var captionParts = [
          '必修 ' + (tree.required_count || 0) + ' 组',
          '全部 ' + (tree.group_count || 0) + ' 组'
        ];
        /* 得分点是另一套口径（只统计有计分点的组），作为补充信息放在说明里 */
        if (tree.total_points) {
          captionParts.push('得分点 ' + (tree.finish_points || 0) + '/' + tree.total_points);
        }
        if (tree.class_name) { captionParts.push(tree.class_name); }
        /* 完成度必须按「组」算：finish_points 是**得分点**口径，
           纯学习组（text-learn / 视频）没有计分点，永远是 0 ——
           拿它画进度条会让一本 33 组全做完的书显示 0%。 */
        var reqTotal = 0, reqDone = 0, anyTotal = 0, anyDone = 0, hasState = false;
        (tree.units || []).forEach(function (unit) {
          (unit.groups || []).forEach(function (g) {
            anyTotal += 1;
            if (g.finished !== undefined && g.finished !== null) { hasState = true; }
            if (g.finished) { anyDone += 1; }
            if (g.required) {
              reqTotal += 1;
              if (g.finished) { reqDone += 1; }
            }
          });
        });
        /* state=none 时不回读状态，groups 里没有 finished —— 别画一个假的 0% */
        if (!hasState) {
          var pending = el('div', 'pills');
          pending.appendChild(pill('必修 ' + (tree.required_count || reqTotal) + ' 组', 'pill-mute'));
          pending.appendChild(pill('全部 ' + (tree.group_count || anyTotal) + ' 组', 'pill-mute'));
          pending.appendChild(pill('未读取完成状态', 'pill-warn'));
          sec.appendChild(pending);
        } else if (reqTotal) {
          sec.appendChild(completionProgress('必修完成度', reqDone, reqTotal,
            captionParts.join(' · '), '组'));
        } else {
          sec.appendChild(completionProgress('已完成', anyDone, anyTotal,
            captionParts.join(' · '), '组'));
        }
      } else {
        var summary = el('div', 'pills');
        summary.appendChild(pill('必修 ' + (tree.required_count || 0) + ' 组', 'pill-mute'));
        summary.appendChild(pill('全部 ' + (tree.group_count || 0) + ' 组', 'pill-mute'));
        if (tree.class_name) { summary.appendChild(pill(tree.class_name, 'pill-mute')); }
        sec.appendChild(summary);
      }

      var unitsBox = el('div', 'tree-units');
      (tree.units || []).forEach(function (unit, index) {
        var unitEl = el('details', 'tree-unit');
        if (index === 0) { unitEl.open = true; }
        var sum = el('summary');
        sum.appendChild(el('span', '', unit.name || ('单元 ' + (unit.id || ''))));
        sum.appendChild(el('span', 'count', (unit.groups || []).length + ' 组'));
        unitEl.appendChild(sum);

        var groupsBox = el('div', 'tree-groups');
        (unit.groups || []).forEach(function (g) {
          var row = el('div', 'tree-group');
          var mark = el('span', 'mark ' + (g.finished ? 'ok' : (g.required ? 'todo' : '')));
          row.appendChild(mark);
          var main = el('div', '');
          main.appendChild(el('div', 'g-name', g.name || g.group_name || ('组 ' + (g.id || ''))));
          var meta = el('div', 'g-meta');
          if (g.assignment_id) { meta.appendChild(el('span', 'mono', g.assignment_id)); }
          if (g.total_items) {
            meta.appendChild(el('span', '', '完成 ' + (g.correct_items || 0) + '/' + g.total_items));
          }
          main.appendChild(meta);
          row.appendChild(main);
          var side = el('div', 'g-side');
          if (g.required) { side.appendChild(pill('必修', 'pill-accent')); }
          if (g.finished) { side.appendChild(pill('已完成', 'pill-ok')); }
          if (g.score !== undefined && g.score !== null) {
            side.appendChild(pill(String(g.score) + ' 分', 'pill-mute'));
          }
          row.appendChild(side);
          groupsBox.appendChild(row);
        });
        unitEl.appendChild(groupsBox);
        unitsBox.appendChild(unitEl);
      });
      sec.appendChild(unitsBox);
      treeHost.appendChild(sec);
    }

    function openBook(courseId, name) {
      selectedId = courseId;
      stateText.textContent = '正在读取《' + name + '》的结构…';
      clear(treeHost);
      treeHost.appendChild(loadingBlock());
      get('/api/platforms/' + encodeURIComponent(platName) + '/course-tree',
        { course: courseId }).then(function (tree) {
        stateText.textContent = '已选教材：' + (tree.course || name);
        renderTree(tree);
      }).catch(function (e) {
        stateText.textContent = '教材读取失败：' + describe(e);
        clear(treeHost);
        treeHost.appendChild(stateBlock('error', '读不到这本教材的结构', describe(e)));
      });
    }

    booksBox.appendChild(loadingBlock());
    post('/api/platforms/' + encodeURIComponent(platName) + '/actions/courses', {}).then(function (env) {
      var rows = rowsOfEnvelope(env);
      clear(booksBox);
      if (!rows.length) {
        booksBox.appendChild(stateBlock('', '没有取到教材',
          '这个平台没有返回课程；也可能账号会话已失效。'));
        return;
      }
      rows.forEach(function (row) {
        var name = pickColumn(row, ['name', 'title', 'course_name', 'book_name']) || '未命名教材';
        var cid = pickColumn(row, ['id', 'course', 'course_id', 'remote_id']);
        var item = el('div', 'book-row');
        var main = el('div', '');
        main.appendChild(el('div', 'book-name', name));
        var meta = el('div', 'book-meta');
        if (cid) { meta.appendChild(el('span', 'mono', cid)); }
        var teacher = pickColumn(row, ['teacher', 'teacher_name']);
        if (teacher) { meta.appendChild(el('span', '', teacher)); }
        var term = pickColumn(row, ['term', 'semester', 'class_name']);
        if (term) { meta.appendChild(el('span', '', term)); }
        main.appendChild(meta);
        item.appendChild(main);
        item.appendChild(btn('打开', 'btn-sm', function () { openBook(cid, name); }));
        booksBox.appendChild(item);
      });
      if (rows.length === 1) {
        openBook(pickColumn(rows[0], ['id', 'course', 'course_id', 'remote_id']),
          pickColumn(rows[0], ['name', 'title', 'course_name']) || '教材');
      }
    }).catch(function (e) {
      clear(booksBox);
      booksBox.appendChild(panel(null, errorBlock(e)));
    });

    return function cleanup() {
      alive = false;
      stopPolling();
    };
  }

  /* 学习通作答面板：就在作业页里展开 —— 逐题信息 + 实时日志 + 交卷分数。
     不跳工具页：用户点一行就能「做考试 / 做测验」，跑的是后台任务（/jobs 轮询）。 */
  function chaoxingWorkspace(platName, host, target) {
    clear(host);
    host.hidden = false;
    var box = el('section', 'panel workspace');

    var head = el('div', 'workspace-head');
    head.appendChild(el('h2', '', target.title || '作答'));
    head.appendChild(pill('写操作', 'pill-danger'));
    head.appendChild(el('span', 'hint-text',
      target.action === 'quiz' ? '章节测验：先干跑填卷并核验，确认后再交卷'
                               : '考试：先干跑作答，确认后再交卷（进入考试后计时不停）'));
    head.appendChild(el('span', 'spacer'));
    head.appendChild(btn('收起', 'btn-sm btn-ghost', function () {
      host.hidden = true;
      clear(host);
    }));
    box.appendChild(head);

    var body = el('div', 'panel-body');
    box.appendChild(body);

    /* 分数横幅（交卷后才出现） */
    var scoreBox = el('div', 'score-banner');
    scoreBox.hidden = true;
    body.appendChild(scoreBox);

    /* 状态 + 操作 */
    var stateText = el('span', 'pill pill-mute', '未开始');
    var summaryText = el('span', 'hint-text', '');
    var statusRow = el('div', 'pills');
    statusRow.appendChild(stateText);
    statusRow.appendChild(summaryText);
    var secStatus = el('div', 'section');
    secStatus.appendChild(el('div', 'section-title', '状态'));
    secStatus.appendChild(statusRow);
    body.appendChild(secStatus);

    var actions = el('div', 'auto-foot');
    var dryBtn = btn('干跑作答', 'btn-primary btn-sm', function () {
      Promise.resolve(start(false)).catch(function () { /* 错误已写进日志 */ });
    });
    var liveBtn = btnArmored('交卷', '确认：交卷不可逆', 'btn-danger btn-sm', function () {
      return start(true);
    });
    var stopBtn = btn('停止任务', 'btn-sm btn-danger', function () {
      if (!currentJobId) { return; }
      post('/api/jobs/' + encodeURIComponent(currentJobId) + '/cancel', {})
        .then(function () { toast('已请求停止 ' + currentJobId); })
        .catch(function (e) { toast(describe(e)); });
    });
    stopBtn.hidden = true;
    [dryBtn, liveBtn, stopBtn].forEach(function (node) { actions.appendChild(node); });
    body.appendChild(actions);

    /* 前台画面：任务运行时把浏览器画面实时显示出来（滑块/人脸都看得见） */
    var liveBox = el('div', 'live-view');
    liveBox.hidden = true;
    var liveImg = el('img', '');
    liveImg.alt = '任务画面';
    liveImg.draggable = false;
    liveBox.appendChild(liveImg);
    var liveHint = el('div', 'hint-text',
      '可直接在这张画面上拖动滑块（按住拼图往右拖），完成后点下面的「继续」');
    liveBox.appendChild(liveHint);
    body.appendChild(liveBox);

    var humanBox = el('div', 'human-gate');
    humanBox.hidden = true;
    body.appendChild(humanBox);

    /* 滑块验证策略：自动解 / 人工拖（记住上次选择） */
    var captchaMode = el('select', 'input');
    ['auto', 'human'].forEach(function (value) {
      var option = el('option', '', value === 'auto' ? '自动过滑块（推荐）' : '人工过滑块（我手动拖）');
      option.value = value;
      captchaMode.appendChild(option);
    });
    captchaMode.value = readCaptchaMode();
    captchaMode.onchange = function () { saveCaptchaMode(captchaMode.value); };
    var verifyField = el('div', 'field');
    verifyField.appendChild(el('span', 'field-label', '滑块验证'));
    verifyField.appendChild(captchaMode);
    verifyField.appendChild(el('span', 'hint-text', '人工：遇安全验证暂停等你拖（需要 --show 启动、有可见窗口）'));
    var secVerify = el('div', 'section');
    secVerify.appendChild(verifyField);
    body.appendChild(secVerify);

    /* 考试信息：时间窗口 + 剩余提交次数（进面板就查，不开始作答） */
    var metaGrid = el('div', '');
    var secMeta = el('div', 'section');
    secMeta.appendChild(el('div', 'section-title', '考试信息'));
    secMeta.appendChild(metaGrid);
    body.appendChild(secMeta);

    /* 题目信息 */
    var tableHost = el('div', '');
    var secQuestions = el('div', 'section');
    secQuestions.appendChild(el('div', 'section-title', '题目信息'));
    secQuestions.appendChild(tableHost);
    body.appendChild(secQuestions);

    /* 日志 */
    var logView = el('pre', 'log-view');
    logView.textContent = '作答日志会出现在这里。';
    var secLog = el('div', 'section');
    secLog.appendChild(el('div', 'section-title', '日志'));
    secLog.appendChild(logView);
    body.appendChild(secLog);

    host.appendChild(box);
    host.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

    var currentJobId = '';
    var watcher = null;

    function renderHumanGate(message) {
      clear(humanBox);
      humanBox.appendChild(el('div', '', '⏸ ' + message));
      var okBtn = btn('我已完成，继续', 'btn-primary btn-sm', function () {
        post('/api/jobs/' + encodeURIComponent(currentJobId) + '/human', {})
          .then(function () { humanBox.hidden = true; })
          .catch(function (e) { toast(describe(e)); });
      });
      humanBox.appendChild(okBtn);
      var captcha = el('div', 'captcha-panel');
      captcha.appendChild(el('div', 'hint-text', '正在读取验证码…'));
      humanBox.appendChild(captcha);
      loadCaptcha(captcha);
    }

    function loadCaptcha(host) {
      get('/api/platforms/chaoxing/captcha?job=' + encodeURIComponent(currentJobId))
        .then(function (info) {
          clear(host);
          if (!info || !info.ok) {
            host.appendChild(el('div', 'hint-text', (info && info.error) || '没有读到验证码'));
            return;
          }
          var linkRow = el('div', 'hint-text');
          var bgLink = el('a', '', '背景图链接');
          bgLink.href = info.bg_url; bgLink.target = '_blank'; bgLink.rel = 'noreferrer';
          var pieceLink = el('a', '', '拼图链接');
          pieceLink.href = info.piece_url; pieceLink.target = '_blank'; pieceLink.rel = 'noreferrer';
          linkRow.appendChild(bgLink);
          linkRow.appendChild(el('span', '', ' · '));
          linkRow.appendChild(pieceLink);
          linkRow.appendChild(el('span', '', ' · 缺口约 ' + info.gap_px + 'px'));
          host.appendChild(linkRow);

          var bg = el('img', 'captcha-bg');
          bg.src = info.bg_image;
          bg.alt = '滑块背景';
          host.appendChild(bg);
          var piece = el('img', 'captcha-piece');
          piece.src = info.piece_image;
          piece.alt = '拼图';
          host.appendChild(piece);

          host.appendChild(btn('按缺口自动拖一次', 'btn-sm btn-primary', function () {
            var nx = info.handle_nx;
            var ny = info.handle_ny;
            var target = nx + info.gap_ratio;
            sendPointer('down', nx, ny);
            var step = 0;
            var timer = setInterval(function () {
              step += 1;
              sendPointer('move', nx + (target - nx) * (step / 24), ny);
              if (step >= 24) {
                clearInterval(timer);
                sendPointer('up', target, ny);
              }
            }, 40);
          }));
        })
        .catch(function (e) {
          clear(host);
          host.appendChild(el('div', 'hint-text', '读取验证码失败：' + describe(e)));
        });
    }

    function sendPointer(kind, nx, ny) {
      if (!currentJobId) { return; }
      post('/api/platforms/chaoxing/pointer', {
        job: currentJobId, type: kind, nx: nx, ny: ny
      }).catch(function () { /* 画面更新时的竞态忽略 */ });
    }

    function bindPointer() {
      var dragging = false;
      var last = 0;
      function coords(event) {
        var rect = liveImg.getBoundingClientRect();
        if (!rect.width || !rect.height) { return null; }
        return {
          nx: (event.clientX - rect.left) / rect.width,
          ny: (event.clientY - rect.top) / rect.height
        };
      }
      liveImg.addEventListener('pointerdown', function (event) {
        var at = coords(event);
        if (!at) { return; }
        event.preventDefault();
        dragging = true;
        liveImg.setPointerCapture && liveImg.setPointerCapture(event.pointerId);
        sendPointer('down', at.nx, at.ny);
      });
      liveImg.addEventListener('pointermove', function (event) {
        if (!dragging) { return; }
        var now = Date.now();
        if (now - last < 40) { return; }
        last = now;
        var at = coords(event);
        if (at) { sendPointer('move', at.nx, at.ny); }
      });
      liveImg.addEventListener('pointerup', function (event) {
        if (!dragging) { return; }
        dragging = false;
        var at = coords(event);
        sendPointer('up', at ? at.nx : 0.5, at ? at.ny : 0.5);
      });
      liveImg.addEventListener('pointercancel', function () {
        if (dragging) { dragging = false; sendPointer('up', 0.5, 0.5); }
      });
    }
    bindPointer();

    function readCaptchaMode() {
      try {
        var value = window.localStorage.getItem('kdb.chaoxing.captcha');
        return value === 'human' ? 'human' : 'auto';
      } catch (e) { return 'auto'; }
    }

    function saveCaptchaMode(value) {
      try { window.localStorage.setItem('kdb.chaoxing.captcha', value === 'human' ? 'human' : 'auto'); }
      catch (e) { /* 无痕模式等拿不到 localStorage 就只在本次生效 */ }
    }

    function metaValue(data) {
      if (!data) { return '—'; }
      if (data.attempts_unlimited) { return '不限'; }
      if (data.attempts_remaining !== null && data.attempts_remaining !== undefined) {
        return '剩 ' + data.attempts_remaining + ' 次'
          + (data.attempts_limit ? '（上限 ' + data.attempts_limit + '）' : '');
      }
      if (data.attempts_limit) { return '上限 ' + data.attempts_limit + ' 次'; }
      return data.attempts_note || '—';
    }

    function renderMeta(data) {
      clear(metaGrid);
      var rows = [
        ['开始时间', data.start_text || '平台未提供'],
        ['截止时间', data.end_text || target.deadline || '平台未提供'],
        ['考试时长', data.duration_text || '平台未提供'],
        ['剩余提交次数', metaValue(data) !== '—'
          ? metaValue(data)
          : (target.action === 'exam' ? '平台未提供（能重考时不限次）' : '—')]
      ];
      var table = el('table', 'table');
      var tbody = el('tbody');
      rows.forEach(function (pair) {
        var tr = el('tr');
        tr.appendChild(el('td', 'hint-text', pair[0]));
        tr.appendChild(el('td', 'mono', pair[1]));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      metaGrid.appendChild(table);
      var remaining = data.attempts_remaining;
      if (remaining === 0) {
        dryBtn.disabled = true;
        liveBtn.disabled = true;
        stateText.textContent = '没有剩余提交次数';
        stateText.className = 'pill pill-danger';
        appendLog({ level: 'warn', text: '⚠ 平台显示剩余提交次数为 0，这次不能再交卷。' });
      }
    }

    function loadMeta() {
      var payload = {
        action: target.action,
        course: target.course,
        relation_id: String(target.relation_id || '')
      };
      if (target.action === 'quiz') {
        payload.chapter = String(target.id || '').replace(/^quiz:/, '');
      } else {
        payload.exam_id = String(target.id || '');
      }
      post('/api/platforms/chaoxing/meta', payload).then(renderMeta).catch(function (e) {
        clear(metaGrid);
        metaGrid.appendChild(el('span', 'hint-text', '读取考试信息失败：' + describe(e)));
      });
    }
    loadMeta();
    /* 交卷保险：题太少/置信度低时后端会停在「待确认」，这里把按钮切成「强制交卷」 */
    var blocked = false;

    function appendLog(line) {
      var row = el('div');
      row.appendChild(el('span', 't', time(line.at).slice(11) + '  '));
      row.appendChild(el('span', line.level || 'info', line.text || ''));
      logView.appendChild(row);
      logView.scrollTop = logView.scrollHeight;
    }

    function renderItems(items) {
      clear(tableHost);
      if (!items || !items.length) {
        tableHost.appendChild(el('div', 'hint-text', '还没有题目信息。'));
        return;
      }
      var wrap = el('div', 'table-wrap');
      var table = el('table', 'table');
      var thead = el('thead');
      var hr = el('tr');
      ['题号', '题型', '答案', '置信度', '来源', '题目'].forEach(function (name) {
        hr.appendChild(el('th', '', name));
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      var tbody = el('tbody');
      items.forEach(function (item) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', String(item.number || '—')));
        tr.appendChild(el('td', '', item.type || '—'));
        tr.appendChild(el('td', 'mono', item.answer || '（未答）'));
        var conf = typeof item.confidence === 'number' ? item.confidence.toFixed(2) : '—';
        tr.appendChild(el('td', 'mono', conf));
        tr.appendChild(el('td', '', item.source || '—'));
        var stem = String(item.text || '').replace(/\s+/g, ' ').trim();
        var stemCell = el('td', '', truncate(stem || '（题面是图片）', 90));
        tr.appendChild(stemCell);
        if (item.unsupported) { tr.title = item.unsupported; }
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      tableHost.appendChild(wrap);
    }
    renderItems([]);

    function setBusy(busy) {
      dryBtn.disabled = busy;
      liveBtn.disabled = busy;
      stopBtn.hidden = !busy;
      if (!busy) {
        /* btnArmored 点击后会把标签改成「执行中…」，跑完必须自己复位 */
        dryBtn.textContent = '干跑作答';
        liveBtn.textContent = blocked ? '强制交卷' : '交卷';
        liveBtn.classList.remove('btn-primary');
      }
    }

    function start(confirm) {
      if (!confirm) { blocked = false; }
      currentJobId = '';
      clear(logView);
      logView.textContent = '';
      scoreBox.hidden = true;
      stateText.textContent = confirm ? '交卷中…' : '干跑中…';
      stateText.className = 'pill pill-warn';
      summaryText.textContent = '';
      setBusy(true);
      var payload = {
        action: target.action,
        course: target.course,
        title: target.title,
        confirm: !!confirm
      };
      if (confirm && blocked) { payload.force = true; }
      if (target.retake) { payload.retake = true; }
      payload.captcha_mode = captchaMode.value === 'human' ? 'human' : 'auto';
      if (target.relation_id) { payload.relation_id = String(target.relation_id); }
      if (target.action === 'quiz') {
        payload.chapter = String(target.id || '').replace(/^quiz:/, '');
      } else {
        payload.exam_id = String(target.id || '');
      }
      appendLog({ level: 'info', text: '$ POST /api/platforms/' + platName + '/jobs ' +
        truncate(JSON.stringify(payload), 180) });
      return post('/api/platforms/' + encodeURIComponent(platName) + '/jobs', payload)
        .then(function (job) {
          currentJobId = job.job_id;
          stateText.textContent = '任务 ' + currentJobId + ' 运行中…';
          setBusy(true);
          if (watcher) { watcher.stop(); }
          watcher = watchJob(job.job_id, {
            intervalMs: 1200,
            guard: function () { return host.isConnected && !host.hidden; },
            onLog: appendLog,
            onJob: function (state) {
              if (state.items && state.items.length) { renderItems(state.items); }
              if (state.summary) { summaryText.textContent = state.summary; }
              if (state.has_live) {
                liveBox.hidden = false;
                liveImg.src = '/api/platforms/chaoxing/live?job='
                  + encodeURIComponent(currentJobId) + '&t=' + Date.now();
              } else {
                liveBox.hidden = true;
              }
              if (state.human) {
                humanBox.hidden = false;
                if (!humanBox.querySelector('.captcha-panel')) {
                  renderHumanGate(state.human);
                }
              } else {
                humanBox.hidden = true;
              }
              if (state.score_text) {
                scoreBox.hidden = false;
                scoreBox.textContent = '交卷结果：' + state.score_text;
              }
            },
            onDone: function (state) {
              setBusy(false);
              liveBox.hidden = true;
              humanBox.hidden = true;
              if (state.score_text) {
                scoreBox.hidden = false;
                scoreBox.textContent = '交卷结果：' + state.score_text;
              }
              if (state.status === 'failed') {
                stateText.textContent = '失败：' + (state.error || '未知错误');
                stateText.className = 'pill pill-danger';
              } else if (state.submitted) {
                blocked = false;
                stateText.textContent = '已交卷 · ' + (state.score_text || '平台未回显分数');
                stateText.className = 'pill pill-ok';
                liveBtn.textContent = '交卷';
                if (typeof assignmentsReload === 'function') { assignmentsReload(); }
              } else if (state.submit_note) {
                /* 被交卷保险拦下：题少或置信度低，等人工确认 */
                blocked = true;
                stateText.textContent = '待人工确认 · 未交卷';
                stateText.className = 'pill pill-warn';
                liveBtn.textContent = '强制交卷';
                liveBtn.classList.remove('btn-primary');
                appendLog({ level: 'warn', text: '⚠ ' + state.submit_note });
                appendLog({ level: 'info',
                  text: '→ 题目已填好并核验。确认无误后点「强制交卷」；要改答案请先在页面上改。' });
              } else {
                stateText.textContent = '干跑完成';
                stateText.className = 'pill pill-mute';
                appendLog({ level: 'info', text: '→ 干跑完成：题目已填好并核验。确认无误后点「交卷」。' });
                if (typeof assignmentsReload === 'function') { assignmentsReload(); }
              }
            },
            onError: function (e) {
              stateText.textContent = '轮询失败：' + describe(e);
            }
          });
        })
        .catch(function (e) {
          setBusy(false);
          stateText.textContent = '启动失败：' + describe(e);
          stateText.className = 'pill pill-danger';
          appendLog({ level: 'error', text: '✗ ' + describe(e) });
        });
    }
  }

  /* 刷课（平台动作版）：东软的 study 动作 —— 按资源刷，写操作，跑完才算。 */
  function studyWorkspace(platName, host) {
    clear(host);
    var box = el('section', 'panel workspace');
    var head = el('div', 'workspace-head');
    head.appendChild(el('h2', '', '刷课（平台动作）'));
    head.appendChild(pill('写操作', 'pill-danger'));
    head.appendChild(el('span', 'hint-text', '调用平台的 study 动作，以回读状态判定完成'));
    head.appendChild(el('span', 'spacer'));
    head.appendChild(btn('收起', 'btn-sm btn-ghost', function () { clear(host); }));
    box.appendChild(head);

    var body = el('div', 'panel-body');
    box.appendChild(body);
    host.appendChild(box);

    var course = input('text', '', '课程号或课程名（必须精确相等）');
    var limit = input('number', 5, '');
    limit.min = '0'; limit.step = '1';

    var form = el('div', 'form-grid');
    form.appendChild(fieldWrap('课程', course, '用「课程」页里看到的课程号或名称'));
    form.appendChild(fieldWrap('本次最多刷几个', limit, '0 = 不限；建议先 3~5 个试水'));
    body.appendChild(form);

    var stateText = el('span', 'hint-text', '还没有开始');
    var resultHost = el('div', '');
    var runBtn = btn('开始刷课', 'btn-danger', function () {
      if (!armed.on) {
        armed.on = true;
        runBtn.textContent = '再点一次，确认开始';
        runBtn.classList.add('btn-primary');
        return;
      }
      var name = String(course.value || '').trim();
      if (!name) {
        stateText.textContent = '先填课程号或课程名。';
        return;
      }
      armed.on = false;
      runBtn.disabled = true;
      runBtn.textContent = '刷课中…';
      stateText.textContent = '正在调用 study（可能需要几分钟，别关页面）';
      clear(resultHost);
      post('/api/platforms/' + encodeURIComponent(platName) + '/actions/study', {
        course: name,
        limit: Number(limit.value) || 0
      }).then(function (env) {
        stateText.textContent = '完成';
        resultHost.appendChild(panel(env.title || '刷课结果', renderEnvelope(env, {})));
      }).catch(function (e) {
        stateText.textContent = '失败：' + describe(e);
        resultHost.appendChild(panel(null, errorBlock(e)));
      }).then(function () {
        runBtn.disabled = false;
        runBtn.textContent = '开始刷课';
        runBtn.classList.remove('btn-primary');
      });
    });
    var armed = { on: false };

    var actions = el('div', 'auto-foot');
    actions.appendChild(runBtn);
    actions.appendChild(stateText);
    body.appendChild(actions);
    body.appendChild(resultHost);

    body.appendChild(el('p', 'env-note',
      'study 是写操作：会真的把资源标记为已学。平台侧可能限速或要求顺序播放，失败原因会原样显示。'));
  }

  function checkbox(checked) {
    var box = el('label', 'switch');
    var node = el('input');
    node.type = 'checkbox';
    node.checked = !!checked;
    box.appendChild(node);
    box.appendChild(el('span', '', ''));
    return box;
  }

  /* ========================================================== 一键自动化 */

  function autoWorkspace(platName, host) {
    clear(host);
    var box = el('section', 'panel workspace');
    var head = el('div', 'workspace-head');
    head.appendChild(el('h2', '', '一键自动化'));
    head.appendChild(pill('iTEST', 'pill-accent'));
    head.appendChild(pill('写操作 · 交卷不可逆', 'pill-danger'));
    head.appendChild(el('span', 'spacer'));
    head.appendChild(btn('收起', 'btn-sm btn-ghost', function () { clear(host); }));
    box.appendChild(head);

    var body = el('div', 'panel-body');
    box.appendChild(body);
    host.appendChild(box);

    var examInput = input('text', '', '考试号 exam_id');
    var startBtn = btn('开始（取卷 → 答题 → 核验）', 'btn-primary', function () { start(); });
    var form = el('div', 'form-grid');
    form.appendChild(fieldWrap('考试号', examInput, '从考试列表里复制 exam_id'));
    form.appendChild(fieldWrap('操作', actionRow([startBtn]), ''));
    body.appendChild(form);

    var status = el('div', 'pills');
    var statusSec = el('div', 'section');
    statusSec.appendChild(el('div', 'section-title', '状态'));
    statusSec.appendChild(status);
    body.appendChild(statusSec);
    var stepsBox = el('div', 'steps');
    var stepsSec = el('div', 'section');
    stepsSec.appendChild(el('div', 'section-title', '步骤'));
    stepsSec.appendChild(stepsBox);
    body.appendChild(stepsSec);

    var logView = el('pre', 'log-view');
    logView.textContent = '自动化日志会出现在这里。';
    var logSec = el('div', 'section');
    logSec.appendChild(el('div', 'section-title', '日志'));
    logSec.appendChild(logView);
    body.appendChild(logSec);

    var verifyHost = el('div', '');
    body.appendChild(verifyHost);
    var manualHost = el('div', 'auto-manual');
    body.appendChild(manualHost);

    var saveMsg = el('span', 'hint-text', '');
    var saveBtn = btn('保存并复核', 'btn-sm', function () { saveManual(); });
    var submitBtn = btnArmored('确认交卷', '再点一次：真的交卷', 'btn-danger', function () {
      return submitExam();
    });
    var statusText = el('span', 'hint-text', '流程未开始');
    var foot = el('div', 'auto-foot');
    [saveBtn, submitBtn, statusText, saveMsg].forEach(function (n) { foot.appendChild(n); });
    body.appendChild(foot);

    var examId = '';
    var jobId = '';
    var job = null;
    var alive = true;
    var watcher = null;

    function appendLog(line) {
      var row = el('div');
      row.appendChild(el('span', 't', time(line.at).slice(11) + '  '));
      row.appendChild(el('span', line.level || 'info', line.text || ''));
      logView.appendChild(row);
      logView.scrollTop = logView.scrollHeight;
    }

    function setStatus(jobObj) {
      clear(status);
      if (!jobObj) { return; }
      status.appendChild(statusPill(jobObj.status));
      if (jobObj.paper && jobObj.paper.title) {
        status.appendChild(pill(jobObj.paper.title, 'pill-mute'));
        status.appendChild(pill((jobObj.paper.question_count || 0) + ' 题 / ' +
          (jobObj.paper.slot_count || 0) + ' 槽位', 'pill-mute'));
      }
      var missing = (jobObj.manual || []).length;
      if (missing) { status.appendChild(pill('缺答案 ' + missing + ' 题', 'pill-warn')); }
    }

    function renderSteps(steps) {
      clear(stepsBox);
      (steps || []).forEach(function (step) {
        var item = el('span', 'step ' + (step.ok ? 'ok' : 'fail'));
        item.appendChild(el('span', '', step.name || '—'));
        item.appendChild(el('span', 'detail',
          step.detail || (step.ok ? '通过' : '未通过')));
        stepsBox.appendChild(item);
      });
    }

    function renderVerify(verify) {
      clear(verifyHost);
      if (!verify) { return; }
      var sec = el('div', 'section');
      sec.appendChild(el('div', 'section-title', '核验'));
      var chips = el('div', 'pills');
      var persisted = verify.persisted !== false && (verify.matched || 0) > 0;
      chips.appendChild(persisted ? pill('服务端已保存', 'pill-ok') : pill('交卷时重填', 'pill-warn'));
      chips.appendChild(pill('缺答案 ' + (verify.missing || []).length,
        (verify.missing || []).length ? 'pill-warn' : 'pill-ok'));
      chips.appendChild(pill('核验不符 ' + ((verify.checked && verify.checked.total) || 0),
        (verify.checked && verify.checked.total) ? 'pill-danger' : 'pill-ok'));
      sec.appendChild(chips);
      var mism = verify.mismatched || [];
      if (mism.length) {
        sec.appendChild(renderTable(
          [{ key: 'qid', label: '题号', mono: true },
           { key: 'want', label: '答案单', mono: true },
           { key: 'got', label: '页面读回', mono: true }], mism));
        sec.appendChild(el('p', 'env-note',
          '重进考场读回为空是 iTEST 自动保存不认脚本填值导致的；同一会话里已逐项确认，交卷时会重填并再核验。'));
      }
      verifyHost.appendChild(sec);
    }

    function renderManual(rows) {
      clear(manualHost);
      if (!rows || !rows.length) { return; }
      var h = el('h3', '', '需要人工的题');
      h.appendChild(el('span', 'hint-text', '选完点「保存并复核」'));
      manualHost.appendChild(h);
      rows.forEach(function (row) {
        var card = el('div', 'auto-q');
        var head2 = el('div', 'auto-q-head');
        head2.appendChild(pill(row.kind_label || row.kind || '题目', 'pill-mute'));
        head2.appendChild(el('span', 'mono', row.qid));
        if (row.slots) { head2.appendChild(el('span', 'hint-text', row.slots + ' 空')); }
        card.appendChild(head2);
        if (row.stem) { card.appendChild(el('div', 'auto-q-stem', row.stem)); }

        if (row.kind === 'radio' && (row.options || []).length) {
          var subs = {};
          row.options.forEach(function (opt) {
            if (!opt || !opt.letter) { return; }
            (subs[opt.sub] = subs[opt.sub] || []).push(opt);
          });
          var box2 = el('div', 'q-subs');
          Object.keys(subs).sort(function (x, y) { return Number(x) - Number(y); })
            .forEach(function (sub, i) {
              var label = el('label');
              label.appendChild(el('span', '', '第 ' + (i + 1) + ' 小题'));
              var sel = el('select', 'mono');
              sel.setAttribute('data-manual-qid', row.qid);
              sel.setAttribute('data-manual-sub', sub);
              var empty = el('option', '', '— 请选择 —');
              empty.value = '';
              sel.appendChild(empty);
              subs[sub].forEach(function (o) {
                var opt = el('option', '', o.letter + '. ' + truncate(o.text || '', 48));
                opt.value = o.letter;
                sel.appendChild(opt);
              });
              label.appendChild(sel);
              box2.appendChild(label);
            });
          card.appendChild(box2);
        } else {
          var node = row.kind === 'write' ? el('textarea', 'mono') : el('input', 'mono');
          if (row.kind !== 'write') { node.placeholder = '按空顺序填字母，用空格分隔，如 A C D'; }
          node.setAttribute('data-manual-qid', row.qid);
          node.setAttribute('data-manual-sub', '1');
          card.appendChild(node);
        }
        manualHost.appendChild(card);
      });
    }

    function collectManual() {
      var byQid = {};
      Array.prototype.forEach.call(manualHost.querySelectorAll('[data-manual-qid]'), function (node) {
        var qid = node.getAttribute('data-manual-qid');
        var sub = Number(node.getAttribute('data-manual-sub') || 1);
        var value = String(node.value || '').trim();
        if (!value) { return; }
        (byQid[qid] = byQid[qid] || {})[sub] = value;
      });
      var out = {};
      Object.keys(byQid).forEach(function (qid) {
        var subs = byQid[qid];
        out[qid] = Object.keys(subs).sort(function (a, b) { return Number(a) - Number(b); })
          .map(function (k) { return subs[k]; }).join(' ');
      });
      return out;
    }

    function saveManual() {
      if (!examId) { saveMsg.textContent = '先启动一次自动化，才有试卷缓存。'; return; }
      var answers = collectManual();
      if (!Object.keys(answers).length) {
        saveMsg.textContent = '还没有填任何答案。';
        return;
      }
      saveBtn.disabled = true;
      saveMsg.textContent = '保存中…';
      post('/api/platforms/itest/manual', { exam_id: examId, answers: answers })
        .then(function (res) {
          var left = (job && job.manual) || [];
          var filled = Object.keys(answers).length;
          var missing = (res && res.missing) || [];
          saveMsg.textContent = '已保存 ' + filled + ' 题，还缺 ' + missing.length + ' 题';
          if (job) { job.manual = missing; }
          renderManual(missing);
          toast('已保存人工答案');
        })
        .catch(function (e) { saveMsg.textContent = describe(e); })
        .then(function () { saveBtn.disabled = false; });
    }

    function submitExam() {
      if (!examId) { return Promise.reject(new Error('no exam')); }
      if ((job && job.status === 'running')) {
        toast('流程还在跑，等它结束');
        return Promise.reject(new Error('running'));
      }
      statusText.textContent = '交卷中…（会重进考场、重填并逐项核验）';
      return post('/api/assignments/submit', {
        platform: 'itest', assignment_id: examId, confirm: true
      }).then(function (res) {
        appendLog({ level: 'info', text: '$ POST /api/assignments/submit confirm=true' });
        appendLog({ level: (res && (res.server_finish === 1 || res.server_finish === '1')) ? 'ok' : 'warn',
          text: '· 返回：submitted=' + (res && res.submitted) +
            ' server_finish=' + (res && res.server_finish) });
        (res && res.steps || []).forEach(function (step) {
          appendLog({ level: 'info', text: '   · ' + (step.name || '') + ' ' + (step.detail || '') });
        });
        statusText.textContent = (res && (res.server_finish === 1 || res.server_finish === '1'))
          ? '已交卷（服务端 finish=1）'
          : '交卷结果待确认，请看日志';
        renderPage();
      }).catch(function (e) {
        statusText.textContent = '交卷失败：' + describe(e);
        appendLog({ level: 'error', text: '✗ ' + describe(e) });
        submitBtn.disabled = false;
      });
    }

    function paint(jobObj) {
      job = jobObj;
      setStatus(jobObj);
      renderSteps(jobObj.steps);
      renderVerify(jobObj.verify);
      renderManual(jobObj.manual);
      statusText.textContent = jobObj.status === 'running' ? '运行中'
        : (jobObj.status === 'succeeded' ? '流程已完成，核对后可以交卷'
          : ('失败：' + (jobObj.error || '未知错误')));
      var missing = (jobObj.manual || []).length;
      submitBtn.disabled = jobObj.status === 'running' || missing > 0;
      submitBtn.title = missing
        ? ('还有 ' + missing + ' 题没答案，先在下面补上')
        : (jobObj.status === 'running' ? '流程还没跑完' : '真的交卷（不可逆）');
    }

    function watchAuto(jobId) {
      watcher = watchJob(jobId, {
        intervalMs: 1200,
        guard: function () { return alive && host.isConnected; },
        onLog: appendLog,
        onJob: paint,
        onError: function (e) { statusText.textContent = '轮询失败：' + describe(e); }
      });
    }

    function start() {
      examId = String(examInput.value || '').trim();
      if (!examId) { statusText.textContent = '先填考试号。'; return; }
      startBtn.disabled = true;
      logView.textContent = '';
      statusText.textContent = '启动中…';
      post('/api/platforms/itest/auto', { exam_id: examId }).then(function (jobObj) {
        jobId = jobObj.job_id;
        if (watcher) { watcher.stop(); }
        paint(jobObj);
        watchAuto(jobId);
      }).catch(function (e) {
        statusText.textContent = '启动失败：' + describe(e);
        startBtn.disabled = false;
      });
    }

    return function cleanup() {
      alive = false;
      if (watcher) { watcher.stop(); }
    };
  }

  /* ================================================================ 课程 */

  function inspectRow(platformName, row, title) {
    var fields = Object.keys(row || {}).map(function (k) {
      var v = row[k];
      return {
        label: k,
        value: (v && typeof v === 'object') ? jsonText(v, 400) : dash(v)
      };
    });
    openInspector({
      title: title || '记录详情',
      sub: platformLabel(platformName),
      body: renderFields(fields)
    });
  }

  function pageCourses(page, seq) {
    if (needsPlatformPage(page, seq)) { return; }
    var platName = state.platform;

    var reload = btn('重新加载', 'btn-sm btn-primary', function () { load(); });
    page.appendChild(pageHead({
      title: '课程',
      badge: pill('只读', 'pill-ro'),
      sub: [scopeChip(), '读取课程 / 班级列表'],
      actions: [reload]
    }));

    var host = el('div', '');
    page.appendChild(host);

    function load() {
      clear(host);
      host.appendChild(panel(null, loadingBlock()));
      post('/api/platforms/' + encodeURIComponent(platName) + '/actions/courses', {}).then(function (env) {
        if (seq !== state.pageSeq) { return; }
        clear(host);
        host.appendChild(panel(env.title || '课程 / 班级',
          renderEnvelope(env, {
            hideHead: true,
            onRow: function (row) { inspectRow(platName, row, '课程记录'); },
            empty: '这个平台没有返回课程。请确认账号会话是否还有效。'
          }),
          {
            flush: false,
            aside: pill(String((env.rows || []).length) + ' 条', 'pill-mute')
          }));
      }).catch(function (e) {
        if (seq !== state.pageSeq) { return; }
        clear(host);
        host.appendChild(panel(null, errorBlock(e, load)));
      });
    }

    load();
    page.appendChild(hintBlock([
      '调平台只读动作 courses（list_courses）：会真的登录平台并读取数据，但不改动任何内容。',
      '冻结/停用的课程可能不出现在列表里，这取决于平台自己的返回。',
      '任何失败都会显示后端的中文原因，不做静默降级。'
    ]));
  }

  /* ================================================================ 作业 */

  var ASSIGN_ID_KEYS = ['assignment_id', 'assignmentId', 'id', 'rid', 'task_id', 'taskId'];

  /* 作业页注册自己的重载函数：提交成功后用它刷新列表（不整页重绘） */
  var assignmentsReload = null;

  function assignmentIdOf(row) {
    for (var i = 0; i < ASSIGN_ID_KEYS.length; i++) {
      var v = row[ASSIGN_ID_KEYS[i]];
      if (v !== undefined && v !== null && v !== '') { return String(v); }
    }
    return '';
  }

  function pageAssignments(page, seq) {
    if (needsPlatformPage(page, seq)) { return; }
    var platName = state.platform;
    var plat = platformOf(platName) || {};
    var caps = plat.capabilities || {};
    /* 能提交 ≠ 能作答：pigai 声明 submit=True，但没有 fetch_questions，
       走通用提交必然 400「没有取到题目」。这种按钮不该出现。 */
    /* 品课的提交必须走「计划 → 人工复核 → 提交」，行内直提必被守卫拒绝；
       所以这里也把它排除，入口只有上面那个「提交计划（复核）」。 */
    var canSubmit = caps.submit === true && caps.fetch_questions === true
      && platName !== 'pinco';

    var courseField = el('input');
    courseField.type = 'text';
    courseField.className = 'mono';
    courseField.placeholder = '留空 = 遍历全部课程';
    courseField.setAttribute('aria-label', 'course');

    var actions = [];
    if (!canSubmit) {
      /* 学习通没有通用提交链路，但有自己的作答动作：别让用户以为「不能交」 */
      actions.push(pill(
        platName === 'chaoxing' ? '提交走「工具 · 做考试」' : '该平台未声明提交能力',
        platName === 'chaoxing' ? 'pill-mute' : 'pill-warn'));
    }
    if (plat.native_submit_flow) {
      actions.push(btn('一键全部提交', 'btn-sm', function () { openBatchSubmit(platName); }));
    }
    var brushHost = el('div', '');
    var autoHost = el('div', '');
    var chaoxingHost = el('div', '');
    chaoxingHost.hidden = true;
    /* 刷课入口按后端真实能力给：
       - unipus 有原生刷课链路（course-tree + brush）；
       - neusoft 的刷课是平台动作 study；
       - 其它平台没有刷课，按钮直接不出现，不给必 501 的入口。 */
    var hasNativeBrush = platName === 'unipus';
    var hasStudyAction = false;
    actions.push(btn('刷课', 'btn-sm', function () {
      brushHost.hidden = false;
      clear(brushHost);
      if (hasNativeBrush) {
        brushWorkspace(platName, brushHost);
      } else {
        studyWorkspace(platName, brushHost);
      }
      brushHost.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }));
    var brushButton = actions[actions.length - 1];
    brushButton.hidden = true;
    /* 品课的写链路是「计划 → 人工复核 → 提交」：没有人工复核就一律拒绝，
       所以入口是一个独立的面板，而不是行内「提交」。 */
    if (platName === 'pinco') {
      actions.push(btn('提交计划（复核）', 'btn-sm', function () {
        openPincoPlan(courseField);
      }));
    }
    if (platName === 'itest') {
      actions.push(btn('一键自动化', 'btn-sm', function () {
        autoHost.hidden = false;
        clear(autoHost);
        autoWorkspace(platName, autoHost);
        autoHost.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }));
    }
    actions.push(btn('平台日志', 'btn-sm btn-ghost', function () { openPlatformLog(platName); }));

    /* 动作清单一到手，就决定「刷课」按钮出不出现 */
    get('/api/platforms/' + encodeURIComponent(platName) + '/actions').then(function (body) {
      var names = ((body && body.actions) || []).map(function (a) { return a.name; });
      hasStudyAction = names.indexOf('study') >= 0;
      brushButton.hidden = !(hasNativeBrush || hasStudyAction);
      brushButton.title = hasNativeBrush
        ? '原生刷课：教材树 + 时长 + 提交未完成'
        : '按平台动作 study 刷课（写操作，需二次确认）';
    }).catch(function () {
      brushButton.hidden = !hasNativeBrush;
    });

    page.appendChild(pageHead({
      title: '作业',
      badge: pill('列表只读', 'pill-ro'),
      sub: [scopeChip(), '双击任意一行看详情；提交是两步的（先干跑）'],
      actions: actions
    }));

    /* 工具条：参数 + 加载 */
    var bar = el('div', 'form-grid');
    var f1 = el('div', 'field');
    f1.appendChild(el('span', 'field-label', 'course（可选）'));
    f1.appendChild(courseField);
    f1.appendChild(el('span', 'hint-text', '课程号；留空就是全部课程'));
    bar.appendChild(f1);
    var stateToggle = checkbox(false);
    var f1b = el('div', 'field');
    f1b.appendChild(el('span', 'field-label', '读取完成状态'));
    f1b.appendChild(stateToggle);
    f1b.appendChild(el('span', 'hint-text', '逐组回读 progress/v2：慢但真实；不读时状态显示 —'));
    bar.appendChild(f1b);

    var f2 = el('div', 'field field-actions');
    var loadBtn = btn('加载作业', 'btn-primary', function () { load(); });
    f2.appendChild(el('span', 'field-label', '操作'));
    f2.appendChild(loadBtn);
    bar.appendChild(f2);

    page.appendChild(panel('筛选', bar));
    page.appendChild(brushHost);
    page.appendChild(autoHost);
    page.appendChild(chaoxingHost);

    var host = el('div', '');
    page.appendChild(host);

    assignmentsReload = function () { load(true); };

    function load(silent) {
      var course = String(courseField.value || '').trim();
      var wantState = stateToggle.querySelector('input').checked;
      if (!silent) {
        clear(host);
        host.appendChild(panel(null, loadingBlock()));
      }
      loadBtn.disabled = true;
      var payload = course ? { course: course } : {};
      if (wantState) { payload.with_state = 1; }
      function renderAssignments(env) {
        if (seq !== state.pageSeq) { return; }
        clear(host);
        var rows = (env && env.rows) || [];
        var navigable = rows.filter(function (row) { return assignmentIdOf(row) !== ''; });
        host.appendChild(panel(env.title || '作业 / 任务',
          renderEnvelope(env, {
            hideHead: true,
            // 提交成功且回读 finish=True 的行：直接显示「已完成」
            rowPatch: function (row) {
              var id = assignmentIdOf(row);
              if (!id || !state.doneOverrides[platName + '|' + id]) { return row; }
              if (!has(row, 'state')) { return row; }
              var patched = {};
              Object.keys(row).forEach(function (k) { patched[k] = row[k]; });
              patched.state = '已完成';
              return patched;
            },
            onRow: function (row) {
              var index = navigable.indexOf(row);
              if (index < 0) { inspectRow(platName, row, '作业记录'); return; }
              openAssignmentAt(platName, navigable, index, canSubmit);
            },
            empty: '这个平台没有返回作业。',
            rowAction: function (row) {
              var id = assignmentIdOf(row);
              if (!id) { return el('span', 'hint-text', '—'); }
              if (!canSubmit) {
                if (platName === 'chaoxing') {
                  /* 不跳工具页：就地展开作答面板（题目信息 + 日志 + 分数） */
                  var rowState = String(row.state || '');
                  var retakeId = String(row.retake_exam_id || '');
                  if (row.retake === true && retakeId && rowState !== '已过期') {
                    /* 平台允许重考：已完成也能再进一次（分数按平台规则取） */
                    return btn(rowState === '已完成' ? '重考' : '做考试', 'btn-sm', function () {
                      chaoxingWorkspace(platName, chaoxingHost, {
                        action: 'exam',
                        id: retakeId,
                        title: (row.title || retakeId) + (rowState === '已完成' ? '（重考）' : ''),
                        relation_id: String(row.answer_id || row.retake_relation_id || ''),
                        retake: true,
                        deadline: String(row.deadline || ''),
                        course: String(courseField.value || '').trim() || (row.course || '')
                      });
                    });
                  }
                  if (rowState === '已完成' || rowState === '已过期') {
                    /* 状态列已经写着「已完成/已过期」，这里不再重复，只留说明 */
                    var doneTag = el('span', 'hint-text', '—');
                    doneTag.title = rowState === '已完成'
                      ? '已交卷；这场没有重考入口（或平台已关闭重考）'
                      : '已过截止时间，不能再作答';
                    return doneTag;
                  }
                  var isQuiz = String(id).indexOf('quiz:') === 0;
                  return btn(isQuiz ? '做测验' : '做考试', 'btn-sm', function () {
                    chaoxingWorkspace(platName, chaoxingHost, {
                      action: isQuiz ? 'quiz' : 'exam',
                      id: id,
                      title: row.title || id,
                      deadline: String(row.deadline || ''),
                      course: String(courseField.value || '').trim() || (row.course || '')
                    });
                  });
                }
                var why = caps.submit !== true
                  ? '该平台未声明提交能力'
                  : '该平台没有取题能力，通用提交会失败（改用平台自己的动作）';
                var tag = el('span', 'hint-text', '不可提交');
                tag.title = why;
                return tag;
              }
              return btn('提交', 'btn-sm', function () {
                openSubmitFlow(platName, id, row, canSubmit);
              });
            }
          }),
          {
            aside: pill(rows.length + ' 条', 'pill-mute')
          }));
        renderRail();
      }

      function enrichChaoxing(env) {
        var rows = (env && env.rows) || [];
        var items = [];
        var mapping = [];
        rows.forEach(function (row, index) {
          var id = assignmentIdOf(row);
          if (!id) { return; }
          if (String(id).indexOf('quiz:') === 0) {
            items.push({ action: 'quiz', chapter: String(id).slice(5) });
          } else {
            items.push({
              action: 'exam',
              exam_id: String(id),
              relation_id: String(row.answer_id || row.retake_relation_id || '')
            });
          }
          mapping.push(index);
        });
        if (!items.length) { return Promise.resolve(env); }
        return post('/api/platforms/chaoxing/meta-batch', { course: course, items: items })
          .then(function (res) {
            var metas = (res && res.metas) || [];
            var hasStart = false;
            var hasAttempt = false;
            metas.forEach(function (meta, at) {
              var row = rows[mapping[at]];
              if (!row || !meta || meta.ok === false) { return; }
              if (meta.start_text) { row.start_text = meta.start_text; hasStart = true; }
              if (meta.end_text && (!row.deadline || row.deadline === '—')) {
                row.deadline = meta.end_text;
              }
              if (meta.attempts_text) {
                row.attempts_text = meta.attempts_text;
                hasAttempt = true;
              } else if (meta.attempts_unlimited) {
                row.attempts_text = '不限';
                hasAttempt = true;
              } else if (meta.attempts_remaining !== null && meta.attempts_remaining !== undefined) {
                row.attempts_text = '剩 ' + meta.attempts_remaining + ' 次';
                hasAttempt = true;
              } else if (meta.attempts_limit) {
                row.attempts_text = '上限 ' + meta.attempts_limit + ' 次';
                hasAttempt = true;
              }
            });
            rows.forEach(function (row) {
              if (row.attempts_text) { hasAttempt = true; return; }
              var state = String(row.state || '');
              if (row.retake === true) {
                row.attempts_text = '可重考';
                hasAttempt = true;
              } else if (state === '已完成') {
                row.attempts_text = '不可重考';
                hasAttempt = true;
              }
            });
            hasAttempt = hasAttempt || rows.some(function (row) { return !!row.attempts_text; });
            if (Array.isArray(env.columns)) {
              if (hasStart) { env.columns.push({ key: 'start_text', label: '开始', mono: true }); }
              if (hasAttempt) { env.columns.push({ key: 'attempts_text', label: '剩余次数', mono: true }); }
            }
            return env;
          })
          .catch(function () { return env; });
      }

      post('/api/platforms/' + encodeURIComponent(platName) + '/actions/assignments',
        payload).then(function (env) {
        if (seq !== state.pageSeq) { return; }
        if (platName === 'chaoxing' && course) {
          return enrichChaoxing(env).then(function (ready) { renderAssignments(ready); });
        }
        renderAssignments(env);
      }).catch(function (e) {
        if (seq !== state.pageSeq) { return; }
        clear(host);
        host.appendChild(panel(null, errorBlock(e, load)));
      }).then(function () {
        loadBtn.disabled = false;
      });
    }

    load();
    page.appendChild(hintBlock([
      '列表来自平台动作 assignments（list_assignments），只读；双击一行看该作业的详细作答。',
      '东软的「单元测试 / 测验」和作业走同一套取卷-交卷链路，类型列会显示「考试」；' +
        '之前只认作业类型，这类任务被整批过滤掉了。',
      '「提交」是两步的：先 confirm=false 干跑，把每题打算怎么答、置信度多少摊开；确认后才 confirm=true 真交。',
      canSubmit ? '该平台声明了 submit 能力，所以行内提交可用。'
        : (caps.submit === true
          ? '该平台声明了 submit，但没有取题能力（fetch_questions），行内提交保持禁用；'
            + 'pigai 请打开作业详情，用里面的「批改网评分循环（提分）」。'
          : '该平台没有声明 submit 能力，提交入口保持禁用。'),
      '后端还会拦两道门槛：一题都没答出来、或最低置信度低于阈值，都会 400 拒绝。',
      platName === 'unipus'
        ? '注意：口语 / 角色扮演题目前不做语音评测 —— 提交的是空录音，平台会记 0 分；'
          + '这一项已列入「计划」页（只标注、未接入）。'
        : ''
    ]));
  }

  /* 作业详情：检查器里带「上一条 / 下一条」，连续核对不用回列表再点。 */
  function openAssignmentAt(platName, rows, index, canSubmit) {
    var row = rows[index] || {};
    var id = assignmentIdOf(row);
    var title = pickColumn(row, ['title', 'name', 'label', 'subject']) || id;

    function nav(delta) {
      var next = index + delta;
      if (next < 0 || next >= rows.length) { return; }
      openAssignmentAt(platName, rows, next, canSubmit);
    }

    var foot = [];
    foot.push(btn('← 上一条', 'btn-sm btn-ghost', function () { nav(-1); }));
    if (index <= 0) { foot[0].disabled = true; }
    foot.push(btn('下一条 →', 'btn-sm btn-ghost', function () { nav(1); }));
    if (index >= rows.length - 1) { foot[1].disabled = true; }
    if (canSubmit) {
      foot.push(btn('提交这个作业', 'btn-sm btn-danger', function () {
        openSubmitFlow(platName, id, row, canSubmit);
      }));
    }
    foot.push(el('span', 'hint-text', (index + 1) + ' / ' + rows.length));
    foot.push(btn('关闭', 'btn-sm btn-ghost', closeInspector));

    openInspector({
      title: truncate(title, 40),
      sub: platName + ' · ' + id,
      body: loadingBlock(),
      foot: foot
    });
    /* pigai 的作业详情多挂一块「评分循环」：它的唯一写入口。
       详情读失败也照挂 —— 循环只靠 rid 就能跑（自己去读稿件）。 */
    function attachGradePanel(node) {
      if (platName !== 'pigai' || !id) { return node; }
      var gradeHost = el('div', '');
      node.appendChild(gradeHost);
      gradeWorkspace(platName, id, gradeHost);
      return node;
    }

    post('/api/assignments/detail', { platform: platName, assignment_id: id }).then(function (env) {
      openInspector({
        title: env.title || truncate(title, 40),
        sub: platName + ' · ' + id,
        body: attachGradePanel(renderEnvelope(env, { hideHead: true })),
        foot: foot
      });
    }).catch(function (e) {
      openInspector({
        title: truncate(title, 40),
        sub: platName + ' · ' + id,
        body: attachGradePanel(errorBlock(e)),
        foot: foot
      });
    });
  }

  /* ==================================================== 批改网评分循环 */

  /* stop_reason → 结论文案（对齐 platforms/pigai/grade_loop.py 的 STOP_* 常量） */
  var GRADE_STOP_TEXT = {
    reached: '已达目标分',
    exhausted: '轮次用尽，仍未达标',
    unavailable: '暂时没取到分数',
    timeout: '等批改超时（稿子已交，只是还没出分）',
    fetched: '已取到分数',
    error: '循环出错'
  };

  /* 事件类型 → 一行中文；{xxx} 从事件对象里取，取不到就显示 — */
  var GRADE_EVENT_TEXT = {
    'attempt.existing': '发现已有稿件（{eid}，{raw_state}）—— 先取分，不提交',
    'attempt.start': '第 {attempt} 轮：开始交稿（{word_count} 词）',
    'attempt.submitted': '第 {attempt} 轮：已提交（稿件号 {eid}）',
    'attempt.analysis': '第 {attempt} 轮：等批改…（{tick}）',
    'attempt.graded': '第 {attempt} 轮：{score} 分（批注 {feedback_count} 条）',
    'attempt.rewriting': '第 {attempt} 轮：按批注改写（{word_count} 词）',
    'attempt.resubmit_without_score': '第 {attempt} 轮：没有分数，按选择继续改写重交',
    'attempt.timeout': '第 {attempt} 轮：等分超时（{error}）',
    'attempt.error': '第 {attempt} 轮：出错（{error}）',
    'loop.finish': '循环结束：{stop_reason}'
  };

  function gradeEventText(ev) {
    var tpl = GRADE_EVENT_TEXT[ev && ev.kind];
    if (!tpl) { return String((ev && ev.kind) || '事件'); }
    return tpl.replace(/\{(\w+)\}/g, function (_m, key) {
      var v = ev[key];
      return (v === undefined || v === null || v === '') ? '—' : String(v);
    });
  }

  /* 事件级别 → .log-view 里可用的颜色类（info / ok / warn / error） */
  function gradeEventLevel(ev) {
    var kind = String((ev && ev.kind) || '');
    if (kind === 'attempt.graded') { return 'ok'; }
    if (kind === 'loop.finish') { return ev.stop_reason === 'reached' ? 'ok' : 'warn'; }
    if (kind.indexOf('error') >= 0) { return 'error'; }
    if (kind.indexOf('timeout') >= 0) { return 'warn'; }
    return 'info';
  }

  /* 评分循环的 job 与通用任务不同：它没有 logs / steps，只有 events / attempts。
     日志由 events 逐行翻出来，轮次进度由 attempts 拼。 */
  function gradeEventLine(ev) {
    return { at: ev && ev.at, level: gradeEventLevel(ev), text: gradeEventText(ev) };
  }

  /* 取分 / 等分返回的是「快照对象」而不是标准信封，这里包成 detail 信封，
     这样能直接用 renderEnvelope：标量走字段区，批注与正文走 section。 */
  function gradeSnapshotEnvelope(snap, title) {
    snap = isObj(snap) ? snap : {};
    var fields = [];
    var score = snap.score;
    fields.push({
      label: '分数',
      value: (score === null || score === undefined)
        ? '未出分' : (num(score) + ' / ' + num(snap.full_marks))
    });
    fields.push({ label: '平台状态', value: snap.raw_state || '—' });
    fields.push({ label: '是否已批改', value: snap.graded ? '是' : '否' });
    fields.push({ label: '稿件号', value: snap.eid || '—' });
    if (snap.word_count !== undefined && snap.word_count !== null) {
      fields.push({ label: '字数', value: String(snap.word_count) });
    }
    var dims = (snap.dimensions || []).map(function (d) {
      return (d.name || '?') + ' ' + Math.round((Number(d.ratio) || 0) * 100) + '%';
    });
    if (dims.length) { fields.push({ label: '维度得分', value: dims.join(' · ') }); }
    if (snap.analysis_error) { fields.push({ label: '分析请求被拒', value: snap.analysis_error }); }
    if (snap.detail_error) { fields.push({ label: '详情页读取失败', value: snap.detail_error }); }

    var sections = [];
    var overall = String(snap.overall_comment || '').trim();
    if (overall) { sections.push({ label: '总评', kind: 'text', text: overall }); }
    var feedback = snap.feedback_lines || snap.feedback || [];
    if (feedback.length) {
      sections.push({
        label: '批注（' + feedback.length + ' 条）', kind: 'table',
        columns: [{ key: 'text', label: '批注' }],
        rows: feedback.map(function (t) { return { text: String(t) }; })
      });
    }
    var essay = String(snap.essay_text || '').trim();
    if (essay) { sections.push({ label: '正文', kind: 'text', text: essay }); }
    return { kind: 'detail', title: title, fields: fields, sections: sections };
  }

  /* 历史轮次：本地记录库里的每一轮（表结构见 bank/store.py 的 essay_attempts） */
  function gradeHistoryEnvelope(data, rid) {
    var rows = ((data && data.attempts) || []).filter(isObj);
    if (!rows.length) {
      return {
        kind: 'message', title: '历史轮次 · ' + rid,
        message: '还没有记录。跑一次提分循环或点一次「只取分」，每轮的分数与批注就会写进历史。'
      };
    }
    return {
      kind: 'table',
      title: '历史轮次 · ' + rid,
      note: '来自本地记录库：每轮的交稿号、分数与平台状态。批注与正文在上面的「每轮结果」里看。',
      columns: [
        { key: 'attempt', label: '轮次', mono: true },
        { key: 'score', label: '分数', mono: true },
        { key: 'state', label: '平台状态' },
        { key: 'words', label: '字数', mono: true },
        { key: 'eid', label: '稿件号', mono: true },
        { key: 'at', label: '记录时间' }
      ],
      rows: rows.map(function (a) {
        var text = String(a.essay_text || '');
        return {
          attempt: Number(a.attempt) === 0 ? '现有稿件' : String(a.attempt),
          score: (a.score === null || a.score === undefined)
            ? '—' : (num(a.score) + ' / ' + num(a.full_marks)),
          state: a.graded ? (a.raw_state || '已批改') : (a.raw_state || '未出分'),
          words: String(text.trim() ? text.trim().split(/\s+/).length : 0),
          eid: a.eid || '—',
          at: time(a.created_at)
        };
      })
    };
  }

  /* 一轮的卡片：分数 + 四维 + 总评 + 批注 + 正文，默认展开最该看的那一轮 */
  function gradeAttemptCard(a) {
    var isBase = Number(a.attempt) === 0;
    var graded = a.graded === true;
    var scoreText = (graded && a.score !== null && a.score !== undefined)
      ? (num(a.score) + ' 分')
      : (a.raw_state || '未出分');
    var card = el('details', 'tree-unit');
    if (isBase || Number(a.attempt) === 1) { card.open = true; }
    var sum = el('summary');
    sum.appendChild(el('span', '',
      (isBase ? '现有稿件（未重新提交）' : ('第 ' + a.attempt + ' 轮')) + ' · ' + scoreText));
    sum.appendChild(el('span', 'count',
      (a.word_count || 0) + ' 词 · ' + (a.eid ? truncate(a.eid, 14) : '无稿件号')));
    card.appendChild(sum);

    var inner = el('div', 'panel-body');
    if (a.dimensions && a.dimensions.length) {
      var chips = el('div', 'pills');
      a.dimensions.forEach(function (d) {
        chips.appendChild(pill((d.name || '?') + ' ' +
          Math.round((Number(d.ratio) || 0) * 100) + '%', 'pill-mute'));
      });
      inner.appendChild(chips);
    }
    var overall = String(a.overall || '').trim();
    if (overall) { inner.appendChild(el('p', '', '总评：' + overall)); }
    var note = String(a.note || '').trim();
    if (note) { inner.appendChild(el('p', 'env-note', note)); }
    if (a.feedback && a.feedback.length) {
      inner.appendChild(el('div', 'section-title', '批注 ' + a.feedback.length + ' 条'));
      inner.appendChild(listOf(a.feedback.map(String)));
    }
    if (a.essay_text) {
      inner.appendChild(el('div', 'section-title', '正文'));
      inner.appendChild(el('pre', 'code', a.essay_text));
    }
    card.appendChild(inner);
    return card;
  }

  /* 批改网评分循环面板（仅 pigai）。
   *
   * 为什么只能在这里写：pigai 的 Capabilities 里 fetch_questions = False，
   * 通用「提交」拿不到题目必然 400，所以作业列表那一行只会显示「不可提交」；
   * 这个循环（自己读稿件、自己交正文）才是唯一的写入口。
   * 挂载点：openAssignmentAt() 在 platName === 'pigai' 时追加到检查器正文末尾。
   */
  function gradeWorkspace(platName, rid, host) {
    clear(host);

    var alive = true;
    var currentJob = null;
    var watcher = null;
    var renderedEvents = 0;

    var box = el('section', 'panel workspace');
    var head = el('div', 'workspace-head');
    head.appendChild(el('h2', '', '批改网评分循环（提分）'));
    head.appendChild(pill('pigai', 'pill-accent'));
    head.appendChild(pill('写操作 · 会反复重交', 'pill-danger'));
    head.appendChild(el('span', 'spacer'));
    head.appendChild(btn('收起', 'btn-sm btn-ghost', function () {
      alive = false;
      stopWatch();
      clear(host);
    }));
    box.appendChild(head);

    var body = el('div', 'panel-body');
    box.appendChild(body);
    host.appendChild(box);

    body.appendChild(el('p', 'env-note',
      'pigai 没有声明 fetch_questions：控制台的通用「提交」因此不可用（作业列表里显示「不可提交」）。'
      + '这个循环是唯一的写入口 —— 读已有稿件 → 首次交初始正文 → 等批改 → 按批注改写 → 再交；'
      + '「开始提分循环」要点两次才会真的提交。'));

    /* --- 参数 --- */
    var targetInput = input('number', 90, '');
    targetInput.min = '1'; targetInput.max = '100'; targetInput.step = '1';
    var attemptsInput = input('number', 3, '');
    attemptsInput.min = '1'; attemptsInput.max = '10'; attemptsInput.step = '1';
    var timeoutInput = input('number', 180, '');
    timeoutInput.min = '20'; timeoutInput.max = '1800'; timeoutInput.step = '10';
    var essay = el('textarea');
    essay.placeholder = '留空 = 用大模型按题目要求起草（需要后端已配 KEDAIBIAO_LLM_*）';
    essay.setAttribute('aria-label', 'initial_essay');
    var resubmit = checkbox(false);

    var params = el('div', 'form-grid');
    params.appendChild(fieldWrap('目标分（0~100）', targetInput, '达标就停，不会再交'));
    params.appendChild(fieldWrap('最多轮次（1~10）', attemptsInput, '含改写重交的轮数'));
    params.appendChild(fieldWrap('等分上限（秒）', timeoutInput, '开跑时等分，也用于「继续等待」'));
    var essayField = fieldWrap('初始正文', essay, '填了就用你的稿子开局；不填让大模型起草');
    essayField.classList.add('field-wide');
    params.appendChild(essayField);
    params.appendChild(fieldWrap('不等分直接重交', resubmit,
      '拿不到分数也按批注改写重交（只有允许反复提交的作业才有效）'));
    var paramsSec = el('div', 'section');
    paramsSec.appendChild(el('div', 'section-title', '参数'));
    paramsSec.appendChild(params);
    body.appendChild(paramsSec);

    /* --- 按钮 --- */
    var startBtn = btnArmored('开始提分循环', '再点一次：真的提交作文', 'btn-danger', function () {
      return startLoop();
    });
    var fetchBtn = btn('只取分（不提交）', 'btn-sm', function () { doFetch(); });
    var waitBtn = btn('继续等待', 'btn-sm btn-ghost', function () { doWait(); });
    var historyBtn = btn('历史轮次', 'btn-sm btn-ghost', function () { doHistory(); });
    var stopBtn = btn('停止任务', 'btn-sm btn-danger', function () { doCancel(); });
    stopBtn.hidden = true;
    var actions = el('div', 'auto-foot');
    [startBtn, fetchBtn, waitBtn, historyBtn, stopBtn].forEach(function (n) {
      actions.appendChild(n);
    });
    body.appendChild(actions);

    /* --- 状态 / 分数 / 轮次进度 / 日志 / 结论 / 每轮结果 / 补救结果 --- */
    var statusRow = el('div', 'pills');
    var statusSec = el('div', 'section');
    statusSec.appendChild(el('div', 'section-title', '状态'));
    statusSec.appendChild(statusRow);
    body.appendChild(statusSec);

    var scoreBox = el('div', '');
    body.appendChild(scoreBox);

    var stepsBox = el('div', 'steps');
    var stepsSec = el('div', 'section');
    stepsSec.appendChild(el('div', 'section-title', '轮次进度'));
    stepsSec.appendChild(stepsBox);
    body.appendChild(stepsSec);

    /* 结论放在日志上面：跑完先看到结果，再往下翻逐行日志 */
    var conclusionHost = el('div', '');
    body.appendChild(conclusionHost);

    var logView = el('pre', 'log-view');
    logView.textContent = '评分循环的日志会逐行出现在这里。';
    var logSec = el('div', 'section');
    logSec.appendChild(el('div', 'section-title', '日志'));
    logSec.appendChild(logView);
    body.appendChild(logSec);

    var attemptsBox = el('div', '');
    var attemptsSec = el('div', 'section');
    attemptsSec.appendChild(el('div', 'section-title', '每轮结果'));
    attemptsSec.appendChild(attemptsBox);
    body.appendChild(attemptsSec);

    var resultHost = el('div', '');
    var resultSec = el('div', 'section');
    resultSec.appendChild(el('div', 'section-title', '取分 / 等分 / 历史结果'));
    resultSec.appendChild(el('p', 'env-note',
      '这三个按钮都不提交作文，结果只显示在下面这块，不会重绘整个面板。'));
    resultSec.appendChild(resultHost);
    body.appendChild(resultSec);

    /* --- 小工具 --- */
    function numField(node, fallback, min, max) {
      var v = Number(node && node.value);
      if (!isFinite(v)) { v = fallback; }
      return Math.max(min, Math.min(max, v));
    }

    function appendLog(line) {
      var row = el('div');
      var stamp = line && line.at ? time(line.at) : '';
      row.appendChild(el('span', 't', stamp ? stamp.slice(11) + '  ' : '  '));
      row.appendChild(el('span', (line && line.level) || 'info', (line && line.text) || ''));
      logView.appendChild(row);
      while (logView.childNodes.length > 400) { logView.removeChild(logView.firstChild); }
      logView.scrollTop = logView.scrollHeight;
    }

    function clearLog() {
      clear(logView);
      renderedEvents = 0;
    }

    function resetStart() {
      startBtn.disabled = false;
      startBtn.classList.remove('btn-primary');
      startBtn.textContent = '开始提分循环';
    }

    function setBusy(busy) {
      fetchBtn.disabled = !!busy;
      waitBtn.disabled = !!busy;
      stopBtn.hidden = !busy;
    }

    function stopWatch() {
      if (watcher) { watcher.stop(); watcher = null; }
    }

    function lastEid() {
      var list = (currentJob && currentJob.attempts) || [];
      for (var i = list.length - 1; i >= 0; i--) {
        if (list[i] && list[i].eid) { return String(list[i].eid); }
      }
      return '';
    }

    function bestScoreOf(job, attempts) {
      if (job && typeof job.best_score === 'number') { return job.best_score; }
      for (var i = attempts.length - 1; i >= 0; i--) {
        if (typeof attempts[i].score === 'number') { return attempts[i].score; }
      }
      return null;
    }

    /* --- 绘制 --- */
    function paintScore(job, attempts) {
      clear(scoreBox);
      var best = bestScoreOf(job || {}, attempts);
      if (best === null) {
        scoreBox.appendChild(el('p', 'env-note',
          '还没有可用的分数：等批改出分后，这里显示当前最高分与目标分的差距。'));
        return;
      }
      scoreBox.appendChild(completionProgress('当前最高分', best, 100,
        '目标 ' + num(job.target_score) + ' 分 · 已出结果 ' + attempts.length +
        ' 轮 / 上限 ' + num(job.max_attempts) + ' 轮'));
    }

    function paintSteps(job, attempts) {
      clear(stepsBox);
      attempts.forEach(function (a) {
        var isBase = Number(a.attempt) === 0;
        var ok = a.graded === true && a.score !== null && a.score !== undefined;
        var item = el('span', 'step ' + (ok ? 'ok' : 'fail'));
        item.appendChild(el('span', '', isBase ? '现有稿件' : ('第 ' + a.attempt + ' 轮')));
        item.appendChild(el('span', 'detail', ok ? (num(a.score) + ' 分') : (a.raw_state || '未出分')));
        stepsBox.appendChild(item);
      });
      if (!attempts.length) {
        stepsBox.appendChild(el('span', 'step',
          job && job.status === 'running' ? '读已有稿件 / 等第一轮结果…' : '还没开始'));
      }
      if (job && job.status !== 'running') {
        var reached = job.stop_reason === 'reached';
        var chip = el('span', 'step ' + (reached ? 'ok' : 'fail'));
        chip.appendChild(el('span', '', '结论'));
        chip.appendChild(el('span', 'detail',
          GRADE_STOP_TEXT[job.stop_reason] || job.status));
        stepsBox.appendChild(chip);
      }
    }

    function paintAttempts(attempts) {
      clear(attemptsBox);
      if (!attempts.length) {
        attemptsBox.appendChild(el('p', 'env-note', '循环还没产出轮次结果。'));
        return;
      }
      attempts.forEach(function (a) { attemptsBox.appendChild(gradeAttemptCard(a)); });
    }

    function paintJob(jobObj) {
      if (jobObj) { currentJob = jobObj; }
      var job = currentJob || {};
      var attempts = Array.isArray(job.attempts) ? job.attempts : [];
      var events = Array.isArray(job.events) ? job.events : [];

      /* 日志逐行：job 没有通用任务的 logs，用 events 翻；seen 只追加新增的 */
      for (var i = renderedEvents; i < events.length; i++) {
        appendLog(gradeEventLine(events[i]));
      }
      renderedEvents = events.length;

      clear(statusRow);
      statusRow.appendChild(statusPill(job.status));
      statusRow.appendChild(pill('目标 ' + num(job.target_score) + ' 分', 'pill-mute'));
      statusRow.appendChild(pill('最高 ' + ((job.best_score === null || job.best_score === undefined)
        ? '—' : num(job.best_score) + ' 分'), 'pill-mute'));
      statusRow.appendChild(pill('轮次 ' + attempts.length + ' / ' + num(job.max_attempts), 'pill-mute'));
      if (events.length) {
        statusRow.appendChild(pill('事件 ' + events.length + ' 条', 'pill-mute'));
      }

      paintScore(job, attempts);
      paintSteps(job, attempts);
      paintAttempts(attempts);
      setBusy(job.status === 'running');
    }

    function renderConclusion(jobObj) {
      clear(conclusionHost);
      if (!jobObj || jobObj.status === 'running') { return; }
      var reason = jobObj.stop_reason || (jobObj.status === 'failed' ? 'error' : '');
      var kind = reason === 'reached' ? 'pill-ok'
        : ((reason === 'error' || !reason) ? 'pill-danger' : 'pill-warn');
      var sec = el('div', 'section');
      sec.appendChild(el('div', 'section-title', '结论'));
      var row = el('div', 'pills');
      row.appendChild(pill(GRADE_STOP_TEXT[reason] || reason || jobObj.status, kind));
      if (jobObj.best_score !== null && jobObj.best_score !== undefined) {
        row.appendChild(pill('最高 ' + num(jobObj.best_score) + ' 分', 'pill-mute'));
      }
      sec.appendChild(row);
      if (jobObj.message) { sec.appendChild(el('p', 'env-note', jobObj.message)); }
      if (reason === 'timeout' || reason === 'unavailable' || reason === 'error') {
        sec.appendChild(el('p', 'env-note',
          '稿子已经交了，下面的补救都不重新提交：「只取分」再读一次；'
          + '「继续等待」按上面的等分上限再等一段时间；「历史轮次」看本地记录。'
          + '如果这个作业允许反复提交，勾上「不等分直接重交」再开一次循环。'));
      }
      conclusionHost.appendChild(sec);
    }

    /* --- 轮询 --- */
    function watch(jobId) {
      stopWatch();
      watcher = watchJob(jobId, {
        intervalMs: 1200,
        guard: function () {
          /* 面板收起、检查器换了内容或关了，就停表，别留空转的 setInterval */
          if (!alive || !host.isConnected || !$('inspector') || $('inspector').hidden) {
            stopWatch();
            return false;
          }
          return true;
        },
        onJob: function (jobObj) { paintJob(jobObj); },
        onDone: function (jobObj) {
          resetStart();
          setBusy(false);
          renderConclusion(jobObj);
          toast(jobObj.status === 'failed'
            ? ('评分循环结束：' + (GRADE_STOP_TEXT[jobObj.stop_reason] || jobObj.status))
            : ('评分循环结束：' + (jobObj.message || GRADE_STOP_TEXT[jobObj.stop_reason] || '完成')));
        },
        onError: function (e) {
          clear(statusRow);
          statusRow.appendChild(pill('轮询失败', 'pill-warn'));
          appendLog({ level: 'warn', text: '轮询失败：' + describe(e) });
        }
      });
    }

    /* --- 动作 --- */
    function startLoop() {
      var target = numField(targetInput, 90, 1, 100);
      var rounds = Math.round(numField(attemptsInput, 3, 1, 10));
      var timeout = numField(timeoutInput, 180, 20, 1800);
      targetInput.value = String(target);
      attemptsInput.value = String(rounds);
      timeoutInput.value = String(timeout);

      var payload = {
        platform: platName,
        assignment_id: rid,
        target_score: target,
        max_attempts: rounds,
        analysis_timeout: timeout,
        initial_essay: String(essay.value || '').trim(),
        resubmit_without_score: !!resubmit.querySelector('input').checked
      };
      if (!payload.initial_essay) {
        toast('没填初始正文：后端会尝试用大模型起草（未配 LLM 会 400）');
      }

      stopWatch();
      currentJob = null;
      clearLog();
      clear(conclusionHost);
      clear(attemptsBox);
      clear(resultHost);
      clear(statusRow);
      statusRow.appendChild(pill('正在启动…', 'pill-accent'));
      appendLog({
        level: 'info',
        text: '$ POST /api/essays/grade-loop ' + truncate(JSON.stringify(payload), 220)
      });

      return post('/api/essays/grade-loop', payload).then(function (job) {
        if (!job || !job.job_id) {
          var bad = new Error('后端没有返回 job_id');
          bad.kind = 'internal';
          throw bad;
        }
        currentJob = job;
        appendLog({
          level: 'ok',
          text: '→ 任务 ' + job.job_id + ' 已启动，每 1.2 秒读一次 /api/jobs/' + job.job_id
        });
        watch(job.job_id);
      }).catch(function (e) {
        resetStart();
        setBusy(false);
        clear(statusRow);
        statusRow.appendChild(pill('启动失败', 'pill-danger'));
        appendLog({ level: 'error', text: '✗ ' + describe(e) });
        toast(describe(e));
      });
    }

    function doFetch() {
      clear(resultHost);
      resultHost.appendChild(loadingBlock());
      fetchBtn.disabled = true;
      post('/api/essays/fetch', {
        platform: platName, assignment_id: rid, trigger_analysis: true
      }).then(function (snap) {
        clear(resultHost);
        var title = '取分 · ' + rid + (snap && snap.eid ? ' / ' + snap.eid : '');
        resultHost.appendChild(renderEnvelope(gradeSnapshotEnvelope(snap, title)));
        if (snap && snap.graded) {
          toast('取到分数：' + num(snap.score) + ' 分');
          if (snap.score !== null && snap.score !== undefined) {
            clear(scoreBox);
            scoreBox.appendChild(completionProgress('当前分数', snap.score, 100,
              '来自「只取分」· 目标 ' + num(numField(targetInput, 90, 1, 100)) + ' 分'));
          }
        } else {
          toast('还没出分：可以「继续等待」或稍后再取');
        }
      }).catch(function (e) {
        clear(resultHost);
        resultHost.appendChild(errorBlock(e, doFetch));
      }).then(function () {
        fetchBtn.disabled = !!(currentJob && currentJob.status === 'running');
      });
    }

    function doWait() {
      var secs = Math.max(5, Math.min(1800, Number(timeoutInput.value) || 180));
      var eid = lastEid();
      clear(resultHost);
      resultHost.appendChild(loadingBlock());
      waitBtn.disabled = true;
      appendLog({
        level: 'info',
        text: '$ POST /api/essays/wait ' + rid + ' / ' + (eid || '自动取最新') +
          '，最多 ' + secs + ' 秒'
      });
      post('/api/essays/wait', {
        platform: platName, assignment_id: rid, eid: eid, timeout: secs
      }).then(function (res) {
        clear(resultHost);
        if (res && res.timed_out) {
          resultHost.appendChild(renderEnvelope({
            kind: 'message',
            title: '继续等待结果',
            note: '稿子已经交了，等待不涉及重新提交。',
            message: res.message || '还是没出分。'
          }));
          toast('还是没出分，可以再等一次或稍后取分');
          return;
        }
        resultHost.appendChild(renderEnvelope(
          gradeSnapshotEnvelope(res, '等分结果 · ' + rid)));
        if (res && res.graded) { toast('出分了：' + num(res.score) + ' 分'); }
      }).catch(function (e) {
        clear(resultHost);
        resultHost.appendChild(errorBlock(e, doWait));
      }).then(function () {
        waitBtn.disabled = !!(currentJob && currentJob.status === 'running');
      });
    }

    function doHistory() {
      clear(resultHost);
      resultHost.appendChild(loadingBlock());
      historyBtn.disabled = true;
      post('/api/essays/history', {
        platform: platName, assignment_id: rid
      }).then(function (data) {
        clear(resultHost);
        resultHost.appendChild(renderEnvelope(gradeHistoryEnvelope(data, rid)));
      }).catch(function (e) {
        clear(resultHost);
        resultHost.appendChild(errorBlock(e, doHistory));
      }).then(function () { historyBtn.disabled = false; });
    }

    function doCancel() {
      var job = currentJob;
      if (!job || !job.job_id) { return; }
      post('/api/jobs/' + encodeURIComponent(job.job_id) + '/cancel', {})
        .then(function () { toast('已请求停止 ' + job.job_id); })
        .catch(function (e) { toast(describe(e)); });
    }

    /* 首屏：还没开跑 */
    clear(statusRow);
    statusRow.appendChild(pill('尚未开始', 'pill-mute'));
    paintScore({}, []);
    paintSteps(null, []);
    paintAttempts([]);
  }

  /* ---- 干跑计划：逐题改答案 ----
     后端在通用提交链路（东软等）的干跑结果里带 editable=true，每题带
     type / options / choices / text。这里把它渲染成可编辑表单：
       - 单选/判断 → radio，多选 → checkbox，选项文案用平台题面；
       - 填空/简答 → 输入框，预填当前答案；
       - needs_human（置信度低于阈值或没答）的题额外给「我确认这题」勾选：
         勾了即使不改内容，也按人工答案提交（跳过 min_confidence 门槛）。
     collect() 只回传**改过或人工确认过**的题；没动的题仍由后端阈值把关，
     这样「确认计划」这件事不会顺手把低置信度的题一起放过去。 */
  var PLAN_TYPE_LABEL = {
    single: '单选', multiple: '多选', judge: '判断',
    fill: '填空', essay: '简答', unknown: '未知题型'
  };

  function planLetter(index) {
    return String.fromCharCode(65 + index);
  }

  function sameChoices(a, b) {
    var x = (a || []).slice().sort();
    var y = (b || []).slice().sort();
    if (x.length !== y.length) { return false; }
    for (var i = 0; i < x.length; i++) { if (x[i] !== y[i]) { return false; } }
    return true;
  }

  function planEditor(env) {
    if (!isObj(env) || env.editable !== true) { return null; }
    var answers = Array.isArray(env.answers) ? env.answers : [];
    if (!answers.length) { return null; }

    var node = el('div', 'plan-editor');
    var entries = [];
    var dirtyText = el('span', 'hint-text', '');

    if (env.title || env.label || env.platform) {
      var titleRow = el('div', 'plan-head');
      if (env.title) { titleRow.appendChild(el('span', 'plan-title', String(env.title))); }
      if (env.label) { titleRow.appendChild(pill(String(env.label), 'pill-warn')); }
      if (env.platform) { titleRow.appendChild(pill(String(env.platform), 'pill-accent')); }
      node.appendChild(titleRow);
    }
    if (env.note) { node.appendChild(el('p', 'env-note', String(env.note))); }

    var chips = el('div', 'pills');
    chips.appendChild(pill('共 ' + answers.length + ' 题', 'pill-mute'));
    chips.appendChild(pill('已答 ' + dash(env.answered), 'pill-ok'));
    var humanCount = answers.filter(function (a) { return a && a.needs_human; }).length;
    if (humanCount) { chips.appendChild(dotPill('需人工确认 ' + humanCount, 'pill-warn')); }
    if (env.min_confidence !== undefined && env.min_confidence !== null) {
      var confText = '最低置信度 ' + env.min_confidence;
      if (env.threshold !== undefined && env.threshold !== null) {
        confText += ' / 阈值 ' + env.threshold;
      }
      chips.appendChild(pill(confText, 'pill-mute'));
    }
    chips.appendChild(dirtyText);
    node.appendChild(chips);
    node.appendChild(el('p', 'env-note',
      '下面的答案可以直接改：选择题点选项，填空/简答改文字。' +
      '只有改过或勾了「我确认」的题按人工答案提交，其余仍受置信度门槛约束。'));

    function refreshDirty() {
      var count = entries.filter(function (entry) {
        return entry.ack || entry.changed();
      }).length;
      dirtyText.textContent = count ? '已改 ' + count + ' 题' : '还没改任何题';
      dirtyText.className = count ? 'hint-text plan-dirty' : 'hint-text';
    }

    answers.forEach(function (q, qi) {
      q = isObj(q) ? q : {};
      var options = Array.isArray(q.options) ? q.options.slice() : [];
      var qtype = String(q.type || 'unknown');
      /* 判断题没给选项时用「对 / 错」兜底，否则人没法点。 */
      if (!options.length && qtype === 'judge') { options = ['对', '错']; }
      var textMode = options.length === 0;
      var original = {
        choices: Array.isArray(q.choices) ? q.choices.slice() : [],
        text: String(q.text || '')
      };
      var index = q.index || (qi + 1);
      var entry = { index: index, ack: false };
      var card = el('div', 'plan-card');

      var head = el('div', 'plan-card-head');
      head.appendChild(el('span', 'plan-qno', '第 ' + index + ' 题'));
      if (qtype !== 'unknown') { head.appendChild(pill(PLAN_TYPE_LABEL[qtype] || qtype, 'pill-mute')); }
      if (q.needs_human) { head.appendChild(pill('需人工确认', 'pill-warn')); }
      var confValue = (typeof q.confidence === 'number') ? q.confidence.toFixed(2) : '—';
      head.appendChild(el('span', 'hint-text',
        '置信度 ' + confValue + ' · 来源 ' + dash(q.source)));
      card.appendChild(head);
      card.appendChild(el('div', 'plan-stem', q.stem || '（平台没给题面）'));

      var read;
      if (textMode) {
        var area = el('textarea', 'plan-input');
        area.rows = 2;
        area.value = original.text;
        area.placeholder = '人工填写答案';
        area.addEventListener('input', refreshDirty);
        card.appendChild(area);
        read = function () { return { choices: [], text: String(area.value || '').trim() }; };
        entry.changed = function () { return read().text !== original.text.trim(); };
      } else {
        var group = el('div', 'plan-options' + (qtype === 'multiple' ? ' multi' : ''));
        var boxes = [];
        options.forEach(function (text, oi) {
          var label = el('label', 'plan-opt');
          var input = el('input', '');
          input.type = qtype === 'multiple' ? 'checkbox' : 'radio';
          input.name = 'plan-q' + index;
          input.value = String(oi);
          input.checked = original.choices.indexOf(oi) >= 0;
          input.addEventListener('change', refreshDirty);
          label.appendChild(input);
          label.appendChild(el('span', '',
            planLetter(oi) + '. ' + (String(text || '').trim() || '（空选项）')));
          boxes.push(input);
          group.appendChild(label);
        });
        card.appendChild(group);
        read = function () {
          var picked = [];
          boxes.forEach(function (box) { if (box.checked) { picked.push(Number(box.value)); } });
          return { choices: picked, text: '' };
        };
        entry.changed = function () { return !sameChoices(read().choices, original.choices); };
      }
      entry.read = read;

      if (q.needs_human) {
        var ackLabel = el('label', 'plan-ack');
        var ack = el('input', '');
        ack.type = 'checkbox';
        ack.addEventListener('change', function () {
          entry.ack = ack.checked;
          refreshDirty();
        });
        ackLabel.appendChild(ack);
        ackLabel.appendChild(el('span', '', '这题我人工确认过（提交时跳过置信度门槛）'));
        card.appendChild(ackLabel);
      }

      entries.push(entry);
      node.appendChild(card);
    });

    refreshDirty();

    return {
      node: node,
      collect: function () {
        var out = [];
        entries.forEach(function (entry) {
          if (!entry.ack && !entry.changed()) { return; }
          var value = entry.read();
          out.push({ index: entry.index, choices: value.choices, text: value.text });
        });
        return out;
      }
    };
  }

  function openSubmitFlow(platName, assignmentId, row, canSubmit) {
    if (!canSubmit) {
      openInspector({
        title: '该平台尚未实现提交',
        sub: platName + ' · ' + assignmentId,
        body: stateBlock('gate', '提交入口已禁用',
          '适配器没有声明 submit 能力（或曾经返回 501 not_supported）。界面不会假装成功。')
      });
      return;
    }
    openInspector({
      title: '提交计划（干跑）',
      sub: platName + ' · ' + assignmentId,
      body: loadingBlock()
    });
    post('/api/assignments/submit', {
      platform: platName, assignment_id: assignmentId, confirm: false
    }).then(function (env) {
      var body = el('div', '');
      body.appendChild(el('div', 'banner', '这是干跑：只规划、不写平台。核对下面的每题答案与置信度，再决定是否真交。'));
      /* 通用链路（东软等）的计划可以直接改答案；原生链路仍是只读信封。 */
      var editor = planEditor(env);
      if (editor) {
        body.appendChild(editor.node);
      } else {
        body.appendChild(renderEnvelope(env, {}));
      }
      var guard = { armed: false };
      var live = btn('确认真提交', 'btn-danger', function () {
        if (!guard.armed) {
          guard.armed = true;
          live.textContent = '再点一次，真的提交';
          live.classList.add('btn-primary');
          return;
        }
        live.disabled = true;
        live.textContent = '提交中…';
        post('/api/assignments/submit', {
          platform: platName, assignment_id: assignmentId, confirm: true,
          /* 只带上改过/人工确认过的题；没改的题后端照旧走阈值门槛。 */
          answers: editor ? editor.collect() : undefined
        }).then(function (res) {
          var finished = res && (res.server_finish === true || res.server_finish === 1 ||
            res.server_finish === '1');
          if (finished) {
            state.doneOverrides[platName + '|' + assignmentId] = true;
          }
          var resultBody = el('div', '');
          resultBody.appendChild(renderEnvelope(res, {}));
          if (finished) {
            var scored = Array.isArray(res.score) ? res.score.length : 0;
            resultBody.appendChild(el('p', 'env-note', scored
              ? '平台已记账：finish=True。列表里这一行的状态会显示为「已完成」。'
              : '平台已记账：finish=True。这组没有计分点（纯学习 / 内容组），' +
                '所以得分率是 0.0 —— 它不等于没交，看 finish 才对。'));
          }
          openInspector({
            title: '提交结果',
            sub: platName + ' · ' + assignmentId,
            body: resultBody,
            foot: [btn('关闭', 'btn-sm', closeInspector)]
          });
          toast(finished
            ? '已提交 · 回读 finish=True，列表状态已更新为已完成'
            : '已提交，但平台没回读 finish：列表状态请点「读取完成状态」核对');
          if (typeof assignmentsReload === 'function') { assignmentsReload(); }
        }).catch(function (e) {
          openInspector({
            title: '提交失败',
            sub: platName + ' · ' + assignmentId,
            body: errorBlock(e),
            foot: [btn('关闭', 'btn-sm', closeInspector)]
          });
        });
      });
      openInspector({
        title: '提交计划（干跑）',
        sub: platName + ' · ' + assignmentId,
        body: body,
        foot: [live, btn('取消', 'btn-sm btn-ghost', closeInspector)]
      });
    }).catch(function (e) {
      openInspector({
        title: '干跑失败',
        sub: platName + ' · ' + assignmentId,
        body: errorBlock(e),
        foot: [btn('关闭', 'btn-sm', closeInspector)]
      });
    });
  }

  /* ============================================ 品课：计划 → 复核 → 提交 */

  /* 为什么单独一块：品课的 submit() 有三道硬守卫，其中一道要求
     「人已经复核过这份计划」（adapter.confirm_plan）。所以界面必须把计划摊开、
     让人看过、再带着 digest 提交；行内「提交」永远会被守卫拒绝。
     后端接口：POST /api/platforms/pinco/plan、POST /api/platforms/pinco/submit-reviewed。 */
  function openPincoPlan(courseField) {
    var course = courseField ? String(courseField.value || '').trim() : '';
    openInspector({
      title: '提交计划（品课）',
      sub: '先看计划 → 复核 → 再提交；计划变了会被拒绝',
      body: loadingBlock()
    });

    function load() {
      post('/api/platforms/pinco/plan', course ? { course: course } : {})
        .then(render)
        .catch(function (e) {
          openInspector({
            title: '提交计划（品课）',
            sub: '计划生成失败',
            body: errorBlock(e, load),
            foot: [btn('关闭', 'btn-sm', closeInspector)]
          });
        });
    }

    function render(env) {
      var rows = (env && env.rows) || [];
      var counts = (env && env.counts) || {};
      var digest = String((env && env.digest) || '');

      var body = el('div', '');
      body.appendChild(el('p', 'env-note',
        '这是只读计划：还没有发出任何写请求。复核每一行的答案与阻断原因，'
        + '确认后才点「复核并提交」。提交时会把计划的 digest 一起带上 —— '
        + '如果期间计划变了（新作业、答案更新），后端会拒绝并要求重新生成。'));

      /* 计数 */
      var chips = el('div', 'pills');
      chips.appendChild(pill('共 ' + (counts.total || rows.length) + ' 项', 'pill-mute'));
      chips.appendChild(dotPill('可提交 ' + (counts.planned || 0), 'pill-ok'));
      if (counts.needs_human) {
        chips.appendChild(dotPill('需要人工 ' + counts.needs_human, 'pill-warn'));
      }
      if (counts.skipped) { chips.appendChild(dotPill('跳过 ' + counts.skipped, 'pill-mute')); }
      if (counts.failed) { chips.appendChild(dotPill('失败 ' + counts.failed, 'pill-danger')); }
      body.appendChild(chips);

      /* digest：提交要原样带回，界面上要能核对 */
      var digestRow = el('div', 'pills');
      digestRow.appendChild(pill('计划摘要', 'pill-mute'));
      digestRow.appendChild(el('code', 'mono', digest || '—'));
      var digestSec = el('div', 'section');
      digestSec.appendChild(el('div', 'section-title', '计划摘要（提交时原样带回）'));
      digestSec.appendChild(digestRow);
      body.appendChild(digestSec);

      /* 计划表 */
      if (!rows.length) {
        body.appendChild(stateBlock('', '计划里没有作业',
          '指定课程下没有可提交的作业；换个课程号或先到平台确认。'));
      } else {
        var list = el('div', 'list');
        rows.forEach(function (row) {
          list.appendChild(pincoPlanRow(row, digest));
        });
        var sec = el('div', 'section');
        sec.appendChild(el('div', 'section-title', '逐项复核'));
        sec.appendChild(list);
        body.appendChild(sec);
      }

      if (env && env.note) { body.appendChild(el('p', 'env-note', env.note)); }
      if (env && (env.human_reasons || []).length) {
        var sec2 = el('div', 'section');
        sec2.appendChild(el('div', 'section-title', '需要人工的原因'));
        sec2.appendChild(listOf(env.human_reasons));
        body.appendChild(sec2);
      }

      openInspector({
        title: env.title || '提交计划（品课）',
        sub: '复核后提交 · ' + (digest ? 'digest ' + digest.slice(0, 12) + '…' : '无 digest'),
        body: body,
        foot: [btn('重新生成计划', 'btn-sm', load),
               btn('关闭', 'btn-sm btn-ghost', closeInspector)]
      });
    }

    function pincoPlanRow(row, digest) {
      var state = String((row && row.state) || '');
      var item = el('div', 'rowx');
      item.appendChild(el('span', 'mark ' + (
        state === 'planned' ? 'ok'
          : state === 'needs_human' ? 'warn'
            : state === 'failed' ? 'err' : '')));

      var main = el('div', 'rowx-main');
      var title = el('div', 'rowx-title');
      title.appendChild(el('span', '', row.title || row.assignment_id || '未命名'));
      title.appendChild(pill(row.state_label || state || '—',
        state === 'planned' ? 'pill-ok'
          : state === 'needs_human' ? 'pill-warn'
            : state === 'failed' ? 'pill-danger' : 'pill-mute'));
      main.appendChild(title);
      var meta = el('div', 'rowx-meta');
      if (row.assignment_id) { meta.appendChild(el('span', 'mono', row.assignment_id)); }
      if (row.course) { meta.appendChild(el('span', '', row.course)); }
      meta.appendChild(el('span', '', (row.question_count || 0) + ' 题'));
      if (row.human_count) { meta.appendChild(el('span', '', '待人工 ' + row.human_count)); }
      if (row.digest) { meta.appendChild(el('span', 'mono', '摘要 ' + row.digest)); }
      if (row.reason && row.reason !== '—') { meta.appendChild(el('span', '', row.reason)); }
      main.appendChild(meta);
      item.appendChild(main);

      var side = el('div', 'rowx-side');
      if (state === 'planned' && digest) {
        side.appendChild(btn('复核并提交', 'btn-sm btn-danger', function () {
          confirmPincoSubmit(row, digest);
        }));
      } else {
        side.appendChild(el('span', 'hint-text', state === 'needs_human' ? '先补答案' : '不可提交'));
      }
      item.appendChild(side);
      return item;
    }

    /* 两步确认：第一下只是「准备复核」，第二下才真的带 digest 提交。 */
    function confirmPincoSubmit(row, digest) {
      var body = el('div', '');
      body.appendChild(defs([
        ['作业', row.title || '—', 'strong'],
        ['任务号', row.assignment_id || '—'],
        ['课程', row.course || '—'],
        ['题目数', String(row.question_count || 0)],
        ['计划摘要', digest]
      ]));
      body.appendChild(el('p', 'env-note',
        '提交是写操作，不可撤销。后端会重新生成计划并核对摘要：'
        + '只要内容有任何变化就会拒绝，届时请重新生成计划再看一遍。'));

      var armed = { on: false };
      var msg = el('span', 'hint-text', '');
      var go = btn('确认提交', 'btn-danger', function () {
        if (!armed.on) {
          armed.on = true;
          go.textContent = '再点一次，真的提交';
          go.classList.add('btn-primary');
          return;
        }
        go.disabled = true;
        msg.textContent = '提交中…';
        post('/api/platforms/pinco/submit-reviewed', {
          assignment_id: row.assignment_id,
          digest: digest
        }).then(function (res) {
          var okBody = el('div', '');
          okBody.appendChild(renderEnvelope(res, {}));
          openInspector({
            title: '提交结果',
            sub: '品课 · ' + (row.assignment_id || ''),
            body: okBody,
            foot: [btn('关闭', 'btn-sm', closeInspector)]
          });
          toast(res && res.submitted ? '已提交' : '提交返回未标记成功，请看结果');
        }).catch(function (e) {
          msg.textContent = describe(e);
          go.disabled = false;
          go.textContent = '确认提交';
          go.classList.remove('btn-primary');
          armed.on = false;
        });
      });
      var rowWrap = el('div', 'pills');
      rowWrap.appendChild(msg);

      openInspector({
        title: '复核后提交',
        sub: '品课 · ' + (row.assignment_id || ''),
        body: append(body, rowWrap),
        foot: [go, btn('返回计划', 'btn-sm btn-ghost', loadPincoAgain)]
      });

      function loadPincoAgain() {
        openPincoPlanFromScratch();
      }
    }

    function openPincoPlanFromScratch() {
      var cf = document.createElement('input');
      cf.value = course;
      openPincoPlan(cf);
    }

    load();
  }

  function openBatchSubmit(platName) {
    var limit = el('input');
    limit.type = 'number'; limit.value = '50'; limit.min = '0'; limit.step = '1';
    limit.className = 'mono';
    var interval = el('input');
    interval.type = 'number'; interval.value = '8'; interval.min = '0.5'; interval.max = '120';
    interval.step = '0.5'; interval.className = 'mono';
    var skip = el('input');
    skip.type = 'checkbox'; skip.checked = true;
    var skipWrap = el('label', 'switch');
    skipWrap.appendChild(skip);
    skipWrap.appendChild(el('span', '', '跳过已完成'));

    var grid = el('div', 'form-grid');
    function field(label, node, hint) {
      var f = el('div', 'field');
      f.appendChild(el('span', 'field-label', label));
      f.appendChild(node);
      if (hint) { f.appendChild(el('span', 'hint-text', hint)); }
      return f;
    }
    grid.appendChild(field('最多处理任务数', limit, '0 = 不限'));
    grid.appendChild(field('组间间隔（秒）', interval, '平台有限流，建议 ≥5'));
    var sf = el('div', 'field');
    sf.appendChild(el('span', 'field-label', '已完成处理'));
    sf.appendChild(skipWrap);
    grid.appendChild(sf);

    var guard = { armed: false };
    var liveBtn = btn('确认全部真交', 'btn-danger', function () {
      if (!guard.armed) {
        guard.armed = true;
        liveBtn.textContent = '再点一次，全部真交';
        liveBtn.classList.add('btn-primary');
        return;
      }
      start(true);
    });
    var dryBtn = btn('先干跑', 'btn-primary', function () { start(false); });

    function start(live) {
      dryBtn.disabled = true;
      liveBtn.disabled = true;
      post('/api/platforms/' + encodeURIComponent(platName) + '/batch-submit', {
        confirm: live,
        limit: Number(limit.value) || 0,
        interval: Number(interval.value) || 8,
        skip_done: skip.checked
      }).then(function (job) {
        toast(live ? '已启动真交任务' : '已启动干跑任务');
        followJob(job.job_id);
      }).catch(function (e) {
        dryBtn.disabled = false;
        liveBtn.disabled = false;
        toast(describe(e));
      });
    }

    var body = el('div', '');
    body.appendChild(el('div', 'banner', '干跑只读内容并做本地校验，不写平台；确认后才逐组提交。任务在后台运行，切页不中断。'));
    body.appendChild(grid);

    openInspector({
      title: '一键全部提交',
      sub: platName,
      body: body,
      foot: [dryBtn, liveBtn, btn('关闭', 'btn-sm btn-ghost', closeInspector)]
    });
  }

  function followJob(jobId) {
    openInspector({
      title: '任务 ' + jobId,
      sub: '后台运行中，进度每秒刷新',
      body: loadingBlock()
    });
    watchJob(jobId, {
      intervalMs: 1500,
      onJob: function (job) {
        var body = el('div', '');
        var head = el('div', 'pills');
        head.appendChild(statusPill(job.status));
        head.appendChild(pill(platformLabel(job.platform), 'pill-mute'));
        head.appendChild(pill(job.live ? '真提交' : '干跑', job.live ? 'pill-danger' : 'pill-warn'));
        body.appendChild(head);
        body.appendChild(defs([
          ['开始', time(job.started_at)],
          ['结束', time(job.finished_at)],
          ['耗时', elapsed(job.started_at, job.finished_at)],
          ['摘要', job.summary || '—']
        ]));
        if (job.error) {
          body.appendChild(el('div', 'banner error', job.error));
        }
        if (job.records && job.records.length) {
          var list = el('div', 'list');
          job.records.forEach(function (rec) {
            var row = el('div', 'rowx');
            row.appendChild(el('span', 'mark ' + (rec.status === 'succeeded' ? 'ok'
              : rec.status === 'failed' ? 'err' : '')));
            var m = el('div', 'rowx-main');
            m.appendChild(el('div', 'rowx-title mono', rec.assignment_id || '—'));
            if (rec.note) { m.appendChild(el('div', 'rowx-meta', rec.note)); }
            row.appendChild(m);
            var s = el('div', 'rowx-side');
            s.appendChild(statusPill(rec.status));
            row.appendChild(s);
            list.appendChild(row);
          });
          body.appendChild(panel('记录', list, { flush: true }));
        }
        openInspector({ title: '任务 ' + jobId, sub: 'job_id ' + jobId, body: body });
      },
      onDone: function (job) {
        toast('任务 ' + job.status);
      },
      onError: function (e, watcher) {
        watcher.stop();
        openInspector({ title: '任务 ' + jobId, body: errorBlock(e) });
      }
    });
  }

  function openPlatformLog(platName) {
    openInspector({
      title: '执行日志',
      sub: platName,
      body: loadingBlock()
    });
    function load() {
      get('/api/platforms/' + encodeURIComponent(platName) + '/logs', { limit: 200 })
        .then(function (body) {
          var logs = (body && body.logs) || [];
          var view = el('pre', 'log-view');
          if (!logs.length) {
            view.textContent = '这个平台还没有执行记录。调用动作、跑批或提交后会写入这里。';
          } else {
            logs.forEach(function (line) {
              var row = el('div');
              row.appendChild(el('span', 't', time(line.at).slice(11) + ' '));
              row.appendChild(el('span', line.level || 'info', line.text || ''));
              view.appendChild(row);
            });
          }
          openInspector({
            title: '执行日志',
            sub: platName + ' · ' + logs.length + ' / ' + ((body && body.total) || 0) + ' 行',
            body: view,
            foot: [btn('刷新', 'btn-sm', load), btn('关闭', 'btn-sm btn-ghost', closeInspector)]
          });
        }).catch(function (e) {
          openInspector({ title: '执行日志', sub: platName, body: errorBlock(e, load) });
        });
    }
    load();
  }
  /* ================================================================ 考勤 */

  /* 签到：场次详情 + 可选定位参数 + 两步确认（写操作，不可逆）。 */
  function openSignFlow(platName, row, onDone) {
    var termValue = String(row.__term || '');
    var lat = input('text', '', '纬度，例如 30.5123');
    var lng = input('text', '', '经度，例如 114.4123');
    var addr = input('text', '', '地址文本（可选）');
    var needLoc = String(row.location || '') === '需定位';
    var msg = el('span', 'hint-text', needLoc
      ? '这个场次要求定位：经纬度都要填，否则请求会被拦在发出之前。'
      : '这个场次不要求定位，坐标可以留空。');

    var body = el('div', '');
    body.appendChild(defs([
      ['场次号', row.attendance_id || '—', 'strong'],
      ['课程', row.course || '—'],
      ['类型', row.type || '—'],
      ['状态', row.status || '—'],
      ['定位', row.location || '—']
    ]));
    var grid = el('div', 'form-grid');
    grid.appendChild(fieldWrap('纬度', lat, ''));
    grid.appendChild(fieldWrap('经度', lng, ''));
    grid.appendChild(fieldWrap('地址', addr, '会随签到请求一起提交'));
    body.appendChild(grid);
    var msgRow = el('div', 'pills');
    msgRow.appendChild(msg);
    body.appendChild(msgRow);
    body.appendChild(el('p', 'env-note', needLoc
      ? '签到是写操作：提交后不能撤回。平台可能校验位置，坐标不对会失败。'
      : '签到是写操作：提交后不能撤回。'));

    var armed = { on: false };
    var go = btn('签到', 'btn-danger', function () {
      if (!armed.on) {
        armed.on = true;
        go.textContent = '再点一次，确认签到';
        go.classList.add('btn-primary');
        return;
      }
      if (needLoc && (!lat.value.trim() || !lng.value.trim())) {
        msg.textContent = '这个场次需要定位：请先填经纬度。';
        return;
      }
      go.disabled = true;
      msg.textContent = '提交中…';
      post('/api/platforms/' + encodeURIComponent(platName) + '/actions/sign', {
        attendance_id: row.attendance_id,
        term: termValue,
        latitude: lat.value.trim(),
        longitude: lng.value.trim(),
        address: addr.value.trim()
      }).then(function (env) {
        openInspector({
          title: '签到结果',
          sub: platName + ' · ' + (row.attendance_id || ''),
          body: renderEnvelope(env, {}),
          foot: [btn('关闭', 'btn-sm', closeInspector)]
        });
        toast('签到请求已提交，正在回读状态');
        if (onDone) { onDone(); }
      }).catch(function (e) {
        msg.textContent = describe(e);
      }).then(function () { go.disabled = false; });
    });

    openInspector({
      title: '签到',
      sub: platName + ' · ' + (row.course || ''),
      body: body,
      foot: [go, btn('取消', 'btn-sm btn-ghost', closeInspector)]
    });
  }

  /* 品课：签到码模式。没有场次列表，只有「老师发的链接/码」这一条路。 */
  function pageSigninCode(page, platName) {
    page.appendChild(pageHead({
      title: '考勤 · 签到码',
      badge: pill('签到是写操作', 'pill-danger'),
      sub: [scopeChip(), '把老师发的签到链接 / 签到码提交上去']
    }));

    var code = input('text', '', '/student/signin?attendanceCode=… 或直接填码');
    var bar = el('div', 'form-grid');
    var codeField = fieldWrap('签到码 / 签到链接', code, '整条链接也行，后台会解析出码');
    codeField.classList.add('field-wide');
    bar.appendChild(codeField);
    page.appendChild(panel('签到', bar));

    var host = el('div', '');

    var msg = el('span', 'hint-text', '');
    var dryBtn = btn('解析（干跑）', 'btn-primary', function () { send(false); });
    var armed = { on: false };
    var liveBtn = btn('确认签到', 'btn-danger', function () {
      if (!armed.on) {
        armed.on = true;
        liveBtn.textContent = '再点一次，真的签到';
        liveBtn.classList.add('btn-primary');
        return;
      }
      send(true);
    });
    var row = el('div', 'auto-foot');
    [dryBtn, liveBtn, msg].forEach(function (n) { row.appendChild(n); });
    page.appendChild(panel('操作', row, {
      foot: '先「解析（干跑）」：只解析签到码、不发送任何请求；确认无误后再点「确认签到」。'
    }));
    page.appendChild(host);

    function send(confirm) {
      var raw = String(code.value || '').trim();
      if (!raw) {
        msg.textContent = '先填签到码或链接。';
        return;
      }
      dryBtn.disabled = true;
      liveBtn.disabled = true;
      msg.textContent = confirm ? '提交中…' : '解析中…';
      post('/api/platforms/' + encodeURIComponent(platName) + '/actions/signin', {
        code: raw,
        confirm: confirm ? 1 : 0
      }).then(function (env) {
        msg.textContent = '';
        clear(host);
        /* 和上面的面板统一：结果也包一层 panel，标题吃信封自己的 */
        host.appendChild(panel(env && env.title ? env.title : '签到结果',
          renderEnvelope(env, { hideHead: true }),
          { flush: false }));
        host.firstChild.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        if (confirm) {
          toast('签到请求已发送，结果见上方');
        } else {
          armed.on = false;
          liveBtn.textContent = '确认签到';
          liveBtn.classList.remove('btn-primary');
          toast('解析通过，还没有发送签到');
        }
      }).catch(function (e) {
        msg.textContent = describe(e);
      }).then(function () {
        dryBtn.disabled = false;
        liveBtn.disabled = false;
      });
    }

    page.appendChild(hintBlock([
      '品课的签到没有「场次列表」：老师发一条带 attendanceCode 的链接，把它贴进来就行。',
      '签到码是滚动码，过期/换码后要重新取当次的链接；不能复用。',
      '「解析（干跑）」不发送任何请求；只有「确认签到」（点两次）才会真的提交。',
      '考勤统计以平台为准 —— 这里只负责把这一次签到发出去。'
    ]));
  }

  function pageAttendance(page, seq) {
    if (needsPlatformPage(page, seq)) { return; }
    var platName = state.platform;

    /* 有场次列表就按场次走；只有签到码动作就切到签到码模式。
       清单还没到手时先显示 loading 并等它 —— 否则会把「品课的签到码」
       误判成场次模式，渲染出一个空列表。 */
    var names = state.platformActions[platName];
    if (!names) {
      page.appendChild(pageHead({
        title: '考勤',
        sub: [scopeChip(), '正在确认这个平台的考勤方式']
      }));
      page.appendChild(panel(null, loadingBlock()));
      loadPlatformActions(platName).then(function () {
        if (seq === state.pageSeq) { renderPage(); }
      });
      return;
    }
    if (names.indexOf('attendance') < 0 && names.indexOf('signin') >= 0) {
      pageSigninCode(page, platName);
      return;
    }
    if (names.indexOf('attendance') < 0) {
      /* 兜底：两个动作都没有（正常不会走到这里，route 已经拦过） */
      page.appendChild(pageHead({ title: '考勤', sub: [scopeChip()] }));
      page.appendChild(stateBlock('gate', '这个平台没有考勤能力',
        '适配器没有声明 attendance / signin 动作，所以这里没有可用的考勤方式。'));
      return;
    }

    var termInput = el('input');
    termInput.type = 'text';
    termInput.className = 'mono';
    termInput.placeholder = '学期 id（留空 = 当前学期）';
    termInput.setAttribute('aria-label', 'term');

    var host = el('div', '');
    var reload = btn('刷新', 'btn-sm btn-primary', function () { load(); });

    page.appendChild(pageHead({
      title: '考勤',
      badge: pill('签到是写操作', 'pill-danger'),
      sub: [scopeChip(), '考勤场次与签到'],
      actions: [reload]
    }));

    var bar = el('div', 'form-grid');
    bar.appendChild(fieldWrap('学期 term（可选）', termInput, '留空就用账号的当前学期'));
    page.appendChild(panel('筛选', bar));
    page.appendChild(host);

    function load() {
      clear(host);
      host.appendChild(panel(null, loadingBlock()));
      var term = String(termInput.value || '').trim();
      post('/api/platforms/' + encodeURIComponent(platName) + '/actions/attendance',
        term ? { term: term } : {}).then(function (env) {
        if (seq !== state.pageSeq) { return; }
        clear(host);
        var rows = (env && env.rows) || [];
        if (!rows.length) {
          host.appendChild(panel(null, stateBlock('', '这个学期没有考勤场次',
            '换个学期试试，或确认账号的当前学期是否正确。')));
          return;
        }
        rows.forEach(function (row) { row.__term = term; });
        var signed = rows.filter(function (r) { return r.status === '已签'; }).length;
        var open = rows.filter(function (r) { return r.status === '可签'; }).length;
        host.appendChild(kpis([
          { label: '场次', value: rows.length },
          { label: '已签', value: signed },
          { label: '可签', value: open, sub: open ? '现在就能签' : '没有进行中的场次' },
          { label: '已结束', value: rows.length - signed - open, muted: true }
        ]));
        host.appendChild(panel(env.title || '考勤场次',
          renderEnvelope(env, {
            hideHead: true,
            empty: '这个学期没有考勤记录。',
            rowAction: function (row) {
              if (row.status !== '可签') {
                return el('span', 'hint-text', row.status === '已签' ? '已签' : '—');
              }
              return btn('签到', 'btn-sm btn-danger', function () {
                openSignFlow(platName, row, function () {
                  // 回读：code=0 不等于签上，要以平台状态为准
                  setTimeout(load, 1200);
                });
              });
            }
          }),
          { aside: pill(rows.length + ' 场', 'pill-mute') }));
        host.appendChild(hintBlock([
          '考勤列表来自平台的 attendance 动作（只读）；「签到」是写操作，提交后不可撤回。',
          '平台返回 code=0 不代表签到落库：签完会自动回读一次，状态变成「已签」才算真的签上。',
          '需要定位的场次要填经纬度，否则请求会被拦在发出之前。',
          '目前只有东软（neusoft）声明了考勤与签到动作；其它平台会提示没有这个动作。'
        ]));
      }).catch(function (e) {
        if (seq !== state.pageSeq) { return; }
        clear(host);
        if (e && e.kind === 'unknown_action') {
          host.appendChild(panel(null, stateBlock('gate', '这个平台没有考勤动作',
            '考勤 / 签到由适配器声明；该平台还没有实现，或不在支持的平台之列。')));
          return;
        }
        host.appendChild(panel(null, errorBlock(e, load)));
      });
    }

    load();
  }

  /* ================================================================ 题库 */

  function pageBank(page, seq) {
    if (needsPlatformPage(page, seq)) { return; }

    page.appendChild(pageHead({
      title: '题库',
      sub: [scopeChip(), '判分准确率与来源构成'],
      actions: [btn('刷新', 'btn-sm', function () { renderPage(); })]
    }));

    var host = el('div', '');
    page.appendChild(host);
    host.appendChild(panel(null, loadingBlock()));

    get('/api/bank', { platform: state.platform }).then(function (b) {
      if (seq !== state.pageSeq) { return; }
      clear(host);

      host.appendChild(kpis([
        { label: '题目总数', value: num(b.total) },
        { label: '已判分', value: num(b.graded) },
        { label: '正确', value: num(b.correct) },
        { label: '准确率', value: b.accuracy === null || b.accuracy === undefined ? '—' : pct(b.accuracy, 1),
          sub: b.graded ? ('样本 ' + num(b.graded)) : '还没有被平台判过分的题' },
        { label: '未判分', value: num(b.ungraded), muted: true }
      ]));

      var split = el('div', 'split');
      var left = el('div', 'main-col');
      var aside = el('div', 'aside');

      var sources = Object.keys(b.by_source || {}).map(function (k) {
        return { label: k, value: b.by_source[k] };
      }).sort(function (x, y) { return y.value - x.value; });

      left.appendChild(panel('按来源构成',
        sources.length ? bars(sources, { percent: true })
          : stateBlock('', '题库还是空的', '跑一次作业后，题目与答案会沉淀到这里。'),
        { aside: pill(Object.keys(b.by_source || {}).length + ' 类', 'pill-mute') }));

      var plats = Object.keys(b.by_platform || {}).map(function (k) {
        return { label: platformLabel(k), value: b.by_platform[k] };
      }).sort(function (x, y) { return y.value - x.value; });

      left.appendChild(panel('按平台（全量）',
        plats.length ? bars(plats, { percent: true })
          : stateBlock('', '还没有任何平台的题目'),
        { aside: pill('始终全量', 'pill-mute') }));

      aside.appendChild(panel('存储', defs([
        ['数据库', b.db_path],
        ['当前作用域', b.platform || '全部平台']
      ])));

      split.appendChild(left);
      split.appendChild(aside);
      host.appendChild(split);

      host.appendChild(hintBlock([
        '题库是跨学期沉淀的：同一个题在不同平台、不同学期出现时可以直接复用。',
        '「准确率」只统计被平台判过分的题；没有判分记录时显示「—」，不按 0% 处理。',
        '题库没有导出端点，这里只显示数据库位置。'
      ]));
    }).catch(function (e) {
      if (seq !== state.pageSeq) { return; }
      clear(host);
      host.appendChild(panel(null, errorBlock(e, function () { renderPage(); })));
    });
  }

  /* ================================================================ 运行日志 */

  function pageLogs(page, seq) {
    var active = 'runs';
    var tabs = [
      ['runs', '跑批'], ['subs', '提交'], ['jobs', '任务'],
      ['events', '事件流'], ['plog', '平台日志']
    ];

    var tabBar = el('div', 'tabs');
    tabBar.setAttribute('role', 'tablist');
    page.appendChild(pageHead({
      title: '运行日志',
      sub: [scopeChip(), '跑批 / 提交 / 任务 / 事件'],
      actions: [btn('刷新', 'btn-sm', function () { show(active, true); })]
    }));

    var buttons = tabs.map(function (spec) {
      var b = el('button', 'tab', spec[1]);
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(spec[0] === active));
      b.addEventListener('click', function () { show(spec[0]); });
      tabBar.appendChild(b);
      return { id: spec[0], node: b };
    });
    page.appendChild(tabBar);

    var host = el('div', '');
    page.appendChild(host);

    function markTabs() {
      buttons.forEach(function (b) {
        b.node.setAttribute('aria-selected', String(b.id === active));
      });
    }

    function show(tab, silent) {
      active = tab;
      markTabs();
      if (!silent) { clear(host); host.appendChild(panel(null, loadingBlock())); }
      if (tab === 'runs') { return loadRuns(silent); }
      if (tab === 'subs') { return loadSubs(silent); }
      if (tab === 'jobs') { return loadJobs(silent); }
      if (tab === 'events') { return loadEvents(silent); }
      return loadPlatformLogs(silent);
    }

    function guard() { return seq === state.pageSeq && active; }

    function loadRuns(silent) {
      return get('/api/runs', { limit: 50, platform: state.platform }).then(function (body) {
        if (seq !== state.pageSeq || active !== 'runs') { return; }
        var runs = (body && body.runs) || [];
        clear(host);
        var box = el('div', '');
        if (!runs.length) {
          box.appendChild(panel(null, stateBlock('', '还没有跑批记录',
            '跑批会把每个作业的处理结果写成一条记录。')));
        } else {
          var list = el('div', 'list');
          runs.forEach(function (run) {
            var row = el('div', 'rowx');
            row.appendChild(el('span', 'mark ' + (run.status === 'succeeded' ? 'ok'
              : run.status === 'failed' ? 'err'
              : run.status === 'running' ? 'run' : '')));
            var m = el('div', 'rowx-main');
            var title = el('div', 'rowx-title');
            title.appendChild(el('span', 'mono', run.assignment_id || '—'));
            if (!state.platform) { title.appendChild(pill(run.platform, 'pill-mute')); }
            m.appendChild(title);
            var meta = el('div', 'rowx-meta');
            meta.appendChild(el('span', '', '#' + run.id));
            meta.appendChild(el('span', '', '尝试 ' + num(run.attempts)));
            meta.appendChild(el('span', '', time(run.started_at)));
            meta.appendChild(el('span', '', '耗时 ' + elapsed(run.started_at, run.finished_at)));
            if (run.error) { meta.appendChild(el('span', '', truncate(run.error, 80))); }
            m.appendChild(meta);
            row.appendChild(m);
            var side = el('div', 'rowx-side');
            side.appendChild(statusPill(run.status));
            if (run.detail) {
              side.appendChild(btn('明细', 'btn-sm btn-ghost', function () {
                var detail = run.detail;
                if (typeof detail === 'string') {
                  try { detail = JSON.parse(detail); } catch (e) { /* 保持原文 */ }
                }
                var pre = el('pre', 'code',
                  typeof detail === 'string' ? detail : JSON.stringify(detail, null, 2));
                openInspector({
                  title: '跑批 detail',
                  sub: 'run #' + run.id + ' · ' + (run.assignment_id || ''),
                  body: pre
                });
              }));
            }
            row.appendChild(side);
            list.appendChild(row);
          });
          box.appendChild(panel('跑批历史', list, {
            flush: true,
            aside: pill(runs.length + ' 条', 'pill-mute')
          }));
        }
        host.appendChild(box);
      }).catch(function (e) {
        if (seq !== state.pageSeq || active !== 'runs') { return; }
        clear(host);
        host.appendChild(panel(null, errorBlock(e, function () { show('runs'); })));
      });
    }

    function loadSubs(silent) {
      return get('/api/submissions', { limit: 50, platform: state.platform }).then(function (body) {
        if (seq !== state.pageSeq || active !== 'subs') { return; }
        var subs = (body && body.submissions) || [];
        clear(host);
        if (!subs.length) {
          host.appendChild(panel(null, stateBlock('', '还没有提交记录',
            '只有真提交才会写进这里；干跑不落记录。')));
          return;
        }
        var list = el('div', 'list');
        subs.forEach(function (sub) {
          var row = el('div', 'rowx');
          row.appendChild(el('span', 'mark ' + (sub.ok ? 'ok' : 'err')));
          var m = el('div', 'rowx-main');
          var title = el('div', 'rowx-title');
          title.appendChild(el('span', 'mono', sub.assignment_id || '—'));
          if (!state.platform) { title.appendChild(pill(sub.platform, 'pill-mute')); }
          m.appendChild(title);
          var meta = el('div', 'rowx-meta');
          meta.appendChild(el('span', '', '得分 ' + (sub.score === null || sub.score === undefined ? '—' : num(sub.score))));
          meta.appendChild(el('span', '', sub.message || ''));
          meta.appendChild(el('span', '', time(sub.created_at)));
          m.appendChild(meta);
          row.appendChild(m);
          var side = el('div', 'rowx-side');
          side.appendChild(sub.ok ? pill('成功', 'pill-ok') : pill('失败', 'pill-danger'));
          row.appendChild(side);
          list.appendChild(row);
        });
        host.appendChild(panel('提交记录', list, {
          flush: true,
          aside: pill(subs.length + ' 条', 'pill-mute')
        }));
      }).catch(function (e) {
        if (seq !== state.pageSeq || active !== 'subs') { return; }
        clear(host);
        host.appendChild(panel(null, errorBlock(e, function () { show('subs'); })));
      });
    }

    function loadJobs(silent) {
      return get('/api/jobs', state.platform ? { platform: state.platform } : null).then(function (body) {
        if (seq !== state.pageSeq || active !== 'jobs') { return; }
        var jobs = (body && body.jobs) || [];
        state.jobs = jobs;
        renderRail();
        clear(host);
        if (!jobs.length) {
          host.appendChild(panel(null, stateBlock('', '还没有后台任务')));
          return;
        }
        var list = el('div', 'list');
        jobs.forEach(function (job) { list.appendChild(jobRow(job)); });
        host.appendChild(panel('任务历史', list, {
          flush: true,
          aside: pill(jobs.length + ' 条', 'pill-mute')
        }));
      }).catch(function (e) {
        if (seq !== state.pageSeq || active !== 'jobs') { return; }
        clear(host);
        host.appendChild(panel(null, errorBlock(e, function () { show('jobs'); })));
      });
    }

    function loadEvents(silent) {
      return get('/api/events', { limit: 100 }).then(function (body) {
        if (seq !== state.pageSeq || active !== 'events') { return; }
        var events = (body && body.events) || [];
        clear(host);
        if (!events.length) {
          host.appendChild(panel(null, stateBlock('', '还没有事件',
            '事件流记录任务运行中的每一步，运行时会实时追加。')));
          return;
        }
        var list = el('div', 'ev-list');
        events.slice().reverse().forEach(function (ev) {
          var row = el('div', 'ev');
          row.appendChild(el('span', 't', time(ev.at)));
          row.appendChild(el('span', 'n', ev.name || '—'));
          row.appendChild(el('span', 'p', jsonText(ev.payload, 160)));
          list.appendChild(row);
        });
        host.appendChild(panel('事件流', list, {
          aside: pill('最近 ' + events.length + ' 条', 'pill-mute')
        }));
      }).catch(function (e) {
        if (seq !== state.pageSeq || active !== 'events') { return; }
        clear(host);
        host.appendChild(panel(null, errorBlock(e, function () { show('events'); })));
      });
    }

    function loadPlatformLogs(silent) {
      if (!state.platform) {
        clear(host);
        host.appendChild(gateBlock('平台日志按平台各存一份：先在顶栏选一个平台。'));
        return Promise.resolve();
      }
      return get('/api/platforms/' + encodeURIComponent(state.platform) + '/logs', { limit: 200 })
        .then(function (body) {
          if (seq !== state.pageSeq || active !== 'plog') { return; }
          var logs = (body && body.logs) || [];
          clear(host);
          var view = el('pre', 'log-view');
          if (!logs.length) {
            view.textContent = '这个平台还没有执行记录。';
          } else {
            logs.forEach(function (line) {
              var row = el('div');
              row.appendChild(el('span', 't', time(line.at).slice(11) + '  '));
              row.appendChild(el('span', line.level || 'info', line.text || ''));
              view.appendChild(row);
            });
          }
          host.appendChild(panel('平台执行日志', view, {
            aside: pill(logs.length + ' / ' + ((body && body.total) || 0) + ' 行', 'pill-mute')
          }));
        }).catch(function (e) {
          if (seq !== state.pageSeq || active !== 'plog') { return; }
          clear(host);
          host.appendChild(panel(null, errorBlock(e, function () { show('plog'); })));
        });
    }

    show('runs');
    every(2500, function () {
      if (seq !== state.pageSeq) { return; }
      if (active === 'jobs' || active === 'events' || active === 'plog') { show(active, true); }
    });

    page.appendChild(hintBlock([
      '跑批记录 = 一个作业的处理结果；任务 = 一次跑批的整体。两套状态枚举不互相映射。',
      '提交记录只有真提交才有，干跑不落记录。',
      '事件流与平台日志在每个进程内是环形的，重启后清空。'
    ]));
  }

  /* ================================================================ 统计 */

  function pageStats(page, seq) {
    page.appendChild(pageHead({
      title: '统计',
      sub: ['跨平台对比 · 不受顶栏平台筛选影响'],
      actions: [btn('刷新', 'btn-sm', function () { renderPage(); })]
    }));
    var host = el('div', '');
    page.appendChild(host);
    host.appendChild(panel(null, loadingBlock()));

    get('/api/statistics').then(function (st) {
      if (seq !== state.pageSeq) { return; }
      clear(host);
      var bank = st.bank || {};
      var rows = st.platforms || [];

      host.appendChild(kpis([
        { label: '题库总量', value: num(bank.total) },
        { label: '已判分', value: num(bank.graded) },
        { label: '判分准确率', value: bank.accuracy === null || bank.accuracy === undefined ? '—' : pct(bank.accuracy, 1) },
        { label: '平台数', value: rows.length, unit: ' 个' }
      ]));

      if (!rows.length) {
        host.appendChild(panel(null, stateBlock('', '还没有可统计的平台')));
        return;
      }

      var table = el('table', 'tbl');
      var head = el('thead');
      var htr = el('tr');
      ['平台', '跑批', '成功', '失败', '跳过', '成功率', '提交', '题数', '准确率']
        .forEach(function (label, i) {
          htr.appendChild(el('th', i ? 'num' : '', label));
        });
      head.appendChild(htr);
      table.appendChild(head);

      var body = el('tbody');
      rows.forEach(function (r) {
        var tr = el('tr');
        var nameTd = el('td');
        nameTd.appendChild(el('span', 'mono', r.platform));
        nameTd.appendChild(document.createTextNode(' '));
        nameTd.appendChild(el('span', 'dim', platformLabel(r.platform) === r.platform ? '' : platformLabel(r.platform)));
        tr.appendChild(nameTd);
        tr.appendChild(el('td', 'num', num(r.total)));
        tr.appendChild(el('td', 'num', num(r.succeeded)));
        tr.appendChild(el('td', 'num', num(r.failed)));
        tr.appendChild(el('td', 'num', num(r.skipped)));
        var rateTd = el('td', 'num');
        var rate = r.total ? (r.succeeded / r.total) : 0;
        var rateKind = !r.total ? 'pill pill-mute'
          : (rate >= 0.8 ? 'pill pill-ok' : (rate >= 0.5 ? 'pill pill-warn' : 'pill pill-danger'));
        rateTd.appendChild(el('span', rateKind, r.total ? pct(rate, 0) : '—'));
        tr.appendChild(rateTd);
        tr.appendChild(el('td', 'num', num(r.submissions)));
        tr.appendChild(el('td', 'num', num(r.questions)));
        tr.appendChild(el('td', 'num', r.accuracy === null || r.accuracy === undefined ? '—' : pct(r.accuracy, 1)));
        body.appendChild(tr);
      });
      table.appendChild(body);
      var wrap = el('div', 'tbl-wrap');
      wrap.appendChild(table);
      host.appendChild(panel('各平台成败对比', wrap, { flush: true }));

      var plats = Object.keys(bank.by_platform || {}).map(function (k) {
        return { label: platformLabel(k), value: bank.by_platform[k] };
      }).sort(function (a, b) { return b.value - a.value; });
      host.appendChild(panel('题库分布（全部平台）',
        plats.length ? bars(plats, { percent: true })
          : stateBlock('', '题库还是空的'),
        { aside: pill('全量', 'pill-mute') }));

      host.appendChild(hintBlock([
        '这是唯一的横向视图：不传 platform，就是为了能对比各平台。',
        '成功率 = 成功 / 该平台跑批总数；没有跑批的平台显示「—」而不是 0%。',
        '准确率来自题库里被平台判过分的题。'
      ]));
    }).catch(function (e) {
      if (seq !== state.pageSeq) { return; }
      clear(host);
      host.appendChild(panel(null, errorBlock(e, function () { renderPage(); })));
    });
  }
  /* ================================================================ 账号 */

  function pageAccounts(page, seq) {
    page.appendChild(pageHead({
      title: '账号',
      sub: [scopeChip(), '每个平台可以有多个号，随时切当前号'],
      actions: [
        btn('新增账号', 'btn-sm btn-primary', function () { openAddAccount(); }),
        btn('刷新', 'btn-sm', function () { renderPage(); })
      ]
    }));

    var host = el('div', '');
    page.appendChild(host);
    host.appendChild(panel(null, loadingBlock()));

    Promise.all([
      get('/api/accounts', state.platform ? { platform: state.platform } : null),
      get('/api/settings')
    ]).then(function (res) {
      if (seq !== state.pageSeq) { return; }
      var accounts = (res[0] && res[0].accounts) || [];
      var settings = res[1] || {};
      state.settings = settings;
      clear(host);

      var split = el('div', 'split');
      var left = el('div', 'main-col');
      var aside = el('div', 'aside');

      if (!accounts.length) {
        left.appendChild(panel('平台账号', stateBlock('', '还没有任何平台账号',
          '添加账号后，课程 / 作业 / 工具这些动作才有会话可用。',
          [btn('新增账号', 'btn-sm btn-primary', function () { openAddAccount(); })])));
      } else {
        var groups = {};
        accounts.forEach(function (a) {
          (groups[a.platform] = groups[a.platform] || []).push(a);
        });
        Object.keys(groups).forEach(function (name) {
          var list = el('div', 'list');
          groups[name].forEach(function (a) { list.appendChild(accountRow(a)); });
          left.appendChild(panel(platformLabel(name), list, {
            flush: true,
            aside: pill(name, 'pill-mute')
          }));
        });
      }

      /* --- 大模型 --- */
      var llm = settings.llm || {};
      var llmBody = el('div', '');
      var status = el('div', 'pills');
      status.appendChild(dotPill(llm.enabled && llm.configured ? '已启用' : '未配置',
        llm.enabled && llm.configured ? 'pill-ok' : 'pill-mute'));
      status.appendChild(pill('key ' + (llm.api_key_set ? '已设置' : '未设置'), 'pill-mute'));
      llmBody.appendChild(status);
      llmBody.appendChild(defs([
        ['模型', llm.model || '—'],
        ['地址', llm.api_base || '—'],
        ['来源', llm.configured_via || '—'],
        ['覆盖字段', (llm.override_fields || []).join('、') || '—']
      ]));

      if (llm.editable && state.isSuper) {
        var form = el('div', 'form-grid');
        var baseUrl = input('text', llm.api_base || '', 'https://api.example.com/v1');
        var model = input('text', llm.model || '', 'deepseek-chat');
        var key = input('password', '', '留空 = 不修改');
        var timeout = input('number', llm.timeout || 90, '');
        var temperature = input('number', llm.temperature === undefined ? 0 : llm.temperature, '');
        form.appendChild(fieldWrap('接口地址', baseUrl, 'OpenAI 兼容，通常以 /v1 结尾'));
        form.appendChild(fieldWrap('模型', model, '网关允许的模型名'));
        form.appendChild(fieldWrap('API Key', key, '只写不读，保存后界面拿不到明文'));
        form.appendChild(fieldWrap('超时（秒）', timeout, '1 ~ 600'));
        form.appendChild(fieldWrap('温度', temperature, '0 ~ 2'));

        var msg = el('span', 'hint-text', '');
        var save = btn('保存', 'btn-primary btn-sm', function () {
          save.disabled = true;
          post('/api/settings/llm', {
            base_url: baseUrl.value,
            model: model.value,
            timeout: Number(timeout.value) || 90,
            temperature: Number(temperature.value) || 0,
            api_key: key.value
          }).then(function () {
            toast('已保存，立即生效');
            msg.textContent = '已保存';
            renderPage();
          }).catch(function (e) {
            msg.textContent = describe(e);
          }).then(function () { save.disabled = false; });
        });
        var test = btn('测试连接', 'btn-sm', function () {
          msg.textContent = '正在测试…';
          post('/api/settings/llm/test', {}).then(function (r) {
            msg.textContent = '连通：' + jsonText(r, 120);
          }).catch(function (e) {
            msg.textContent = describe(e);
          });
        });
        var clearKey = btn('清除 Key', 'btn-sm btn-danger', function () {
          post('/api/settings/llm', { clear_api_key: true }).then(function () {
            toast('已清除 Key');
            renderPage();
          }).catch(function (e) { msg.textContent = describe(e); });
        });
        llmBody.appendChild(form);
        llmBody.appendChild(actionRow([save, test, clearKey, msg]));
      }

      left.appendChild(panel('大模型', llmBody, {
        aside: pill(state.isSuper ? '超管可改' : '只读', 'pill-mute')
      }));

      /* --- 配置现状 --- */
      aside.appendChild(panel('配置现状', defs([
        ['环境', settings.env],
        ['干跑', settings.dry_run ? '是' : '否'],
        ['无头浏览器', settings.headless ? '是' : '否'],
        ['并发上限', settings.max_concurrency],
        ['最小间隔', settings.min_interval],
        ['最大尝试', settings.max_attempts],
        ['最低置信度', settings.min_confidence],
        ['通知', settings.notify],
        ['数据库', settings.db_path],
        ['会话目录', settings.session_dir],
        ['.env', settings.dotenv_present ? '已读到' : '没有找到']
      ])));

      /* --- 凭据状态 --- */
      var plats = settings.platforms || [];
      var table = el('table', 'tbl');
      var thead = el('thead');
      var htr = el('tr');
      ['平台', '显示名', '凭据', '期望环境变量', '能力'].forEach(function (t) {
        htr.appendChild(el('th', '', t));
      });
      thead.appendChild(htr);
      table.appendChild(thead);
      var tbody = el('tbody');
      plats.forEach(function (p) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', p.name));
        tr.appendChild(el('td', '', p.display_name || '—'));
        var cred = el('td');
        cred.appendChild(p.has_credentials ? pill('已配置', 'pill-ok') : pill('缺失', 'pill-warn'));
        tr.appendChild(cred);
        tr.appendChild(el('td', 'mono wrap', p.credentials_hint || '—'));
        var caps = el('td');
        caps.appendChild(capChips(p.capabilities || {}));
        tr.appendChild(caps);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      var wrap = el('div', 'tbl-wrap');
      wrap.appendChild(table);
      left.appendChild(panel('平台凭据状态', wrap, { flush: true }));

      /* --- 环境变量键名 --- */
      var keys = (settings.env_keys_seen || []);
      var keyBox = el('div', 'pills');
      if (!keys.length) {
        keyBox.appendChild(pill('没有读到任何键名', 'pill-warn'));
      } else {
        keys.forEach(function (k) { keyBox.appendChild(pill(k, 'pill-mute')); });
      }
      aside.appendChild(panel('环境变量键名', keyBox));

      var deps = settings.optional_deps || {};
      var depBox = el('div', 'pills');
      Object.keys(deps).forEach(function (k) {
        depBox.appendChild(dotPill(k, deps[k] ? 'pill-ok' : 'pill-mute'));
      });
      aside.appendChild(panel('可选依赖', depBox));

      split.appendChild(left);
      split.appendChild(aside);
      host.appendChild(split);

      host.appendChild(hintBlock([
        '平台密码不落盘：只在登录那一刻用来换 cookie，换完就丢；会话过期要重新输一次。',
        '每个账号的 cookie 存在自己的文件里，切换当前号 = 切换 cookie，互不影响。',
        '界面任何地方都不展示密码或令牌，后端也不会返回它们。',
        '大模型配置的 Key 只写不读；保存后立即生效，不用重启。'
      ]));
    }).catch(function (e) {
      if (seq !== state.pageSeq) { return; }
      clear(host);
      host.appendChild(panel(null, errorBlock(e, function () { renderPage(); })));
    });
  }

  function input(type, value, placeholder) {
    var node = el('input');
    node.type = type;
    if (value !== undefined && value !== null && value !== '') { node.value = String(value); }
    if (placeholder) { node.placeholder = placeholder; }
    if (type === 'text' || type === 'password') { node.className = 'mono'; }
    return node;
  }

  function fieldWrap(label, node, hint) {
    var f = el('div', 'field');
    f.appendChild(el('span', 'field-label', label));
    f.appendChild(node);
    if (hint) { f.appendChild(el('span', 'hint-text', hint)); }
    return f;
  }

  function accountRow(a) {
    var row = el('div', 'rowx');
    row.appendChild(el('span', 'mark ' + (a.last_login_ok ? 'ok' : 'warn')));

    var main = el('div', 'rowx-main');
    var title = el('div', 'rowx-title');
    title.appendChild(el('span', '', a.display || a.label || a.username || ('#' + a.id)));
    if (a.is_active) { title.appendChild(pill('当前号', 'pill-accent')); }
    if (a.last_login_ok === false) { title.appendChild(pill('会话失效', 'pill-danger')); }
    main.appendChild(title);
    var meta = el('div', 'rowx-meta');
    meta.appendChild(el('span', 'mono', a.username || '—'));
    meta.appendChild(el('span', '', '上次登录 ' + time(a.last_login_at)));
    if (a.last_login_note) { meta.appendChild(el('span', '', truncate(a.last_login_note, 60))); }
    main.appendChild(meta);
    row.appendChild(main);

    var side = el('div', 'rowx-side');
    if (!a.is_active) {
      side.appendChild(btn('设为当前', 'btn-sm', function () {
        post('/api/accounts/' + a.id + '/activate', {}).then(function () {
          toast('已切到 ' + (a.display || a.username));
          renderPage();
        }).catch(function (e) { toast(describe(e)); });
      }));
    }
    side.appendChild(btn('重新登录', 'btn-sm btn-ghost', function () {
      openRelogin(a);
    }));
    var armed = { on: false };
    var del = btn('删除', 'btn-sm btn-ghost', function () {
      if (!armed.on) {
        armed.on = true;
        del.textContent = '确认删除';
        del.classList.add('btn-danger');
        return;
      }
      post('/api/accounts/' + a.id + '/delete', {}).then(function () {
        toast('已删除账号');
        refreshAfterAccountChange();
      }).catch(function (e) { toast(describe(e)); });
    });
    side.appendChild(del);
    row.appendChild(side);
    return row;
  }

  function openRelogin(a) {
    var pass = input('password', '', '平台密码（不落盘）');
    var msg = el('span', 'hint-text', '');
    var submit = btn('登录', 'btn-primary', function () {
      submit.disabled = true;
      post('/api/accounts/' + a.id + '/login', { password: pass.value }).then(function (r) {
        toast('登录成功');
        closeInspector();
        renderPage();
      }).catch(function (e) {
        msg.textContent = describe(e);
      }).then(function () { submit.disabled = false; });
    });
    var body = el('div', '');
    body.appendChild(el('p', 'env-note',
      '平台密码只在登录那一刻使用，换完 cookie 就丢；这里不会保存它。'));
    body.appendChild(fieldWrap('平台密码', pass, '账号 ' + (a.display || a.username)));
    body.appendChild(actionRow([submit, msg]));
    openInspector({
      title: '重新登录',
      sub: platformLabel(a.platform) + ' · ' + (a.display || a.username),
      body: body
    });
  }

  function openAddAccount() {
    var platSel = el('select');
    state.platforms.forEach(function (p) {
      var o = el('option', '', (p.display_name || p.name) + '（' + p.name + '）');
      o.value = p.name;
      platSel.appendChild(o);
    });
    if (state.platform) { platSel.value = state.platform; }
    var label = input('text', '', '例如：我的号');
    var username = input('text', '', '平台用户名');
    var password = input('password', '', '平台密码');
    var loginNow = el('input');
    loginNow.type = 'checkbox';
    loginNow.checked = true;
    var sw = el('label', 'switch');
    sw.appendChild(loginNow);
    sw.appendChild(el('span', '', '创建后立即登录'));
    var msg = el('span', 'hint-text', '');

    var form = el('div', 'form-grid');
    form.appendChild(fieldWrap('平台', platSel, '必填'));
    form.appendChild(fieldWrap('标签', label, '给人看的名字，可留空'));
    form.appendChild(fieldWrap('平台用户名', username, ''));
    form.appendChild(fieldWrap('平台密码', password, '不落盘、不回显'));
    var f = el('div', 'field field-wide');
    f.appendChild(sw);
    form.appendChild(f);

    var submit = btn('创建账号', 'btn-primary', function () {
      if (!username.value.trim() || !password.value) {
        msg.textContent = '用户名和密码都要填。';
        return;
      }
      submit.disabled = true;
      post('/api/accounts', {
        platform: platSel.value,
        label: label.value,
        username: username.value,
        password: password.value,
        login: loginNow.checked
      }).then(function () {
        toast('账号已创建');
        closeInspector();
        refreshAfterAccountChange();
      }).catch(function (e) {
        msg.textContent = describe(e);
      }).then(function () { submit.disabled = false; });
    });

    var body = el('div', '');
    body.appendChild(form);
    body.appendChild(actionRow([submit, msg]));
    openInspector({ title: '新增账号', sub: '平台 + 用户名 + 密码', body: body });
  }

  /* ================================================================ 工具 */

  /* 这些动作已经在专属页面提供了更完整的入口，工具页不再重复列。
     值是「去哪儿找」，用来在提示里告诉用户。 */
  var ACTION_SURFACED_IN = {
    study: '作业页 · 刷课',
    duration: '作业页 · 刷课（刷时长）',
    attendance: '考勤页',
    sign: '考勤页',
    signin: '考勤页'
  };

  /* 过滤后剩下的空提示（不同平台不同，所以按有没有剩余动作决定文案） */
  function toolsEmptyText(hidden) {
    if (!hidden) {
      return '这个平台没有声明只读动作。课程 / 作业这类通用动作在左侧对应页面。';
    }
    return '这个平台的动作都已经有专属入口（' + hidden + '），工具页没有额外可跑的。';
  }

  function pageTools(page, seq) {
    if (needsPlatformPage(page, seq)) { return; }
    var platName = state.platform;

    page.appendChild(pageHead({
      title: '工具',
      badge: pill('通用渲染', 'pill-mute'),
      sub: [scopeChip(), '动作由适配器声明，界面不认识任何平台细节'],
      actions: [btn('平台日志', 'btn-sm btn-ghost', function () { openPlatformLog(platName); })]
    }));

    var host = el('div', '');
    page.appendChild(host);
    host.appendChild(panel(null, loadingBlock()));

    var actions = [];
    var selected = null;

    /* 「显示全部」开关：默认只列没有专属入口的动作。
       存在 state 里 —— 页面会整块重绘，局部变量活不过一次 renderPage()。 */
    var showAll = {
      get on() { return state.toolsShowAll === true; },
      set on(v) { state.toolsShowAll = v === true; }
    };

    get('/api/platforms/' + encodeURIComponent(platName) + '/actions').then(function (body) {
      if (seq !== state.pageSeq) { return; }
      // 通用动作（courses / assignments / detail）已有专属页面，工具页不再重复列
      var all = ((body && body.actions) || []).filter(function (a) {
        return a.builtin !== true;
      });
      var hidden = all.filter(function (a) { return ACTION_SURFACED_IN[a.name]; });
      actions = showAll.on
        ? all
        : all.filter(function (a) { return !ACTION_SURFACED_IN[a.name]; });
      clear(host);

      /* 提示条：说明隐藏了什么、去哪儿找、以及切回全部 */
      if (hidden.length) {
        var note = el('div', 'banner');
        var label = el('span', 'banner-title', '已收起 ' + hidden.length + ' 个动作');
        note.appendChild(label);
        var list = el('ul');
        hidden.forEach(function (a) {
          list.appendChild(el('li', '',
            (a.label || a.name) + ' —— 去「' + ACTION_SURFACED_IN[a.name] + '」'));
        });
        note.appendChild(list);
        if (!showAll.on) {
          note.appendChild(btn('照样显示在工具页', 'btn-sm btn-ghost', function () {
            showAll.on = true;
            renderPage();
          }));
        }
        host.appendChild(note);
      }

      if (!actions.length) {
        host.appendChild(panel(null, stateBlock('', '没有额外可跑的动作',
          toolsEmptyText(hidden.length),
          showAll.on ? [] : (hidden.length ? [btn('显示全部动作', 'btn-sm', function () {
            showAll.on = true;
            renderPage();
          })] : []))));
        return;
      }

      var chips = el('div', 'tabs');
      chips.setAttribute('role', 'tablist');
      var runHost = el('div', '');
      var formHost = el('div', '');

      actions.forEach(function (a) {
        var b = el('button', 'tab', a.label || a.name);
        b.type = 'button';
        b.setAttribute('role', 'tab');
        b.addEventListener('click', function () {
          select(a);
          Array.prototype.forEach.call(chips.children, function (c) {
            c.setAttribute('aria-selected', String(c === b));
          });
        });
        chips.appendChild(b);
        b.__action = a;
      });

      host.appendChild(panel('平台动作', chips));
      host.appendChild(formHost);
      host.appendChild(runHost);

      function select(a) {
        selected = a;
        clear(formHost);
        clear(runHost);

        var head = el('div', 'panel-head');
        head.appendChild(el('h2', '', a.label || a.name));
        head.appendChild(pill(a.name, 'pill-mute'));
        head.appendChild(a.mutates ? pill('写操作', 'pill-danger') : pill('只读', 'pill-ro'));
        if (a.description) { head.appendChild(el('span', 'hint-text', a.description)); }

        var form = el('div', 'form-grid');
        var inputs = {};
        (a.params || []).forEach(function (p) {
          var node;
          if (p.type === 'text') {
            node = el('textarea');
            node.className = 'mono';
          } else {
            node = el('input');
            node.type = p.type === 'int' ? 'number' : 'text';
            node.className = 'mono';
          }
          if (p.required) { node.required = true; }
          var f = fieldWrap((p.label || p.name) + (p.required ? ' *' : ''),
            node, p.type === 'int' ? '整数' : (p.type === 'text' ? '长文本' : ''));
          if (p.type === 'text') { f.classList.add('field-wide'); }
          form.appendChild(f);
          inputs[p.name] = node;
        });

        var msg = el('span', 'hint-text', '');
        var run = btn('执行', 'btn-primary', function () {
          var payload = {};
          var missing = [];
          (a.params || []).forEach(function (p) {
            var v = inputs[p.name] ? String(inputs[p.name].value || '').trim() : '';
            if (p.required && !v) { missing.push(p.label || p.name); return; }
            if (!v) { return; }
            payload[p.name] = p.type === 'int' ? Number(v) : v;
          });
          if (missing.length) {
            msg.textContent = '还差必填参数：' + missing.join('、');
            return;
          }
          run.disabled = true;
          msg.textContent = '执行中…';
          post('/api/platforms/' + encodeURIComponent(platName) + '/actions/' +
            encodeURIComponent(a.name), payload).then(function (env) {
            msg.textContent = '';
            clear(runHost);
            runHost.appendChild(panel(env.title || (a.label || a.name),
              renderEnvelope(env, {
                hideHead: true,
                onRow: function (row) { inspectRow(platName, row, a.label || a.name); },
                empty: '动作成功，但没有返回数据行。'
              })));
          }).catch(function (e) {
            msg.textContent = describe(e);
            clear(runHost);
            runHost.appendChild(panel(null, errorBlock(e)));
          }).then(function () { run.disabled = false; });
        });

        var panelNode = el('section', 'panel');
        panelNode.appendChild(head);
        var bodyNode = el('div', 'panel-body');
        if (!(a.params || []).length) {
          bodyNode.appendChild(el('p', 'env-note', '这个动作没有参数，直接执行即可。'));
        }
        bodyNode.appendChild(form);
        bodyNode.appendChild(actionRow([run, msg]));
        panelNode.appendChild(bodyNode);
        formHost.appendChild(panelNode);
      }

      select(actions[0]);
      if (chips.firstChild) { chips.firstChild.setAttribute('aria-selected', 'true'); }
    }).catch(function (e) {
      if (seq !== state.pageSeq) { return; }
      clear(host);
      host.appendChild(panel(null, errorBlock(e, function () { renderPage(); })));
    });

    page.appendChild(hintBlock([
      '工具页是**兜底入口**：只列那些还没有专属页面的平台动作。',
      '已经在专属页面有入口的动作（刷课、考勤 / 签到、刷时长）默认收起，避免同一个操作有两个按钮。',
      '动作清单来自适配器声明，前端按 kind（table / detail / message）通用渲染，不为平台写专属页面。',
      '标「写操作」的动作会改平台侧状态（开考计时、填答案、交卷），不可逆。',
      '结果里的正文与批注属于个人内容：只在这里展示，不缓存、不外发、不导出。'
    ]));
  }

  /* ================================================================ 用户 */

  function pageUsers(page, seq) {
    if (!state.isSuper) {
      page.appendChild(pageHead({ title: '用户', sub: ['仅超管'] }));
      page.appendChild(stateBlock('gate', '这个页面只对超管开放',
        '非超管请求 /api/users 会被后端 403 拒绝；入口隐藏只是少一个点击，不是权限。'));
      return;
    }

    page.appendChild(pageHead({
      title: '用户',
      sub: ['超管视图 · 所有人 + 每人账号数'],
      actions: [btn('刷新', 'btn-sm', function () { renderPage(); })]
    }));

    var host = el('div', '');
    page.appendChild(host);

    get('/api/users').then(function (body) {
      if (seq !== state.pageSeq) { return; }
      var users = (body && body.users) || [];
      state.users = users;
      clear(host);

      /* 新建用户 */
      var username = input('text', '', '用户名');
      var password = input('password', '', '至少 8 位');
      var role = el('select');
      [['user', '普通用户'], ['superadmin', '超级管理员']].forEach(function (r) {
        var o = el('option', '', r[1]);
        o.value = r[0];
        role.appendChild(o);
      });
      var note = input('text', '', '备注（可选）');
      var msg = el('span', 'hint-text', '');
      var submit = btn('创建用户', 'btn-primary', function () {
        if (String(password.value || '').length < 8) {
          msg.textContent = '密码至少 8 位。';
          return;
        }
        submit.disabled = true;
        post('/api/users', {
          username: username.value, password: password.value,
          role: role.value, note: note.value
        }).then(function () {
          toast('用户已创建');
          renderPage();
        }).catch(function (e) {
          msg.textContent = describe(e);
        }).then(function () { submit.disabled = false; });
      });
      var form = el('div', 'form-grid');
      form.appendChild(fieldWrap('用户名', username, ''));
      form.appendChild(fieldWrap('密码', password, '至少 8 位'));
      form.appendChild(fieldWrap('角色', role, ''));
      form.appendChild(fieldWrap('备注', note, ''));
      form.appendChild(fieldWrap('操作', actionRow([submit, msg]), ''));
      host.appendChild(panel('新建用户', form));

      /* 用户列表 */
      var list = el('div', 'list');
      users.forEach(function (u) {
        var row = el('div', 'rowx');
        row.appendChild(el('span', 'mark ' + (u.disabled ? 'err' : 'ok')));
        var m = el('div', 'rowx-main');
        var title = el('div', 'rowx-title');
        title.appendChild(el('span', 'mono', u.username));
        title.appendChild(pill(u.role === 'superadmin' ? '超管' : '普通用户',
          u.role === 'superadmin' ? 'pill-accent' : 'pill-mute'));
        if (u.disabled) { title.appendChild(pill('已停用', 'pill-danger')); }
        m.appendChild(title);
        var meta = el('div', 'rowx-meta');
        meta.appendChild(el('span', '', '#' + u.id));
        meta.appendChild(el('span', '', '创建 ' + time(u.created_at)));
        meta.appendChild(el('span', '', (u.account_count || 0) + ' 个账号'));
        if (u.note) { meta.appendChild(el('span', '', u.note)); }
        m.appendChild(meta);
        row.appendChild(m);

        var side = el('div', 'rowx-side');
        var roleSel = el('select', 'role-sel');
        [['user', '普通用户'], ['superadmin', '超管']].forEach(function (r) {
          var o = el('option', '', r[1]);
          o.value = r[0];
          roleSel.appendChild(o);
        });
        roleSel.value = u.role;
        roleSel.addEventListener('change', function () {
          post('/api/users/' + u.id + '/update', { role: roleSel.value }).then(function () {
            toast('角色已更新');
            renderPage();
          }).catch(function (e) { toast(describe(e)); });
        });
        side.appendChild(roleSel);
        side.appendChild(btn(u.disabled ? '启用' : '停用', 'btn-sm', function () {
          post('/api/users/' + u.id + '/update', { disabled: !u.disabled }).then(function () {
            toast(u.disabled ? '已启用' : '已停用');
            renderPage();
          }).catch(function (e) { toast(describe(e)); });
        }));
        var armed = { on: false };
        var del = btn('删除', 'btn-sm btn-ghost', function () {
          if (!armed.on) {
            armed.on = true;
            del.textContent = '确认删除';
            del.classList.add('btn-danger');
            return;
          }
          post('/api/users/' + u.id + '/delete', {}).then(function () {
            toast('已删除');
            renderPage();
          }).catch(function (e) { toast(describe(e)); });
        });
        side.appendChild(del);
        row.appendChild(side);
        list.appendChild(row);
      });
      host.appendChild(panel('用户列表', list, {
        flush: true,
        aside: pill(users.length + ' 个', 'pill-mute')
      }));

      host.appendChild(hintBlock([
        '数据隔离在存储层强制：普通用户带 owner_id 指向别人会被 403。',
        '不允许删掉最后一个超管，后端会拦下并返回中文原因。',
        '响应里永远不会返回密码或哈希。'
      ]));
    }).catch(function (e) {
      if (seq !== state.pageSeq) { return; }
      clear(host);
      host.appendChild(panel(null, errorBlock(e, function () { renderPage(); })));
    });
  }

  /* ================================================================ 调度 */

  function renderPage() {
    var main = $('main');
    if (!main) { return; }
    var seq = ++state.pageSeq;
    var page = el('div', 'page');
    clear(main);
    main.appendChild(page);
    main.scrollTop = 0;

    /* 依赖平台动作清单的页面（如考勤）：清单没到手就先显示骨架。
       否则会先按「默认模式」渲染一次、打出必然 404 的动作请求。 */
    var pageDef = PAGES[state.page];
    var needsActions = pageDef && (pageDef.requiresAction || pageDef.requiresAnyAction);
    if (needsActions && state.platform && !state.platformActions[state.platform]) {
      page.appendChild(pageHead({
        title: pageDef.label,
        sub: [scopeChip(), '正在确认这个平台的能力']
      }));
      page.appendChild(panel(null, loadingBlock()));
      loadPlatformActions(state.platform).then(function () {
        if (seq === state.pageSeq) { renderPage(); }
      });
      return;
    }

    var fn = {
      overview: pageOverview,
      tasks: pageTasks,
      courses: pageCourses,
      assignments: pageAssignments,
      attendance: pageAttendance,
      bank: pageBank,
      logs: pageLogs,
      stats: pageStats,
      accounts: pageAccounts,
      tools: pageTools,
      roadmap: pageRoadmap,
      users: pageUsers
    }[state.page] || pageOverview;

    try {
      fn(page, seq);
    } catch (e) {
      page.appendChild(panel(null, errorBlock(e)));
    }
    document.title = '课代表 · ' + PAGES[state.page].label;
  }

  /* ================================================================ 启动 */

  function bind() {
    var form = $('login-form');
    if (form) { form.addEventListener('submit', doLogin); }
    var logout = $('logout-btn');
    if (logout) { logout.addEventListener('click', doLogout); }
    var theme = $('theme-btn');
    if (theme) { theme.addEventListener('click', toggleTheme); }

    var moreBtn = $('more-btn');
    if (moreBtn) {
      moreBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if ($('more-menu').hidden) { openMoreMenu(); } else { closeMoreMenu(); }
      });
    }

    var acctInfo = $('acct-info');
    if (acctInfo) {
      acctInfo.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if ($('acct-menu').hidden) { openAcctMenu(); } else { closeAcctMenu(); }
      });
    }
    var acctSwitch = $('acct-switch');
    if (acctSwitch) {
      acctSwitch.addEventListener('change', function () { switchAccount(acctSwitch.value); });
    }
    document.addEventListener('click', function (ev) {
      var menu = $('acct-menu');
      if (menu && !menu.hidden) {
        if (!(menu.contains(ev.target) || (acctInfo && acctInfo.contains(ev.target)))) {
          closeAcctMenu();
        }
      }
      var more = $('more-menu');
      if (more && !more.hidden) {
        var mb = $('more-btn');
        if (!(more.contains(ev.target) || (mb && mb.contains(ev.target)))) {
          closeMoreMenu();
        }
      }
    });

    var close = $('inspector-close');
    if (close) { close.addEventListener('click', closeInspector); }
    var inspector = $('inspector');
    if (inspector) {
      inspector.addEventListener('click', function (ev) {
        if (ev.target && ev.target.getAttribute && ev.target.getAttribute('data-close')) {
          closeInspector();
        }
      });
    }

    var palette = $('palette');
    if (palette) {
      palette.addEventListener('click', function (ev) {
        if (ev.target === palette) { closePalette(); }
      });
    }
    var pin = $('palette-input');
    if (pin) {
      pin.addEventListener('input', function () { renderPalette(pin.value); });
      pin.addEventListener('keydown', function (ev) {
        var items = paletteState.filtered || [];
        if (ev.key === 'ArrowDown') {
          ev.preventDefault();
          paletteState.active = Math.min(items.length - 1, paletteState.active + 1);
          renderPalette(pin.value);
        } else if (ev.key === 'ArrowUp') {
          ev.preventDefault();
          paletteState.active = Math.max(0, paletteState.active - 1);
          renderPalette(pin.value);
        } else if (ev.key === 'Enter') {
          ev.preventDefault();
          var it = items[paletteState.active];
          if (it) { closePalette(); it.run(); }
        } else if (ev.key === 'Escape') {
          closePalette();
        }
      });
    }

    document.addEventListener('keydown', function (ev) {
      if ((ev.metaKey || ev.ctrlKey) && (ev.key === 'k' || ev.key === 'K')) {
        ev.preventDefault();
        if ($('palette').hidden) { openPalette(); } else { closePalette(); }
        return;
      }
      if (ev.key === 'Escape') {
        if (!$('palette').hidden) { closePalette(); return; }
        if ($('acct-menu') && !$('acct-menu').hidden) { closeAcctMenu(); return; }
        if ($('more-menu') && !$('more-menu').hidden) { closeMoreMenu(); return; }
        if (!$('inspector').hidden) { closeInspector(); }
        return;
      }
      var tag = (ev.target && ev.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { return; }
      if (ev.key === '/') {
        ev.preventDefault();
        openPalette();
      }
    });

    window.addEventListener('hashchange', route);
    window.addEventListener('pagehide', clearTimers);

    /* 从别的标签页登录完回来：平台 has_credentials / 账号状态可能已经变了。
       只在页面可见且距上次同步超过 3 秒时拉一次，避免频繁请求。 */
    var lastSync = 0;
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { return; }
      var now = Date.now();
      if (now - lastSync < 3000) { return; }
      lastSync = now;
      refreshAfterAccountChange();
    });
  }

  function boot() {
    var saved = 'dark';
    try { saved = localStorage.getItem('kdb.theme') || 'dark'; } catch (e) { saved = 'dark'; }
    applyTheme(saved);
    bind();

    api('/api/auth/me').then(function (body) {
      if (body && body.user) {
        adoptIdentity(body);
        return enterApp();
      }
      showLogin('boot');
      return null;
    }).catch(function (e) {
      if (e && e.status === 401) { showLogin('boot'); return; }
      showLogin('offline', describe(e));
      var note = $('boot');
      if (note) {
        note.hidden = false;
        note.textContent = describe(e);
      }
    });
  }

  boot();
})();
