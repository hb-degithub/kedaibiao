/* 课代表 kedaibiao · Web 控制台前端
 *
 * 约束（见 docs/web-console-api.md，本文件唯一的接口契约来源）：
 *   - 零构建、零依赖、零 CDN：只用原生 DOM API 与同源 fetch，页面不外发任何数据。
 *   - 认证（契约 §1.5）：令牌由 HttpOnly Cookie（kdb_session）承载，前端**不碰 localStorage**；
 *     所有请求带 credentials: 'same-origin'。任意 /api/* 返回 401 kind=unauthorized 就切到
 *     全屏登录页；登录成功后回到登录前所在的 hash 位置。
 *   - 角色：superadmin 能管理用户、能看所有人的数据（?owner_id=<id>）；user 只看得到自己的。
 *     「用户」入口只对超管显示，但**不只靠隐藏**：普通用户手改 hash 走到 #/users 时后端会
 *     403，页面把后端的中文 error 与 kind 原样显示出来。
 *   - 归属：数据接口按当前用户自动过滤，前端不传参；超管在顶栏选「用户」后数据请求带
 *     ?owner_id=<id>，聚合视图下列表多一列「归属」（响应每行带 owner_id）。
 *   - 导航：左侧栏一列两段 —— 上段切平台（全部平台 / pigai / pinco / unipus …），
 *           下段切「当前平台的功能」：概览 / 课程 / 作业 / 题库 / 工具 / 运行日志 /
 *           统计 / 账号 /〔仅超管〕用户。顶栏放「当前平台 + 归属筛选 + 干跑/真提交 + 当前用户」。
 *           当前页 + 当前平台存在 URL hash（#/功能/平台）里，刷新后仍停在同一处。
 *   - 「账号」页 = 平台多账号（/api/accounts）：一个平台下多个号、当前号切换、重新登录、
 *     增删改；每个账号的详细信息用 <details> 折叠（摘要行：标签 / 用户名 / 当前号 / 登录状态）。
 *   - 已完成历史与详细作答：工具页的平台专属动作（如批改网的 finished / essay），
 *     一律按响应 kind（table / detail / message）通用渲染，不为任何平台写专属页面；
 *     列表结果若有能对上的详情动作，按声明的必填参数从该行自动填好参数。
 *   - 没有横跨所有平台的能力对比表：当前选哪个平台，就只看那个平台的能力
 *     （概览页的能力摘要、账号页该平台那一行）。跨平台对比只在「统计」页。
 *   - 没有任何行内快捷跑批按钮：跑批一律在概览页的触发区，且必须明确选择平台。
 *   - 作业（assignments）页：行支持**双击**看详情（POST /api/assignments/detail，
 *     返回 kind: detail 的信封，通用渲染在右侧抽屉里，不整页跳走）；每行也有
 *     「详情」「提交」按钮，行的 title 写明「双击查看详情」以便发现。
 *   - 单作业提交是**两步**的：先 confirm=false 干跑（每题答案 + 置信度 + needs_human），
 *     展示后用户再确认才发 confirm=true。平台没实现提交时后端返回 501 not_supported ——
 *     界面写明「该平台尚未实现提交」并禁用该平台的行内「提交」（capabilities.submit === false
 *     时也禁用），绝不假装成功。干跑/真提交两条路径的硬门槛（一题没答、置信度低于阈值）
 *     由后端 400 拦下，前端只显示它的中文 error。
 *   - 大模型配置可在本页面直接改（POST /api/settings/llm），状态来自 /api/settings.llm；
 *     显示「未配置」）；前端**不收集 api_key**。
 *   - 浏览器会话抓取：契约没有 cookie 写入接口，所以账号页给可复制的命令行
 *     （capture --profile / --cookie），并说明「未登录的登录页也会下发匿名 csrfToken，
 *     所以要验证会话」。
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

  /* 功能页（顺序即左侧栏下段的顺序）；users 仅超管可见。 */
  var PAGES = ['overview', 'courses', 'assignments', 'bank', 'tools', 'logs',
               'stats', 'account', 'users'];
  var PAGE_LABEL = {
    overview: '概览', courses: '课程', assignments: '作业', bank: '题库',
    tools: '工具', logs: '运行日志', stats: '统计', account: '账号', users: '用户'
  };
  /* 仅超管可见的功能页（入口隐藏，但后端 403 前端照样要处理）。 */
  var ADMIN_PAGES = { users: 1 };
  /* 这些功能必须有具体平台才有意义；「全部平台」下给提示而不是空表。 */
  var NEEDS_PLATFORM = { courses: 1, assignments: 1, bank: 1, tools: 1 };
  /* 旧 hash 名（上一版导航）→ 新名字，老书签不失效。 */
  var PAGE_ALIAS = { records: 'logs', settings: 'account' };

  /* 角色展示名（契约 §1.5：superadmin / user）。未知角色原样显示。 */
  var ROLE_LABEL = { superadmin: '超管', user: '普通用户' };
  var ROLE_HINT = {
    superadmin: '超管：能管理用户，能看所有人的数据（可切「用户」筛选）',
    user: '普通用户：只能看自己的数据，不能进「用户」管理'
  };

  /* 密码规则（契约 §1.5：至少 8 位，否则 400）—— 前端先校验。 */
  var MIN_PASSWORD = 8;

  /* 认证相关路径：这些接口自身的 401 不算「会话过期」，不要切登录页。 */
  var AUTH_PATHS = { '/api/auth/login': 1, '/api/auth/logout': 1, '/api/auth/me': 1 };

  /* 归属（owner_id）只发给「按用户归属过滤有意义」的数据接口；
     /api/settings、/api/platforms、/api/platforms/<n>/actions/* 是全局或平台级，
     不按 owner 过滤，也不要带 owner_id。 */
  var OWNER_SCOPED = {
    '/api/overview': 1, '/api/runs': 1, '/api/submissions': 1, '/api/bank': 1,
    '/api/statistics': 1, '/api/jobs': 1, '/api/events': 1, '/api/accounts': 1
  };

  var LIVE_CONFIRM =
    '这会真的提交作业，不可撤销。\n\n' +
    '确定要把「真提交（live）」打开吗？\n' +
    '建议先用干跑确认规划结果（哪些作业会被处理），再决定是否真提交。';

  /* 单作业真提交的二次确认（比 live 开关更重：只影响这一个作业，但同样不可撤销）。 */
  var SUBMIT_CONFIRM =
    '这会真的把这个作业提交到平台，不可撤销。\n\n' +
    '后端还会拦两道门槛：一题都没答出来、或最低置信度低于阈值，都会直接拒绝（400）。\n\n' +
    '确定要真提交吗？';

  /* 「作业」页表格行里哪个键是任务号：契约的 /api/assignments/* 走 assignment_id，
     但列表信封的列键名由适配器自己声明（实测批改网是 id、待办是 rid），
     所以按固定优先级取第一个非空值 —— 接新平台不用改前端。 */
  var ASSIGN_ID_KEYS = ['assignment_id', 'assignmentId', 'id', 'rid', 'task_id', 'taskId'];
  var ASSIGN_TITLE_KEYS = ['title', 'name', 'label', 'subject', 'course_title'];

  /* 平台没实现提交时（501 not_supported）界面的固定说法。 */
  var NOT_SUPPORTED_TEXT = '该平台尚未实现提交';

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
    actions: { platform: null, list: null, active: null, result: null, busy: false, prefill: null },
    /* 一键自动化：job 轮询、最后一份快照、人工答案 */
    auto: { jobId: null, timer: null, job: null, platform: '', examId: '', title: '',
            manual: {}, saving: false, submitting: false, logCount: 0 },
    /* 平台执行日志面板 */
    plog: { open: false, timer: null, platform: '' },
    actionPages: {
      courses: { busy: false, loaded: false, error: '', result: null, params: {}, seq: 0 },
      assignments: { busy: false, loaded: false, error: '', result: null, params: {}, seq: 0 }
    },
    problems: {},            // key -> { message, level }

    /* --- 单作业提交：后端说过 501 not_supported 的平台（下次直接禁用行内「提交」）--- */
    noSubmit: {},

    /* --- 作业抽屉：详情（双击行）与两步提交 --- */
    drawer: {
      open: false,
      mode: '',              // 'detail' | 'submit'
      platform: null,
      assignmentId: '',
      title: '',
      busy: false,
      status: '',            // 加载 / 干跑中的状态文案
      error: '',             // 后端中文 error（经 describeError 补过「接下来做什么」）
      errorKind: '',
      notSupported: '',      // 501 not_supported 时后端的原文
      logTimer: null,        // 抽屉里执行日志的轮询计时器
      detail: null,          // POST /api/assignments/detail 的信封
      plan: null,            // POST /api/assignments/submit confirm=false 的原始响应
      planRows: null,        // 从 plan 里解析出的「每题答案 + 置信度」（解析不出就是 null）
      planStats: null,
      step: '',              // 'plan'（等确认） | 'done'（真提交已完成）
      result: null,          // confirm=true 的响应
      seq: 0
    },

    /* --- 认证 / 身份（契约 §1.5）--- */
    authed: false,
    authChecked: false,      // 已经成功确认过一次身份（用于区分「尚未登录」与「会话过期」）
    user: null,              // /api/auth/me 的 user 对象
    isSuper: false,
    ownerId: null,           // 超管专用：null = 不传 owner_id（看全部）
    users: [],               // /api/users 列表（超管；也用于 owner 下拉与「归属」列显示用户名）
    usersLoaded: false,
    usersError: '',
    usersLoading: false,
    usersPwOpen: null,       // 正在改密的用户 id
    loginBusy: false,
    returnHash: '',          // 登录前的位置（登录后回到原页）
    authNotice: '',

    /* --- 平台多账号（契约 §1.5）--- */
    acct: {
      list: null,            // null = 还没取到；[] = 确实没有
      busy: false,
      error: '',
      seq: 0,
      open: {},              // account id -> <details> 是否展开（重绘后保留）
      panel: {},             // account id -> 'relogin' | 'rename'
      err: {},               // account id -> 行内错误
      actId: null            // 正在执行操作的 account id
    }
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

  /* 只取路径部分（去掉 query），用于判断是不是 auth 接口。 */
  function pathOnly(path) {
    var s = String(path || '');
    var i = s.indexOf('?');
    return i >= 0 ? s.slice(0, i) : s;
  }

  function api(path, options) {
    var opts = options || {};
    opts.cache = 'no-store';
    /* 契约 §1.5：令牌是 HttpOnly Cookie，浏览器自动带上；前端不存令牌。 */
    opts.credentials = 'same-origin';
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
          var err = new HttpError(kind, msg, res.status, path, detail);
          /* 契约 §1.5：未登录访问任何 /api/*（除 auth/*）→ 401 + kind unauthorized，
             前端收到就切登录页。auth 接口自身的 401 由登录表单处理。 */
          if (res.status === 401 && !AUTH_PATHS[pathOnly(path)]) {
            onUnauthorized();
          }
          throw err;
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
  var FORBIDDEN_HINT =
    '这可能是权限不足（「用户」管理只对超管开放，普通用户带 ?owner_id= 指向别人也会被拒），' +
    '也可能是 Host / Origin 被安全策略拒绝 —— 请通过 http://127.0.0.1:<端口> 访问本页，' +
    '不要用别的域名或 file:// 打开。';

  var ERROR_HINTS = {
    unauthorized: '未登录或会话已过期（401）：请重新登录。',
    no_credentials: '请在 .env 里补全该平台的账号配置（error 里写了期望的键名），重启服务后再试。',
    unknown_platform: '平台没注册或名字不对。点「刷新」重读平台列表。',
    unknown_action: '该平台没有声明这个只读动作。「刷新」会重读动作清单。',
    busy: '同一时刻只允许一个跑批任务，请等当前任务结束后再触发。',
    bad_request: '请检查请求参数（platform / live / limit / kinds 或动作参数；密码至少 8 位）。',
    forbidden: FORBIDDEN_HINT,
    /* 后端若给 403 另起 kind 名，这里一并认掉，别让用户看到光秃秃的状态码。 */
    permission_denied: FORBIDDEN_HINT,
    not_allowed: FORBIDDEN_HINT,
    not_found: '目标不存在或已被清理。',
    not_supported: '该平台尚未实现这个能力（例如「提交」）：后端返回 501 not_supported。' +
      '界面会禁用对应按钮，要接入得先按真实抓包实现该平台的写接口 —— 不会假装成功。',
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
    /* 403：无论 kind 叫什么，都按「权限 / 安全策略」给人话（后端原文优先）。 */
    if (e && e.status === 403 && base.indexOf(FORBIDDEN_HINT) === -1) {
      base += '（kind: ' + dash(kind) + '。' + FORBIDDEN_HINT + '）';
      return base;
    }
    var hint = ERROR_HINTS[kind];
    if (hint && base.indexOf(hint) === -1) { base += '（' + hint + '）'; }
    return base;
  }

  /* ==================================================================
     认证 / 身份 / 归属（契约 §1.5）
     令牌在 HttpOnly Cookie 里 —— 这里只跟 /api/auth/* 与身份状态打交道，
     不读也不写 localStorage。
     ================================================================== */

  function isSuperadmin() { return state.isSuper === true; }

  function roleText(role) { return ROLE_LABEL[role] || role || '—'; }

  function stopAllTimers() { cancelTimer(); }

  /* 登录页：全屏接管。控制台整体隐藏只是 UI 状态，权限由后端强制。 */
  function showLogin(reason, message) {
    $('app').hidden = true;
    $('login-screen').hidden = false;
    setBoxError('login-error', message || '');
    $('login-state').textContent = '';
    $('login-retry').hidden = (reason !== 'offline');
    $('login-note').textContent = loginNote(reason);
    var u = $('login-username');
    if (u && !u.value) { u.focus(); }
    else if (u) { $('login-password').focus(); }
  }

  function loginNote(reason) {
    if (reason === 'expired') {
      return '会话已过期（后端返回 401 unauthorized）：请重新登录。' +
             '你的位置（' + (state.returnHash || location.hash || '#/overview') + '）会保留。';
    }
    if (reason === 'logout') { return '已退出登录。换账号登录后，数据会按新用户重新加载。'; }
    if (reason === 'offline') {
      return '取不到会话状态：请确认控制台服务已启动，然后点「重试连接」或直接登录。';
    }
    return '登录后所有数据按当前用户隔离；超管可以切换「用户」筛选查看某个用户的数据。';
  }

  function hideLogin() {
    $('login-screen').hidden = true;
    $('app').hidden = false;
    setBoxError('login-error', '');
    $('login-password').value = '';
    $('login-state').textContent = '';
  }

  function isOnLoginScreen() { return !$('login-screen').hidden; }

  /* 任意 /api/*（auth 除外）返回 401：停轮询、清状态、切登录页。 */
  function onUnauthorized() {
    if (isOnLoginScreen()) { return; }   // 已经在登录页，别重复处理并发 401
    stopAllTimers();
    state.authed = false;
    state.isSuper = false;
    state.ownerId = null;
    state.users = [];
    state.usersLoaded = false;
    state.usersError = '';
    state.acct = {
      list: null, busy: false, error: '', seq: state.acct.seq + 1,
      open: {}, panel: {}, err: {}, actId: null
    };
    state.currentId = null;
    state.currentJob = null;
    state.problems = {};
    renderProblems();
    if (!state.returnHash) { state.returnHash = location.hash || ''; }
    renderChrome();
    showLogin(state.authChecked ? 'expired' : 'boot');
  }

  /* 采纳身份：/api/auth/me 给 {user, is_superadmin}；登录只保证 {user}。
     角色以 is_superadmin 为准，缺字段时退回 user.role。 */
  function adoptIdentity(body) {
    var u = body && body.user;
    if (isObj(u)) { state.user = u; }
    var flag = body ? body.is_superadmin : undefined;
    if (flag !== true && flag !== false) {
      flag = !!(state.user && state.user.role === 'superadmin');
    }
    state.isSuper = flag === true;
    if (!state.isSuper) { state.ownerId = null; }
    state.authChecked = true;
  }

  function fetchIdentity() {
    return api('/api/auth/me').then(function (body) {
      adoptIdentity(body);
      return body;
    });
  }

  /* 启动时的会话探测：401 = 未登录（登录页），网络错误 = 服务没起来。 */
  function startSession() {
    return api('/api/auth/me').then(function (body) {
      adoptIdentity(body);
      enterConsole();
    }).catch(function (e) {
      if (e && e.status === 401) {
        state.authChecked = false;
        showLogin('boot');
        return;
      }
      showLogin('offline', describeError(e));
    });
  }

  /* 身份确认后进入控制台：回到登录前的位置，再按当前页取数。 */
  function enterConsole() {
    state.authed = true;
    hideLogin();
    /* 登录期间 hash 没被改过就是原位置；万一被改过，回到登录前那个位置。 */
    var back = state.returnHash;
    state.returnHash = '';
    if (back && back !== location.hash && window.history && window.history.replaceState) {
      window.history.replaceState(null, '', back);
    }
    var r = parseHash();
    state.page = r.page;
    state.platform = r.platform;
    renderChrome();
    showPage();
    if (isSuperadmin()) { loadUsers({ silent: true }); }
    return loadPlatforms().then(function () {
      var r2 = parseHash();
      var plt = sanitizePlatform(r2.platform);
      if (plt !== state.platform) {
        state.platform = plt;
        replaceHash(state.page, plt);
        renderChrome();
      }
      syncRunPlatformOptions(true);
      applyRunConstraints();
      return loadPageData(true);
    });
  }

  function doLogin(ev) {
    if (ev) { ev.preventDefault(); }
    if (state.loginBusy) { return; }
    var username = String($('login-username').value || '').trim();
    var password = String($('login-password').value || '');
    if (!username || !password) {
      setBoxError('login-error', '请填写用户名和密码。');
      return;
    }
    state.loginBusy = true;
    setBoxError('login-error', '');
    $('login-state').textContent = '正在登录 POST /api/auth/login …';
    $('login-submit').disabled = true;

    postJson('/api/auth/login', { username: username, password: password }).then(function (body) {
      var u = body && body.user;
      if (!isObj(u) || !u.role) {
        /* 登录响应里没带角色：补一次 /api/auth/me 再进控制台。 */
        return fetchIdentity().then(function () { enterConsole(); });
      }
      adoptIdentity(body);
      enterConsole();
    }).catch(function (e) {
      if (e && e.status === 401) {
        setBoxError('login-error', '账号或密码不对（401 unauthorized）。请检查大小写后重试。');
      } else if (e && e.kind === 'network') {
        setBoxError('login-error', describeError(e));
        $('login-retry').hidden = false;
      } else {
        setBoxError('login-error', '登录失败：' + describeError(e));
      }
    }).then(function () {
      state.loginBusy = false;
      $('login-submit').disabled = false;
      $('login-state').textContent = '';
    });
  }

  function doLogout() {
    var btn = $('logout-btn');
    btn.disabled = true;
    postJson('/api/auth/logout', {}).catch(function () {
      /* 退出请求失败也照样切回登录页：cookie 可能已经清掉了。 */
    }).then(function () {
      btn.disabled = false;
      stopAllTimers();
      state.authed = false;
      state.user = null;
      state.isSuper = false;
      state.ownerId = null;
      state.users = [];
      state.usersLoaded = false;
      state.usersError = '';
      state.usersPwOpen = null;
      state.acct = {
        list: null, busy: false, error: '', seq: state.acct.seq + 1,
        open: {}, panel: {}, err: {}, actId: null
      };
      state.overview = null;
      state.bank = null;
      state.statistics = null;
      state.settings = null;
      state.runs = [];
      state.statRuns = [];
      state.submissions = [];
      state.jobs = [];
      state.currentId = null;
      state.currentJob = null;
      state.returnHash = location.hash || '';
      clearProblemsWithPrefix('scope:');
      state.problems = {};
      renderProblems();
      resetTools();
      resetActionPage('courses');
      resetActionPage('assignments');
      renderChrome();
      showPage();
      showLogin('logout');
    });
  }

  /* ---- 超管的归属筛选（?owner_id=）---- */

  function ownerQuery() {
    if (!isSuperadmin()) { return ''; }
    if (state.ownerId === null || state.ownerId === undefined || state.ownerId === '') { return ''; }
    return 'owner_id=' + encodeURIComponent(state.ownerId);
  }

  /* 只给「按归属过滤有意义」的接口带 owner_id；其它 URL 原样返回。 */
  function withOwner(url) {
    if (!OWNER_SCOPED[pathOnly(url)]) { return url; }
    var oq = ownerQuery();
    if (!oq) { return url; }
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + oq;
  }

  function ownerLabel(id) {
    if (id === null || id === undefined || id === '') { return '—'; }
    for (var i = 0; i < state.users.length; i++) {
      if (String(state.users[i].id) === String(id)) {
        return state.users[i].username ? String(state.users[i].username) : ('#' + id);
      }
    }
    return '#' + id;
  }

  /* 归属列：超管 + 「全部用户」时才有信息量。 */
  function showOwnerColumn() { return isSuperadmin() && state.ownerId === null; }

  function renderOwnerFilter() {
    var wrap = $('owner-filter-wrap');
    if (!wrap) { return; }
    var sup = isSuperadmin();
    wrap.hidden = !sup;
    if (!sup) { return; }
    var sel = $('owner-filter');
    var kids = [el('option', '', '全部用户（不传 owner_id）')];
    kids[0].value = '';
    for (var i = 0; i < state.users.length; i++) {
      var u = state.users[i] || {};
      var o = el('option', '', String(u.username || ('#' + u.id)) + '（' + roleText(u.role) + '）');
      o.value = String(u.id);
      kids.push(o);
    }
    fill(sel, kids);
    sel.value = (state.ownerId === null || state.ownerId === undefined) ? '' : String(state.ownerId);
    sel.disabled = !state.usersLoaded;
    sel.title = state.usersLoaded
      ? '超管：按归属用户过滤所有数据接口；留空 = 看全部（不传 owner_id）'
      : '用户列表还没取到，暂时只能看全部';
  }

  function onOwnerChange() {
    var sel = $('owner-filter');
    var v = String(sel.value || '');
    state.ownerId = v === '' ? null : v;
    /* 归属一换，所有归属维度的列表与缓存都失效。 */
    state.runs = [];
    state.submissions = [];
    state.jobs = [];
    state.statRuns = [];
    state.statistics = null;
    state.acct.list = null;
    state.acct.open = {};
    state.acct.panel = {};
    state.acct.err = {};
    state.acct.seq++;
    clearProblemsWithPrefix('scope:');
    showToast(v === '' ? '归属筛选：全部用户（不传 owner_id）' : ('归属筛选：' + ownerLabel(v)));
    renderChrome();
    loadOverview();
    loadJobs();
    loadEvents();
    loadPageData(true);
  }

  /* 响应信封兜底：契约只写了「列表 + 每人账号数」，这里按常见键名兼容；
     裸数组也认。字段名与契约不同时不影响功能。 */
  function extractList(body, keys) {
    if (Array.isArray(body)) { return body; }
    if (!isObj(body)) { return []; }
    for (var i = 0; i < keys.length; i++) {
      if (Array.isArray(body[keys[i]])) { return body[keys[i]]; }
    }
    return [];
  }

  function accountCountOf(u) {
    var keys = ['account_count', 'accounts_count', 'account_total', 'account_num', 'accounts'];
    for (var i = 0; i < keys.length; i++) {
      var v = u[keys[i]];
      if (isNum(v)) { return v; }
      if (Array.isArray(v)) { return v.length; }
    }
    return null;
  }

  function fmtDisabled(v) {
    if (v === true) { return '停用'; }
    if (v === false) { return '启用'; }
    return '—';
  }

  /* 名单里「停用 / 启用」的徽标（用户页与账号页共用同一套措辞）。 */
  function disabledBadge(v) {
    return v === true ? badge(fmtDisabled(true), 'badge-fail') : badge(fmtDisabled(false), 'badge-ok');
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
    url = withOwner(url);
    return api(url).then(function (body) {
      return { platform: plt, ownerId: state.ownerId, body: body, url: url };
    });
  }

  /* 响应到达时平台与归属是否还是同一个（切平台/切用户很快时丢弃过期响应）。 */
  function stillCurrent(res) {
    return !!res && res.platform === state.platform && res.ownerId === state.ownerId;
  }

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

    /* 自动化还没跑完就换界面：先弹确认。取消就把地址栏改回去，不真的切。 */
    if ((changedPage || changedPlat) && autoRunning()) {
      if (!window.confirm(autoLeaveWarning())) {
        replaceHash(state.page, state.platform);
        return;
      }
    }

    state.page = r.page;
    state.platform = r.platform;

    if (changedPlat) {
      resetTools();
      resetActionPage('courses');
      resetActionPage('assignments');
      clearProblemsWithPrefix('scope:');
    }
    /* 抽屉里的详情/提交是「某个平台某个作业」的，切平台或切页就关掉，避免张冠李戴。 */
    if (changedPage || changedPlat) { closeDrawer(); }
    /* 一键自动化同理：面板属于「某个平台某场考试」，切走就收起（后台任务不动）。 */
    if (changedPage || changedPlat) { closeAuto(true); }
    /* 平台执行日志也是按平台区分的，切走就收起 */
    if (changedPage || changedPlat) { closePlatLogs(); }
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
    renderUserBox();
    renderOwnerFilter();
    renderSideMeta();
    renderScopeHints();
    syncRunPlatformOptions();
    syncAccountPlatformOptions();
    syncCapturePlatformOptions();
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
      /* 「用户」入口只对超管显示；普通用户即使手改 hash 进去，后端会 403，
         页面（gate）显示后端的中文说明 —— 隐藏只是少一个入口，不是权限。 */
      if (ADMIN_PAGES[page]) {
        var li = nodes[i].parentNode;
        if (li) { li.hidden = !isSuperadmin(); }
      }
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

  /* 顶栏右上：当前用户（用户名 + 角色徽标）+ 退出按钮。 */
  function renderUserBox() {
    var u = state.user || {};
    var name = u.username ? String(u.username) : null;
    setText('top-username', name || '—');
    var role = $('top-role');
    if (name && isSuperadmin()) {
      role.className = 'badge badge-admin';
      role.textContent = roleText('superadmin');
    } else if (name) {
      role.className = 'badge badge-idle';
      role.textContent = roleText(u.role);
    } else {
      role.className = 'badge badge-idle';
      role.textContent = '—';
    }
    role.title = (ROLE_HINT[isSuperadmin() ? 'superadmin' : u.role] || '当前登录用户') +
      (u.disabled === true ? ' · 该账号已被停用' : '');
    var box = $('top-user');
    if (box) {
      box.title = name
        ? ('当前登录：' + name + '（' + roleText(u.role) + '，user id ' + dash(u.id) + '）')
        : '尚未登录';
    }
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
    var ownerNote = isSuperadmin()
      ? (' · 归属：' + (state.ownerId === null ? '全部用户' : ownerLabel(state.ownerId)))
      : ' · 归属：仅自己（后端强制）';
    setText('ov-scope', '作用域：' + scope +
      (state.platform ? '' : ' · 概览 / 运行日志 / 统计 / 账号在聚合视图下仍可用') + ownerNote);

    var platHint = state.platform
      ? ('平台：' + platformLabel(state.platform) + '（' + state.platform + '）')
      : '未选择平台';
    setText('courses-scope', platHint);
    setText('assignments-scope', platHint);
    if (typeof renderAutoReopen === 'function') { renderAutoReopen(); }
    setText('bank-scope', platHint);
    setText('tools-scope', platHint);
    setText('logs-scope', '作用域：' + scope +
      ' · 跑批历史与提交记录按该作用域过滤，聚合视图下每行显示平台列' + ownerNote);
    setText('stats-scope', '跨平台对比：不受当前平台过滤（' + scope + ' 只是你刚才看的位置）' + ownerNote);
    setText('account-scope', '作用域：' + scope + ' · 每个平台下可以有多个号，随时切换当前号' + ownerNote);
    setText('users-scope', isSuperadmin()
      ? '超管视图：所有人 + 每人账号数；顶栏「用户」筛选可切到某个人看数据'
      : '仅超管可用：非超管请求 /api/users* 会被后端拒绝（403）');
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
    var llm = llmSource();
    var pairs = [
      ['version', o.version],
      ['python', o.python],
      ['repo_root', o.repo_root],
      ['db_path', o.db_path],
      ['db_exists', o.db_exists === undefined ? null : fmtBool(o.db_exists)],
      ['platform_count', o.platform_count],
      ['大模型答题', llm.llm === null ? '未配置' : (llmEnabled(llm.llm) ? '已启用' : '未配置')],
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
    renderLlm();
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
    /* 会话没了就别再每 1 秒打一次 401。 */
    if (!state.authed) { stopPolling(); return; }
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
      if (!state.authed) { stopPolling(); return; }
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
    var envOpts = { emptyText: '该动作没有返回任何' + cfg.dataName + '（0 行）。' };
    if (key === 'assignments') {
      /* 作业页（契约 §3 新增）：双击行看详情；每行「详情 / 提交」两个按钮。
         「提交」在平台 capabilities.submit === false 或后端回过 501 时禁用。 */
      envOpts.rowActions = assignRowActions;
      envOpts.actionsLabel = '操作';
      envOpts.onRowDblClick = onAssignRowDblClick;
      envOpts.rowTitle = '双击查看详情（POST /api/assignments/detail，只读）';
    }
    renderEnvelope($(key + '-result'), ps.result, envOpts);
    if (key === 'assignments') { renderAssignSubmitNote(); }

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

  /* ==================================================================
     作业详情（双击行） + 单作业提交（两步：干跑 → 确认）
     契约 §3：
       POST /api/assignments/detail {platform, assignment_id}
         → kind: detail 的展示信封（任务号 / 名称 / 课程 / 截止 / 题目 / 平台原始数据）
       POST /api/assignments/submit {platform, assignment_id, confirm}
         confirm=false（默认）：只规划不提交，返回每题打算怎么答 + 置信度
         confirm=true：真的调平台写接口
         平台没实现提交 → 501 not_supported（界面写明「该平台尚未实现提交」并禁用按钮）
         两条硬门槛（confirm=true 也拦）：一题没答 → 400；最低置信度 < min_confidence → 400
     展示用抽屉，不整页跳走；所有文本走 textContent，不拼 HTML。
     ================================================================== */

  /* 任务号：列表行的列键名由适配器声明（实测批改网作业表是 id、待办是 rid），
     而新接口要的是 assignment_id —— 所以按固定优先级取第一个非空值。 */
  function assignmentIdOf(rowObj) {
    if (!isObj(rowObj)) { return ''; }
    for (var i = 0; i < ASSIGN_ID_KEYS.length; i++) {
      var v = rowObj[ASSIGN_ID_KEYS[i]];
      if (v === null || v === undefined) { continue; }
      var s = String(v).trim();
      if (s) { return s; }
    }
    return '';
  }

  function assignmentTitleOf(rowObj) {
    if (!isObj(rowObj)) { return ''; }
    for (var i = 0; i < ASSIGN_TITLE_KEYS.length; i++) {
      var v = rowObj[ASSIGN_TITLE_KEYS[i]];
      if (v === null || v === undefined) { continue; }
      var s = String(v).trim();
      if (s) { return s; }
    }
    return '';
  }

  /* 平台现在能不能提交：'' = 可以试；非空 = 禁用的原因。
     两个来源：后端已经回过 501 not_supported（state.noSubmit 记着原文），
     或能力表 capabilities.submit === false。能力未知时不预判，
     让用户点了由后端回答（后端可能还没实现这个能力，那时回到 501 这条路）。 */
  function submitBlockReason(plt) {
    if (!plt) { return '未选择平台'; }
    if (state.noSubmit[plt]) { return NOT_SUPPORTED_TEXT + '（后端已返回 501 not_supported）'; }
    var p = platformByName(plt);
    if (p && isObj(p.capabilities) && p.capabilities.submit === false) {
      return NOT_SUPPORTED_TEXT + '（capabilities.submit = false）';
    }
    return '';
  }

  /* 后端说过 501：把这个平台记下来，行内「提交」立即禁用，并在作业页顶部说明。 */
  function markNoSubmit(plt, e) {
    if (!plt) { return; }
    state.noSubmit[plt] = ((e && e.error) || '').trim() ||
      (NOT_SUPPORTED_TEXT + '（后端 501 not_supported）');
    renderAssignSubmitNote();
    renderActionPage('assignments');
  }

  function renderAssignSubmitNote() {
    var box = $('assign-submit-note');
    if (!box) { return; }
    var plt = state.platform;
    var reason = plt ? submitBlockReason(plt) : '';
    if (!reason) { box.hidden = true; box.textContent = ''; return; }
    box.hidden = false;
    box.textContent = platformLabel(plt) + '：' + reason +
      ' —— 行内「提交」已禁用（作业列表与详情仍可正常查看，只读），界面不会假装提交成功。' +
      (state.noSubmit[plt] ? ' 后端原文：' + state.noSubmit[plt] : '');
  }

  /* 行内操作：详情（只读）+ 提交（两步）。返回 null 表示这行没有可用任务号。 */
  function assignRowActions(rowObj) {
    var id = assignmentIdOf(rowObj);
    if (!id) { return null; }
    var plt = state.platform;
    var title = assignmentTitleOf(rowObj);
    var out = [];

    var bDetail = el('button', 'btn btn-sm', '详情');
    bDetail.type = 'button';
    bDetail.title = '查看作业详情（只读，POST /api/assignments/detail）；双击这一行同样可以';
    bDetail.addEventListener('click', function () { openAssignmentDetail(plt, id, rowObj); });
    out.push(bDetail);

    var reason = submitBlockReason(plt);

    /* iTEST 的作答/交卷在浏览器里完成：给它一个「一键自动化」入口（放在「详情」右边） */
    if (plt === 'itest' && !reason) {
      var bAuto = el('button', 'btn btn-sm btn-warn', '一键自动化');
      bAuto.type = 'button';
      bAuto.title = '取卷 → 音频转写 + AI 答题 → 填写 → 逐项核验 → 重新抓取复核；' +
        '缺的题可在下方手动补，最后再确认交卷';
      bAuto.addEventListener('click', function () { startAuto(plt, id, rowObj); });
      out.push(bAuto);
    }

    var bSub = el('button', 'btn btn-sm ' + (reason ? 'btn-ghost' : 'btn-warn'), '提交');
    bSub.type = 'button';
    bSub.setAttribute('data-submit-for', id);
    if (reason) {
      bSub.disabled = true;
      bSub.title = reason + ' —— 已禁用；平台没实现提交时后端返回 501 not_supported，界面不假装成功。';
    } else {
      bSub.title = '两步提交：先干跑（confirm=false）看每题答案与置信度，确认后才真提交' +
        (title ? '：' + title : '');
      bSub.addEventListener('click', function () { openAssignmentSubmit(plt, id, rowObj); });
    }
    out.push(bSub);
    return out;
  }

  function onAssignRowDblClick(rowObj, ev) {
    if (ev && ev.target && ev.target.closest &&
        ev.target.closest('button, a, input, select')) {
      return;   // 点在行内按钮/链接上，不算「双击这一行」
    }
    var id = assignmentIdOf(rowObj);
    if (!id) {
      showToast('这一行没有可用的任务号（没有 id / assignment_id 字段），无法打开详情。');
      return;
    }
    openAssignmentDetail(state.platform, id, rowObj);
  }

  /* ---- 抽屉骨架 ---- */

  function drawerReset(mode, plt, id, title) {
    var d = state.drawer;
    d.open = true;
    d.mode = mode;
    d.platform = plt;
    d.assignmentId = id;
    d.title = title || ('作业 ' + id);
    d.busy = false;
    d.status = '';
    d.error = '';
    d.errorKind = '';
    d.notSupported = '';
    d.detail = null;
    d.plan = null;
    d.planRows = null;
    d.planStats = null;
    d.step = '';
    d.result = null;
    d.seq++;
    return d;
  }

  function openDrawer(mode, plt, id, title) {
    drawerReset(mode, plt, id, title);
    renderDrawer();
    var close = $('drawer-close');
    if (close && close.focus) { close.focus(); }
  }

  function closeDrawer() {
    var d = state.drawer;
    if (!d.open) { return; }
    graderStopPolling();
    drawerLogStop();
    var host = $('grader-host');
    if (host && host.parentNode) { host.parentNode.removeChild(host); }
    d.open = false;
    d.seq++;                  // 丢弃还在路上的响应
    renderDrawer();
  }

  function renderDrawer() {
    var d = state.drawer;
    var root = $('drawer');
    if (!root) { return; }
    if (!d.open) {
      root.hidden = true;
      return;
    }
    root.hidden = false;

    setText('drawer-title', d.title || '作业');
    var meta = $('drawer-meta');
    clear(meta);
    meta.appendChild(el('span', 'hint', '平台 '));
    meta.appendChild(el('span', 'mono', dash(d.platform)));
    meta.appendChild(el('span', 'hint', ' · 任务号 '));
    meta.appendChild(el('span', 'mono', dash(d.assignmentId)));
    meta.appendChild(el('span', 'hint', ' · '));
    if (d.mode === 'submit') {
      meta.appendChild(badge(d.step === 'done' ? '真提交' : '两步提交',
        d.step === 'done' ? 'badge-live' : 'badge-dry'));
    } else {
      meta.appendChild(badge('只读', 'badge-ro'));
      /* 详情信封的 action / kind 移到抽屉头上，正文里就不再重复标题。 */
      if (d.detail && d.detail.action) {
        meta.appendChild(el('span', 'hint', ' · 动作 '));
        meta.appendChild(el('span', 'mono', d.detail.action));
      }
      if (d.detail && d.detail.kind) {
        meta.appendChild(el('span', 'hint', ' · '));
        meta.appendChild(badge('kind: ' + d.detail.kind, 'badge-ok'));
      }
    }

    var st = $('drawer-status');
    st.hidden = !d.status;
    st.textContent = d.status || '';
    st.className = 'drawer-status' + (d.busy ? ' is-busy' : '');

    setBoxError('drawer-error', d.error);

    renderDrawerBody();
    renderDrawerFoot();
  }

  function renderDrawerBody() {
    var d = state.drawer;
    var box = $('drawer-body');
    clear(box);
    if (d.busy && !d.detail && !d.plan) { return; }   // 加载中：状态条已经说明了

    if (d.notSupported) {
      var nb = el('div', 'alert-box alert-soft');
      nb.appendChild(el('div', 'banner-title', NOT_SUPPORTED_TEXT));
      nb.appendChild(el('p', 'res-message', d.notSupported));
      nb.appendChild(el('p', 'hint',
        '该平台的行内「提交」已禁用。作业列表与详情仍可正常查看（只读）；' +
        '要接入提交得先按真实抓包实现该平台的写接口。'));
      box.appendChild(nb);
      return;
    }

    if (d.mode === 'detail') {
      if (d.platform === 'pigai' && d.assignmentId) {
        graderReset(d.platform, d.assignmentId);
        graderRender();
      }
      if (d.detail) {
        /* 标题已经在抽屉头上（还有平台 / 任务号 / kind 徽标），这里不再重复一遍。 */
        renderEnvelope(box, d.detail, { noTitle: true, emptyText: '详情响应里没有可渲染的内容。' });
      }
      /* 这个平台的执行日志（动作/跑批/评分循环/自动化都在这），跟详情放在一起看 */
      if (d.platform) { drawerPlatformLog(box, d.platform); }
      return;
    }

    if (d.mode === 'submit') {
      if (d.step === 'done' && d.result) {
        box.appendChild(el('div', 'live-banner', '真提交已发出：下面是后端返回的结果。'));
        renderEnvelope(box, d.result, { emptyText: '提交响应里没有可渲染的内容。' });
        return;
      }
      if (d.plan) {
        box.appendChild(el('div', 'dry-banner',
          '这是干跑（confirm=false）的结果：只规划、没有向平台提交任何内容。'));
        if (d.planRows && d.planRows.length) {
          box.appendChild(planBlock(d.planRows, d.planStats));
          var det = el('details');
          det.appendChild(el('summary', '', '展开后端原始响应（kind: ' +
            dash(d.plan.kind) + '）'));
          var inner = el('div', 'action-result');
          renderEnvelope(inner, d.plan, {});
          det.appendChild(inner);
          box.appendChild(det);
        } else {
          renderEnvelope(box, d.plan, { emptyText: '干跑没有返回任何题目。' });
        }
      }
    }
  }


  /* ===================== 抽屉里的平台执行日志 ===================== */

  function drawerLogStop() {
    var d = state.drawer;
    if (d.logTimer) { clearInterval(d.logTimer); d.logTimer = null; }
  }

  function drawerPlatformLog(box, plt) {
    var det = el('details', 'drawer-plog');
    det.open = true;
    var summary = el('summary', '', '执行日志 · ' + platformLabel(plt));
    det.appendChild(summary);

    var pre = el('pre', 'auto-log');
    pre.id = 'drawer-plog-log';
    det.appendChild(pre);

    var foot = el('div', 'action-submit');
    var btn = el('button', 'btn btn-sm', '刷新');
    btn.type = 'button';
    btn.addEventListener('click', function () { fetchDrawerLog(plt); });
    foot.appendChild(btn);
    var hint = el('span', 'field-hint', '每 2.5 秒自动刷新');
    hint.id = 'drawer-plog-hint';
    foot.appendChild(hint);
    det.appendChild(foot);

    box.appendChild(det);
    fetchDrawerLog(plt);
    drawerLogStop();
    state.drawer.logTimer = setInterval(function () {
      if (!state.drawer.open) { drawerLogStop(); return; }
      fetchDrawerLog(plt);
    }, 2500);
  }

  function fetchDrawerLog(plt) {
    var pre = $('drawer-plog-log');
    if (!pre) { return; }
    api('/api/platforms/' + encodeURIComponent(plt) + '/logs?limit=80')
      .then(function (body) {
        var node = $('drawer-plog-log');
        if (!node) { return; }
        paintLog(node, (body && body.logs) || []);
        var hint = $('drawer-plog-hint');
        if (hint) {
          hint.textContent = '共 ' + ((body && body.total) || 0) + ' 行（显示最近 80 行）';
        }
      })
      .catch(function (e) {
        paintLog(pre, [{ at: '', level: 'error', text: '加载失败：' + describeError(e) }]);
      });
  }

  /* ===================== 评分循环（批改网） ===================== */

  var grader = {
    platform: '', rid: '', target: 90, maxAttempts: 3, analysisTimeout: 180,
    job: null, timer: null, history: null, busy: false, error: '', note: ''
  };

  var GRADER_STOP_TEXT = {
    reached: '已达目标分',
    exhausted: '轮次用尽，仍未达标',
    unavailable: '暂时没取到分数',
    error: '出错了',
    timeout: '等批改超时（稿子已交，只是还没出分）',
    fetched: '已取到分数'
  };

  var GRADER_EVENT_TEXT = {
    'attempt.existing': '发现已有稿件（{eid}，{raw_state}）—— 先取分，不提交',
    'attempt.start': '开始第 {attempt} 轮',
    'attempt.submitted': '已提交（稿件号 {eid}）',
    'attempt.analysis': '分析中… （{tick}）',
    'attempt.graded': '出分：{score}（批注 {feedback_count} 条）',
    'attempt.rewriting': '按批注改写中…',
    'attempt.resubmit_without_score': '没有分数，按你的选择继续改写重交',
    'attempt.error': '出错：{error}',
    'attempt.timeout': '等分超时：{error}',
    'loop.finish': '循环结束'
  };

  function graderStopPolling() {
    if (grader.timer) { clearTimeout(grader.timer); grader.timer = null; }
  }

  function graderReset(platform, rid) {
    graderStopPolling();
    grader = {
      platform: platform, rid: rid, target: 90, maxAttempts: 3, analysisTimeout: 180,
      job: null, timer: null, history: null, busy: false, error: '', note: ''
    };
  }

  function graderApi(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  }

  function graderEventText(ev) {
    var tpl = GRADER_EVENT_TEXT[ev.kind] || ev.kind;
    return tpl.replace(/\{(\w+)\}/g, function (_m, key) {
      var v = ev[key];
      return v === undefined || v === null || v === '' ? '—' : String(v);
    });
  }

  function graderPanel(container) {
    var box = el('section', 'grader');
    box.appendChild(el('h3', 'grader-title', '评分循环 · 批改网'));

    var hint = el('p', 'hint',
      '流程：提交 → 触发批改网分析 → 取分与批注 → 未达标则按批注改写 → 再交。' +
      '「开始自动改写」会**真的反复提交作文**（不可撤销）。');
    box.appendChild(hint);

    /* 控制条 */
    var controls = el('div', 'grader-controls');
    controls.appendChild(graderNumber('目标分数', grader.target, 0, 100,
      function (v) { grader.target = v; }));
    controls.appendChild(graderNumber('轮次上限', grader.maxAttempts, 1, 10,
      function (v) { grader.maxAttempts = v; }));
    controls.appendChild(graderNumber('等分上限（秒）', grader.analysisTimeout, 20, 1800,
      function (v) { grader.analysisTimeout = v; }));
    box.appendChild(controls);

    var actions = el('div', 'grader-actions');
    var startBtn = el('button', 'btn btn-danger btn-sm', '开始自动改写');
    startBtn.type = 'button';
    startBtn.addEventListener('click', graderStart);

    var fetchBtn = el('button', 'btn btn-sm', '只取分（不提交）');
    fetchBtn.type = 'button';
    fetchBtn.addEventListener('click', graderFetch);

    var histBtn = el('button', 'btn btn-ghost btn-sm', '历史提交');
    histBtn.type = 'button';
    histBtn.addEventListener('click', graderHistory);

    actions.appendChild(startBtn);
    actions.appendChild(fetchBtn);
    actions.appendChild(histBtn);
    box.appendChild(actions);

    if (grader.busy) {
      startBtn.disabled = true; fetchBtn.disabled = true;
      startBtn.textContent = '运行中…';
      box.appendChild(el('p', 'hint', '正在运行：每 1 秒刷新一次进度。'));
    }
    if (grader.error) {
      var err = el('div', 'err-box');
      err.appendChild(el('div', 'banner-title', '失败'));
      err.appendChild(el('p', 'res-message', grader.error));
      box.appendChild(err);
    }
    if (grader.note) { box.appendChild(el('p', 'hint', grader.note)); }

    /* 任务进度 */
    if (grader.job) { box.appendChild(graderJobBlock(grader.job)); }

    /* 历史 */
    if (grader.history) { box.appendChild(graderHistoryBlock(grader.history)); }

    container.appendChild(box);
  }

  function graderNumber(label, value, min, max, onChange) {
    var wrap = el('label', 'grader-field');
    wrap.appendChild(el('span', '', label));
    var input = document.createElement('input');
    input.type = 'number';
    input.min = String(min);
    input.max = String(max);
    input.value = String(value);
    input.className = 'grader-input';
    input.addEventListener('change', function () {
      var v = Number(input.value);
      if (isNaN(v)) { v = value; }
      v = Math.max(min, Math.min(max, v));
      input.value = String(v);
      onChange(v);
    });
    wrap.appendChild(input);
    return wrap;
  }

  function graderJobBlock(job) {
    var box = el('div', 'grader-job');

    var head = el('div', 'grader-job-head');
    head.appendChild(el('span', 'badge', job.status === 'running' ? '运行中'
      : ((job.stop_reason === 'reached' || job.stop_reason === 'fetched')
         ? '完成' : '已停止')));
    head.appendChild(el('span', 'hint',
      '目标 ' + (job.target_score === undefined ? '—' : job.target_score) +
      ' · 最高 ' + (job.best_score === null || job.best_score === undefined
        ? '—' : job.best_score) +
      ' · 轮次 ' + (job.attempts ? job.attempts.length : 0) +
      '/' + (job.max_attempts === undefined ? '—' : job.max_attempts)));
    box.appendChild(head);

    if (job.stop_reason) {
      var cls = job.stop_reason === 'reached' ? 'alert-ok'
        : (job.stop_reason === 'unavailable' ? 'alert-soft' : 'alert-warn');
      var stop = el('div', 'alert-box ' + cls);
      stop.appendChild(el('div', 'banner-title',
        GRADER_STOP_TEXT[job.stop_reason] || job.stop_reason));
      if (job.message) { stop.appendChild(el('p', 'res-message', job.message)); }
      if (job.stop_reason === 'unavailable') {
        stop.appendChild(el('p', 'hint',
          '批改网有些作业在截止前就有分（老师开了提前批改），也有些确实还没批完。' +
          '先点「只取分」看看详情页；确定这篇还能改，再用「直接改写重交」。'));
      }
      box.appendChild(stop);
    }

    /* 事件日志 */
    var events = job.events || [];
    if (events.length) {
      var det = el('details', 'grader-log');
      det.appendChild(el('summary', '', '运行日志（' + events.length + ' 条）'));
      var list = el('div', 'log-list');
      for (var i = Math.max(0, events.length - 120); i < events.length; i++) {
        var ev = events[i];
        var row = el('div', 'log-row');
        row.appendChild(el('span', 'log-time', String(ev.at || '').slice(11) || '—'));
        row.appendChild(el('span', 'log-kind', ev.kind || ''));
        row.appendChild(el('span', 'log-text', graderEventText(ev)));
        list.appendChild(row);
      }
      det.appendChild(list);
      box.appendChild(det);
    }

    /* 没拿到分数（超时 / 出错）：让用户决定「继续等待 X 秒」还是「手动获取」 */
    if (job.stop_reason === 'timeout' || job.stop_reason === 'error') {
      box.appendChild(graderRetryBlock(job));
    }

    /* 每轮结果 */
    var attempts = job.attempts || [];
    attempts.forEach(function (a) { box.appendChild(graderAttemptCard(a)); });
    return box;
  }

  /* 取不到分数时的补救面板：继续等待 N 秒 / 手动获取一次（都不重新提交作文） */
  function graderRetryBlock(job) {
    var attempts = job.attempts || [];
    var last = attempts.length ? attempts[attempts.length - 1] : {};
    var eid = last.eid || job.eid || '';

    var box = el('div', 'grader-retry');
    box.appendChild(el('p', 'hint',
      job.stop_reason === 'timeout'
        ? '批改还没出分（稿子已经提交，不用重交）。可以再等一段时间、手动获取一次，'
          + '或者确定可以继续改就直接改写重交。'
        : '这一轮没取到分数。可以先手动获取一次、继续等待，或直接改写重交。'));

    var row = el('div', 'grader-controls');
    var secs = 180;
    row.appendChild(graderNumber('继续等待（秒）', secs, 5, 1800,
      function (v) { secs = v; }));

    var waitBtn = el('button', 'btn btn-sm btn-warn', '继续等待');
    waitBtn.type = 'button';
    waitBtn.addEventListener('click', function () {
      graderWait(eid, secs, waitBtn);
    });
    var fetchBtn = el('button', 'btn btn-sm', '手动获取一次');
    fetchBtn.type = 'button';
    fetchBtn.addEventListener('click', function () { graderFetch(); });
    var forceBtn = el('button', 'btn btn-sm btn-danger', '直接改写重交');
    forceBtn.type = 'button';
    forceBtn.title = '不等分数，直接改写后重新提交（批改网有些作业允许提前批改/反复重交）';
    forceBtn.addEventListener('click', function () { graderForceResubmit(); });
    var actions = el('div', 'grader-actions');
    actions.appendChild(waitBtn);
    actions.appendChild(fetchBtn);
    actions.appendChild(forceBtn);
    box.appendChild(row);
    box.appendChild(actions);

    var out = el('div', 'grader-retry-out');
    out.id = 'grader-retry-out';
    box.appendChild(out);
    if (eid) { box.appendChild(el('p', 'hint', '稿件号：' + eid)); }
    return box;
  }

  /* 不等分数，直接按目标改写并重交（用户明确授权的写操作） */
  function graderForceResubmit() {
    var out = $('grader-retry-out');
    var say = function (text, ok) {
      if (!out) { return; }
      out.textContent = text;
      out.className = 'grader-retry-out' + (ok === true ? ' llm-msg-ok' : (ok === false ? ' llm-msg-err' : ''));
    };
    if (!window.confirm('不等分数，直接改写并重新提交？\n\n这会真的向批改网再交一篇（不可撤销）。')) {
      return;
    }
    say('已启动：不等分数，改写后重交…');
    graderApi('/api/essays/grade-loop', {
      platform: 'pigai', assignment_id: grader.rid,
      target_score: grader.target, max_attempts: grader.maxAttempts,
      analysis_timeout: grader.analysisTimeout, resubmit_without_score: true,
    }).then(function (job) {
      grader.busy = true; grader.error = ''; grader.job = job;
      graderRender();
      say('已开始重交流程，进度见下方。', true);
      if (job && job.job_id) { graderPoll(job.job_id); }
    }).catch(function (e) {
      say('启动失败：' + describeError(e, { platform: 'pigai' }), false);
    });
  }

  function graderWait(eid, seconds, btn) {
    var out = $('grader-retry-out');
    var say = function (text, ok) {
      if (!out) { return; }
      out.textContent = text;
      out.className = 'grader-retry-out' + (ok === true ? ' llm-msg-ok' : (ok === false ? ' llm-msg-err' : ''));
    };
    if (btn) { btn.disabled = true; btn.textContent = '等待中…'; }
    say('等待平台出分，最多 ' + seconds + ' 秒（期间不用管它）…');
    graderApi('/api/essays/wait', {
      platform: 'pigai', assignment_id: grader.rid, eid: eid, timeout: seconds,
    }).then(function (body) {
      if (btn) { btn.disabled = false; btn.textContent = '继续等待'; }
      if (body && body.timed_out) {
        say((body.message || '还是没出分') + ' —— 可以再等一次，或点「手动获取一次」。', false);
        return;
      }
      var score = body && body.score;
      if (body && body.graded && score !== undefined && score !== null) {
        say('出分了：' + score + ' 分'
          + (body.raw_state ? '（' + body.raw_state + '）' : '')
          + '。想看批注可以用「手动获取一次」，想继续提分就再开自动改写。', true);
      } else if (body && body.detail_error) {
        say('列表里有分数，但详情页读不到：' + body.detail_error, true);
      } else {
        say('拿到了结果但还没出分（' + ((body && body.raw_state) || '未知状态') + '）。', false);
      }
      /* 不在这里重绘历史：重绘会把上面这条结果提示一起清掉 */
    }).catch(function (e) {
      if (btn) { btn.disabled = false; btn.textContent = '继续等待'; }
      say('等待失败：' + describeError(e), false);
    });
  }

  function graderAttemptCard(a) {
    var card = el('details', 'grader-attempt');
    var scoreText = a.score_label
      ? (a.score_label + ' 分')
      : ((a.score === null || a.score === undefined) ? (a.raw_state || '未出分')
         : (a.score + ' 分'));
    var summary = (a.attempt === 0 ? '现有稿件（未重新提交）' : ('第 ' + a.attempt + ' 轮')) +
      ' · ' + scoreText +
      (a.word_count ? (' · ' + a.word_count + ' 词') : '');
    card.appendChild(el('summary', '', summary));

    if (a.dimensions && a.dimensions.length) {
      var dims = el('div', 'grader-dims');
      a.dimensions.forEach(function (d) {
        dims.appendChild(el('span', 'dim-chip',
          (d.name || '?') + ' ' + Math.round((d.ratio || 0) * 100) + '%'));
      });
      card.appendChild(dims);
    }
    if (a.overall) { card.appendChild(el('p', 'res-message', '总评：' + a.overall)); }
    if (a.feedback && a.feedback.length) {
      var ul = el('ul', 'grader-feedback');
      a.feedback.forEach(function (f) { ul.appendChild(el('li', '', f)); });
      card.appendChild(ul);
    }
    if (a.essay_text) {
      var body = el('pre', 'essay-text', a.essay_text);
      card.appendChild(body);
    }
    return card;
  }

  function graderHistoryBlock(history) {
    var box = el('div', 'grader-history');
    var items = history.attempts || [];
    box.appendChild(el('h4', '', '历史提交（' + items.length + ' 轮）'));
    if (!items.length) {
      box.appendChild(el('p', 'hint', '还没有记录。跑一次评分循环或「只取分」后会出现在这里。'));
      return box;
    }
    items.forEach(function (a) { box.appendChild(graderAttemptCard(a)); });
    return box;
  }

  function graderStart() {
    if (!grader.platform || !grader.rid) { return; }
    var msg = '这会真的反复提交作文（最多 ' + grader.maxAttempts + ' 轮），' +
      '会先读取已有稿件的分数：分够了就停、不会重交；只有低于 ' + grader.target +
      ' 分且拿到批注时，才改写后再提交。\n\n提交不可撤销，确定继续？';
    if (!window.confirm(msg)) { return; }

    grader.busy = true; grader.error = ''; grader.note = '';
    grader.job = null; grader.history = null;
    graderRender();

    graderApi('/api/essays/grade-loop', {
      platform: grader.platform,
      assignment_id: grader.rid,
      target_score: grader.target,
      max_attempts: grader.maxAttempts
    }).then(function (job) {
      grader.job = job;
      graderPoll(job.job_id);
    }).catch(function (err) {
      grader.busy = false;
      grader.error = describeError(err, { platform: grader.platform });
      graderRender();
    });
  }

  function graderPoll(jobId) {
    graderStopPolling();
    api('/api/essays/jobs/' + encodeURIComponent(jobId)).then(function (job) {
      grader.job = job;
      grader.busy = job.status === 'running';
      graderRender();
      if (job.status === 'running') {
        grader.timer = setTimeout(function () { graderPoll(jobId); }, 1000);
      } else {
        graderStopPolling();
        graderHistory();
      }
    }).catch(function (err) {
      graderStopPolling();
      grader.busy = false;
      grader.error = describeError(err, { platform: grader.platform });
      graderRender();
    });
  }

  function graderFetch() {
    if (!grader.platform || !grader.rid) { return; }
    grader.busy = true; grader.error = ''; grader.note = '正在向平台取分（不会提交任何东西）…';
    graderRender();
    graderApi('/api/essays/fetch', {
      platform: grader.platform, assignment_id: grader.rid, trigger_analysis: true
    }).then(function (snap) {
      grader.busy = false;
      grader.note = '';
      var reason = snap.analysis_error
        ? ('平台拒绝了分析请求（' + String(snap.analysis_error).slice(0, 120) + '）' +
           ' —— 这篇可能已经批过了；可以「直接改写重交」，或稍后再取。')
        : (snap.detail_error || snap.raw_state || '待分析');
      var attempt = Object.assign({ attempt: 0 }, snap);
      if (attempt.word_count === undefined) { delete attempt.word_count; }
      var job = {
        job_id: 'fetch', status: 'done', target_score: grader.target,
        best_score: snap.score, max_attempts: 1, events: [],
        attempts: [attempt],
        stop_reason: snap.graded
          ? ((snap.score !== undefined && snap.score !== null && snap.score >= grader.target)
             ? 'reached' : 'fetched')
          : 'unavailable',
        message: snap.graded
          ? ('当前分数 ' + snap.score +
             (snap.detail_error ? '（' + snap.detail_error + '）' : ''))
          : ('没取到分数：' + reason)
      };
      grader.job = job;
      graderRender();
    }).catch(function (err) {
      grader.busy = false; grader.note = '';
      grader.error = describeError(err, { platform: grader.platform });
      graderRender();
    });
  }

  function graderHistory() {
    if (!grader.platform || !grader.rid) { return; }
    graderApi('/api/essays/history', {
      platform: grader.platform, assignment_id: grader.rid
    }).then(function (data) {
      grader.history = data;
      graderRender();
    }).catch(function () { /* 历史失败不该打扰主流程 */ });
  }

  function graderRender() {
    var box = $('drawer-body');
    if (!box) { return; }
    var host = $('grader-host');
    if (!host) {
      host = el('div', 'grader-host');
      host.id = 'grader-host';
      box.parentNode.insertBefore(host, box.nextSibling);
    }
    clear(host);
    if (grader.platform === 'pigai' && grader.rid) { graderPanel(host); }
  }

  function renderDrawerFoot() {
    var d = state.drawer;
    var foot = $('drawer-foot');
    if (!foot) { return; }
    clear(foot);
    var kids = [];

    if (d.busy) {
      kids.push(el('span', 'hint', d.status || '请求中…'));
    } else if (d.mode === 'detail' && d.detail) {
      var reason = submitBlockReason(d.platform);
      if (reason) {
        kids.push(el('span', 'hint', reason + ' —— 「提交」不可用。'));
      } else {
        var b = el('button', 'btn btn-warn', '提交这个作业（两步）');
        b.type = 'button';
        b.id = 'drawer-submit-btn';
        b.title = '先干跑（confirm=false）看每题答案与置信度，确认后才真提交';
        b.addEventListener('click', switchToSubmit);
        kids.push(b);
      }
    } else if (d.mode === 'detail' && d.error) {
      var retry = el('button', 'btn', '重试');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        loadAssignmentDetail(d.platform, d.assignmentId);
      });
      kids.push(retry);
    } else if (d.mode === 'submit' && d.step === 'plan' && d.plan && !d.error) {
      var c = el('button', 'btn btn-danger', '确认真提交（不可撤销）');
      c.type = 'button';
      c.id = 'drawer-confirm-btn';
      if (d.planStats && d.planStats.human > 0) {
        c.title = '有 ' + d.planStats.human + ' 题标了「需人工确认」；仍要真提交吗？';
      } else {
        c.title = '会真的调用平台写接口，不可撤销';
      }
      c.addEventListener('click', confirmSubmit);
      kids.push(c);
      kids.push(el('span', 'hint', '会真的调用平台写接口，不可撤销。'));
    } else if (d.mode === 'submit' && d.step === 'plan' && d.error) {
      var again = el('button', 'btn', '重新干跑');
      again.type = 'button';
      again.addEventListener('click', function () {
        loadSubmitPlan(d.platform, d.assignmentId);
      });
      kids.push(again);
    } else if (d.mode === 'submit' && d.step === 'done') {
      kids.push(el('span', 'hint', '已提交（后端记录的提交结果见「运行日志 · 提交记录」）。'));
    }

    var close = el('button', 'btn btn-ghost btn-sm', '关闭');
    close.type = 'button';
    close.id = 'drawer-close-foot';
    close.addEventListener('click', closeDrawer);
    kids.push(close);

    fill(foot, kids);
    foot.hidden = false;
  }

  /* 详情抽屉 → 直接进入这个作业的两步提交（同一平台、同一任务号）。 */
  function switchToSubmit() {
    var d = state.drawer;
    var reason = submitBlockReason(d.platform);
    if (reason) { showToast(reason); return; }
    openDrawer('submit', d.platform, d.assignmentId, d.title);
    loadSubmitPlan(d.platform, d.assignmentId);
  }

  /* ---- 详情：POST /api/assignments/detail ---- */

  function openAssignmentDetail(plt, id, rowObj) {
    openDrawer('detail', plt, id, assignmentTitleOf(rowObj) || ('作业 ' + id));
    loadAssignmentDetail(plt, id);
  }

  function loadAssignmentDetail(plt, id) {
    var d = state.drawer;
    if (!d.open || d.mode !== 'detail') { return; }
    d.seq++;
    var seq = d.seq;
    d.busy = true;
    d.error = '';
    d.errorKind = '';
    d.notSupported = '';
    d.detail = null;
    d.status = '正在读取作业详情 POST /api/assignments/detail …（只读，会真的登录平台）';
    renderDrawer();

    postJson('/api/assignments/detail', { platform: plt, assignment_id: id })
      .then(function (body) {
        if (seq !== d.seq || !d.open) { return; }
        d.busy = false;
        d.status = '';
        d.detail = isObj(body) ? body : null;
        if (d.detail && d.detail.title) { d.title = String(d.detail.title); }
        renderDrawer();
      }).catch(function (e) {
        if (seq !== d.seq || !d.open) { return; }
        d.busy = false;
        d.status = '';
        d.detail = null;
        drawerFail(d, e, plt);
        renderDrawer();
      });
  }

  /* 抽屉里的失败统一处理：501 not_supported 属于「能力缺失」而不是「报错」，
     只走那一块说明（否则同一句后端原文会在红框和黄框里各出现一遍）。 */
  function drawerFail(d, e, plt) {
    d.errorKind = (e && e.kind) || '';
    if (d.errorKind === 'not_supported') {
      d.error = '';
      d.notSupported = ((e && e.error) || '').trim() ||
        (NOT_SUPPORTED_TEXT + '（501 not_supported）');
      markNoSubmit(plt, e);
      return;
    }
    d.error = describeError(e, { platform: plt });
  }

  /* ---- 两步提交：先干跑，再确认 ---- */

  function openAssignmentSubmit(plt, id, rowObj) {
    var reason = submitBlockReason(plt);
    if (reason && state.noSubmit[plt]) {
      /* 后端确实回过 501：把它的原文显眼地摆出来，不发第二次请求。 */
      drawerReset('submit', plt, id, assignmentTitleOf(rowObj) || ('作业 ' + id));
      state.drawer.notSupported = state.noSubmit[plt];
      renderDrawer();
      return;
    }
    if (reason) {
      showToast(platformLabel(plt) + '：' + reason + '（按钮已禁用）');
      renderAssignSubmitNote();
      return;
    }
    openDrawer('submit', plt, id, assignmentTitleOf(rowObj) || ('作业 ' + id));
    loadSubmitPlan(plt, id);
  }

  function loadSubmitPlan(plt, id) {
    var d = state.drawer;
    if (!d.open || d.mode !== 'submit') { return; }
    d.seq++;
    var seq = d.seq;
    d.busy = true;
    d.status = '第一步：干跑 POST /api/assignments/submit（confirm=false，只规划不提交）…';
    d.error = '';
    d.errorKind = '';
    d.notSupported = '';
    d.plan = null;
    d.planRows = null;
    d.planStats = null;
    d.step = '';
    d.result = null;
    renderDrawer();

    postJson('/api/assignments/submit',
      { platform: plt, assignment_id: id, confirm: false }).then(function (body) {
      if (seq !== d.seq || !d.open) { return; }
      d.busy = false;
      d.status = '';
      d.plan = isObj(body) ? body : null;
      d.planRows = planRowsOf(d.plan);
      d.planStats = d.planRows ? planStatsOf(d.planRows) : null;
      d.step = 'plan';
      renderDrawer();
      if (d.planRows && d.planRows.length) {
        showToast('干跑完成：' + d.planRows.length + ' 题，未向平台提交任何内容');
      }
    }).catch(function (e) {
      if (seq !== d.seq || !d.open) { return; }
      d.busy = false;
      d.status = '';
      drawerFail(d, e, plt);
      renderDrawer();
    });
  }

  function confirmSubmit() {
    var d = state.drawer;
    if (!d.open || d.mode !== 'submit' || d.step !== 'plan' || d.busy) { return; }
    /* 契约 §5.4：写操作必须二次确认，且讲清楚「会真的提交、不可撤销」。 */
    if (!window.confirm(SUBMIT_CONFIRM)) {
      showToast('已取消真提交：没有向平台发送任何写请求。');
      return;
    }
    var plt = d.platform;
    var id = d.assignmentId;
    d.seq++;
    var seq = d.seq;
    d.busy = true;
    d.status = '第二步：真提交 POST /api/assignments/submit（confirm=true，会真的调平台写接口）…';
    d.error = '';
    d.errorKind = '';
    renderDrawer();

    postJson('/api/assignments/submit',
      { platform: plt, assignment_id: id, confirm: true }).then(function (body) {
      if (seq !== d.seq || !d.open) { return; }
      d.busy = false;
      d.status = '';
      d.step = 'done';
      d.result = isObj(body) ? body : null;
      renderDrawer();
      showToast('已发送真提交（' + platformLabel(plt) + ' · ' + id + '）');
      if (state.page === 'logs') { loadRuns(); loadSubmissions(); }
    }).catch(function (e) {
      if (seq !== d.seq || !d.open) { return; }
      d.busy = false;
      d.status = '';
      drawerFail(d, e, plt);
      renderDrawer();
    });
  }

  /* ---- 干跑结果的容错解析 ----
     契约只说 confirm=false「返回每题打算怎么答 + 置信度」，没定信封形状，
     所以按已知结构层层找（顶层数组 → 信封 rows → sections 里的 table 段），
     只有「像一道题」的行才算计划（有答案 / 置信度 / needs_human 之一）；
     认不出来就退回通用 kind 渲染，绝不白屏、也不假装读懂了。 */
  var PLAN_LIST_KEYS = ['questions', 'plan', 'answers', 'items', 'records', 'rows',
                        'details', 'results'];
  var PLAN_ANSWER_KEYS = ['answer', 'planned_answer', 'plannedAnswer', 'value', 'content', 'text'];
  var PLAN_CONF_KEYS = ['confidence', 'conf', 'confidence_score', 'confidenceScore', 'score'];
  var PLAN_HUMAN_KEYS = ['needs_human', 'needsHuman', 'need_human', 'human', 'needs_review'];
  var PLAN_STEM_KEYS = ['stem', 'question', 'question_text', 'title', 'prompt', 'topic'];
  var PLAN_NO_KEYS = ['index', 'no', 'seq', 'number', 'qid', 'question_id', 'id'];
  var PLAN_REASON_KEYS = ['reason', 'note', 'why', 'explain', 'remark', 'rationale'];

  function pickValue(obj, keys) {
    if (!isObj(obj)) { return { found: false, key: '', value: null }; }
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (!has(obj, k)) { continue; }
      var v = obj[k];
      if (v === null || v === undefined) { continue; }
      if (typeof v === 'string' && !v.trim()) { continue; }
      if (typeof v === 'object') { continue; }   // 嵌套对象不当标量用
      return { found: true, key: k, value: v };
    }
    return { found: false, key: '', value: null };
  }

  function pickFlag(obj, keys) {
    var p = pickValue(obj, keys);
    if (!p.found) { return { found: false, key: '', value: false }; }
    var v = p.value;
    var on = (v === true || v === 1 || v === '1' || v === 'true' || v === 'yes' ||
              v === 'needs_human' || v === 'human' || v === 'needs_review');
    return { found: true, key: p.key, value: on };
  }

  function planRowView(rowObj) {
    return {
      no: pickValue(rowObj, PLAN_NO_KEYS),
      stem: pickValue(rowObj, PLAN_STEM_KEYS),
      answer: pickValue(rowObj, PLAN_ANSWER_KEYS),
      confidence: pickValue(rowObj, PLAN_CONF_KEYS),
      human: pickFlag(rowObj, PLAN_HUMAN_KEYS),
      reason: pickValue(rowObj, PLAN_REASON_KEYS)
    };
  }

  function planRowsOf(body) {
    if (!isObj(body)) { return null; }
    var cands = [];
    var i;
    for (i = 0; i < PLAN_LIST_KEYS.length; i++) {
      var v = body[PLAN_LIST_KEYS[i]];
      if (Array.isArray(v) && v.length && isObj(v[0])) { cands.push(v); }
    }
    var sections = Array.isArray(body.sections) ? body.sections : [];
    for (i = 0; i < sections.length; i++) {
      var sec = sections[i];
      if (isObj(sec) && Array.isArray(sec.rows) && sec.rows.length && isObj(sec.rows[0])) {
        cands.push(sec.rows);
      }
    }
    for (i = 0; i < cands.length; i++) {
      var hit = cands[i].filter(function (r) {
        if (!isObj(r)) { return false; }
        return pickValue(r, PLAN_ANSWER_KEYS).found ||
               pickValue(r, PLAN_CONF_KEYS).found ||
               pickFlag(r, PLAN_HUMAN_KEYS).found;
      });
      if (hit.length) { return cands[i]; }
    }
    return null;
  }

  function confidenceThreshold() {
    var o = state.overview, s = state.settings;
    if (isObj(o) && isNum(o.min_confidence)) { return o.min_confidence; }
    if (isObj(s) && isNum(s.min_confidence)) { return s.min_confidence; }
    return null;
  }

  function numOrNull(v) {
    if (isNum(v)) { return v; }
    if (typeof v === 'string' && v.trim() && isFinite(parseFloat(v))) { return parseFloat(v); }
    return null;
  }

  function fmtConf(v) {
    if (v === null || v === undefined || v === '') { return '—'; }
    if (isNum(v)) { return String(Math.round(v * 1000) / 1000); }
    return String(v);
  }

  function planStatsOf(rows) {
    var threshold = confidenceThreshold();
    var human = 0, low = 0, minConf = null;
    for (var i = 0; i < rows.length; i++) {
      var v = planRowView(rows[i]);
      if (v.human.found && v.human.value) { human++; }
      var c = numOrNull(v.confidence.value);
      if (c !== null) {
        if (minConf === null || c < minConf) { minConf = c; }
        if (threshold !== null && c < threshold) { low++; }
      }
    }
    return { total: rows.length, human: human, low: low, min: minConf, threshold: threshold };
  }

  function planStat(label, value, cls) {
    var w = el('span', 'stat');
    w.appendChild(el('span', 'stat-label', label));
    w.appendChild(el('span', 'stat-value mono' + (cls ? ' ' + cls : ''), value));
    return w;
  }

  /* 干跑计划表：每题的计划答案 + 置信度，需人工 / 低于阈值显眼标出。 */
  function planBlock(rows, stats) {
    var s = stats || planStatsOf(rows);
    var box = el('div', 'plan-block');
    var counts = el('div', 'stat-row plan-counts');
    counts.appendChild(planStat('题目', fmtNum(s.total)));
    counts.appendChild(planStat('需人工确认', fmtNum(s.human), s.human ? 'conf-low' : ''));
    counts.appendChild(planStat('低于阈值', fmtNum(s.low), s.low ? 'conf-low' : ''));
    counts.appendChild(planStat('最低置信度', s.min === null ? '—' : fmtConf(s.min),
      (s.min !== null && s.threshold !== null && s.min < s.threshold) ? 'conf-low' : 'conf-ok'));
    counts.appendChild(planStat('阈值 min_confidence',
      s.threshold === null ? '—' : fmtConf(s.threshold)));
    box.appendChild(counts);
    box.appendChild(el('p', 'hint',
      'needs_human = true 的题标「需人工确认」；置信度低于 min_confidence 的题标「低于阈值」——' +
      '这两类后端即使 confirm=true 也会拦下（400）。'));

    var table = el('table');
    var thead = el('thead');
    var hr = el('tr');
    var heads = ['题号', '题目', '计划答案', '置信度', '标记'];
    for (var h = 0; h < heads.length; h++) { hr.appendChild(el('th', '', heads[h])); }
    thead.appendChild(hr);
    table.appendChild(thead);

    var tbody = el('tbody');
    for (var i = 0; i < rows.length; i++) {
      var v = planRowView(rows[i]);
      var no = v.no.found ? String(v.no.value) : String(i + 1);
      var stem = v.stem.found ? truncate(String(v.stem.value), 140) : '—';
      var ans = v.answer.found ? truncate(String(v.answer.value), 240) : '—';
      var cnum = numOrNull(v.confidence.value);
      var cText = v.confidence.found ? fmtConf(v.confidence.value) : '—';
      var lowConf = (cnum !== null && s.threshold !== null && cnum < s.threshold);
      var flags = [];
      if (v.human.found && v.human.value) { flags.push(badge('需人工确认', 'badge-warn')); }
      if (lowConf) { flags.push(badge('低于阈值', 'badge-fail')); }
      var flagCell = el('td', 'flag');
      if (flags.length) {
        for (var f = 0; f < flags.length; f++) { flagCell.appendChild(flags[f]); }
      } else if (v.reason.found) {
        flagCell.appendChild(el('span', 'hint', truncate(String(v.reason.value), 90)));
      } else {
        flagCell.appendChild(el('span', 'hint', '—'));
      }
      var confCls = 'mono' + (lowConf ? ' conf-low' : (cnum === null ? '' : ' conf-ok'));
      tbody.appendChild(row([
        cell(no, 'mono'),
        cell(stem, 'wrap'),
        cell(ans, 'wrap mono'),
        el('td', confCls, cText),
        flagCell
      ]));
    }
    table.appendChild(tbody);
    var wrap = el('div', 'table-wrap');
    wrap.appendChild(table);
    box.appendChild(wrap);
    return box;
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
    state.actions = { platform: null, list: null, active: null, result: null, busy: false, prefill: null };
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
    renderEnvelope(result, state.actions.result, {
      related: relatedActions(list, state.actions.result),
      onRelated: pickRelated
    });
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
        result: keep ? state.actions.result : null, busy: false,
        prefill: keep ? state.actions.prefill : null
      };
      if (!keep) { clearToolsError(); }
      renderTools();
      clearProblem('actions');
    }).catch(function (e) {
      if (plt !== state.platform) { return; }
      state.actions = { platform: plt, list: [], active: null, result: null, busy: false, prefill: null };
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
    if (act.mutates) {
      head.appendChild(badge('写操作', 'badge-rw'));
    } else {
      head.appendChild(badge('只读', 'badge-ro'));
    }
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
        /* type=text 的长参数（答案单 JSON 等）用多行输入框，单行框放不下也看不清 */
        var longText = spec.type === 'text';
        var input = el(longText ? 'textarea' : 'input', 'mono');
        input.id = 'ap-' + spec.name;
        input.name = spec.name;
        if (longText) {
          input.rows = 7;
          input.spellcheck = false;
        } else {
          input.autocomplete = 'off';
          if (spec.type === 'int') {
            input.type = 'number';
            input.step = '1';
            input.inputMode = 'numeric';
          } else {
            input.type = 'text';
          }
        }
        if (spec.required) { input.required = true; }
        /* 「查看详情」带过来的预填值（如 finished 行 → essay 的 rid/eid）。 */
        if (state.actions.prefill && state.actions.prefill[spec.name] !== undefined) {
          input.value = state.actions.prefill[spec.name];
        }
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
      /* 结果里若还有能对上的详情动作（如 finished → essay），保留「查看详情」入口。 */
      renderEnvelope($('action-result'), state.actions.result, {
        related: relatedActions(state.actions.list, state.actions.result),
        onRelated: pickRelated
      });
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
    if (!o.noTitle) {
      head.appendChild(el('div', 'res-title',
        env.title || env.label || env.action || '结果'));
    }
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
      box.appendChild(tableBlock(env.columns, env.rows, o.emptyText || '该动作返回 0 行。', o));
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
      box.appendChild(tableBlock(env.columns, env.rows, o.emptyText || '该动作返回 0 行。', o));
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

  /* 列表渲染。opts.related / opts.onRelated 可选：把「这一行的数据能填满其必填参数」
     的详情动作做成一列按钮（如批改网 finished 行 → essay 详情），全程按动作声明推导。
     opts.rowActions(rowObj, i) 可选：返回该行的操作按钮（作业页的「详情 / 提交」）。
     opts.onRowDblClick(rowObj, ev) 可选：整行双击（作业页 → 作业详情），
     行会带上 opts.rowTitle 的提示文字，双击时点在按钮/链接上不算。 */
  function tableBlock(columns, rows, emptyText, opts) {
    var o = opts || {};
    var related = o.related || [];
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
    if (related.length || o.rowActions) {
      headRow.appendChild(el('th', 'ctr', o.rowActions ? (o.actionsLabel || '操作') : '详情'));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el('tbody');
    for (var r = 0; r < list.length; r++) {
      var rowObj = list[r];
      var cells = [];
      for (var c = 0; c < cols.length; c++) {
        var v = rowObj[cols[c].key];
        var text = (v === null || v === undefined || v === '') ? '—'
          : (isObj(v) || Array.isArray(v) ? jsonText(v, 160) : String(v));
        cells.push(cell(text, cols[c].mono ? 'mono' : 'wrap'));
      }
      if (related.length) { cells.push(relatedCell(rowObj, related, o.onRelated)); }
      if (o.rowActions) { cells.push(actionCell(rowObj, r, o.rowActions)); }
      var tr = row(cells);
      if (o.rowClass) { tr.className = o.rowClass; }
      if (o.onRowDblClick) {
        tr.className = (tr.className ? tr.className + ' ' : '') + 'row-click';
        tr.title = o.rowTitle || '双击查看详情';
        (function (obj) {
          tr.addEventListener('dblclick', function (ev) { o.onRowDblClick(obj, ev); });
        })(rowObj);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  /* 行内操作列：回调返回按钮数组；没有可做的就显示「—」，不留空按钮。 */
  function actionCell(rowObj, index, build) {
    var c = el('td', 'ctr nowrap');
    var nodes = null;
    try { nodes = build(rowObj, index); } catch (e) { nodes = null; }
    if (!nodes || !nodes.length) {
      c.appendChild(el('span', 'hint', '—'));
      return c;
    }
    for (var i = 0; i < nodes.length; i++) { c.appendChild(nodes[i]); }
    return c;
  }

  function requiredParamNames(act) {
    var out = [];
    var ps = (act && act.params) || [];
    for (var i = 0; i < ps.length; i++) {
      if (ps[i] && ps[i].required) { out.push(ps[i].name); }
    }
    return out;
  }

  function rowHasParams(rowObj, names) {
    for (var i = 0; i < names.length; i++) {
      var v = rowObj[names[i]];
      if (v === null || v === undefined || v === '') { return false; }
    }
    return true;
  }

  function relatedCell(rowObj, related, onRelated) {
    var c = el('td', 'ctr nowrap');
    for (var i = 0; i < related.length; i++) {
      (function (act) {
        var names = requiredParamNames(act);
        if (!names.length || !rowHasParams(rowObj, names)) { return; }
        var b = el('button', 'btn btn-sm', '查看' + (act.label || act.name));
        b.type = 'button';
        b.title = (act.description || act.label || act.name) +
          '（参数 ' + names.join(' / ') + ' 取自本行）';
        b.addEventListener('click', function () { onRelated(rowObj, act); });
        c.appendChild(b);
      })(related[i]);
    }
    return c;
  }

  /* 从动作清单里挑「第一行的数据能填满其必填参数」的平台专属动作。 */
  function relatedActions(list, env) {
    var out = [];
    if (!env || env.kind !== 'table') { return out; }
    var rows = (env.rows || []).filter(isObj);
    if (!rows.length) { return out; }
    var sample = rows[0];
    var acts = (list || []).filter(function (a) {
      return a && !a.builtin && (a.params || []).length;
    });
    for (var i = 0; i < acts.length; i++) {
      var names = requiredParamNames(acts[i]);
      if (names.length && rowHasParams(sample, names)) { out.push(acts[i]); }
    }
    return out;
  }

  /* 点了「查看详情」：把该行的值预填进动作参数表单，不自动执行（让用户先确认参数）。 */
  function pickRelated(rowObj, act) {
    var prefill = {};
    var names = requiredParamNames(act);
    for (var i = 0; i < names.length; i++) {
      var v = rowObj[names[i]];
      if (v !== null && v !== undefined && v !== '') { prefill[names[i]] = String(v); }
    }
    state.actions.active = act;
    state.actions.prefill = prefill;
    state.actions.result = null;
    clearToolsError();
    renderTools();
    var form = $('action-form');
    var btn = form ? form.querySelector('button[type="submit"]') : null;
    if (btn) { btn.focus(); }
    showToast('已按这一行填好「' + (act.label || act.name) + '」的参数：' +
      names.join(' / ') + '；点「执行」看详细内容');
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
    return api(withOwner('/api/jobs')).then(function (body) {
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

  /* 归属列：超管看「全部用户」时，每行显示这条是谁的（响应每行带 owner_id）。 */
  function ownerCell(value) {
    var c = cell(ownerLabel(value), 'mono');
    if (value !== null && value !== undefined && value !== '') {
      c.title = 'owner_id = ' + value + '（' + ownerLabel(value) + '）';
    }
    c.hidden = !showOwnerColumn();
    return c;
  }

  function syncOwnerHeaders() {
    setColumnVisible('runs-table', 'owner', showOwnerColumn());
    setColumnVisible('subs-table', 'owner', showOwnerColumn());
    setColumnVisible('jobs-table', 'owner', showOwnerColumn());
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
        ownerCell(job.owner_id),
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
    syncOwnerHeaders();
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
        ownerCell(r.owner_id),
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
    syncOwnerHeaders();
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
        ownerCell(s.owner_id),
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
    syncOwnerHeaders();
    fillTable('subs-table', 'subs-body', 'subs-empty', rows,
      '还没有提交记录。干跑不会产生提交，只有真提交（live=true）才会有。');
  }

  /* ---- 事件流 ---- */

  function loadEvents() {
    if (state.eventsOff) { return Promise.resolve(); }
    return api(withOwner('/api/events?limit=' + EVENT_LIMIT)).then(function (body) {
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
    return api(withOwner('/api/statistics')).then(function (body) {
      state.statistics = isObj(body) ? body : {};
      renderStatistics();
      renderStatBank();
      clearProblem('statistics');
    }).catch(function (e) {
      setProblem('statistics', '统计对比加载失败：' + describeError(e), 'error');
    });
  }

  /* 统计页的趋势刻意取「全部平台」口径：它本身就是横向视图（但仍受超管归属筛选影响）。 */
  function loadStatRuns() {
    return api(withOwner('/api/runs?limit=' + LIST_LIMIT)).then(function (body) {
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

  /* ==================================================================
     账号页（上）：平台多账号（契约 §1.5）
       GET  /api/accounts?platform=         自己的账号列表（超管可加 owner_id）
       POST /api/accounts                   {platform, label, username, password, login?}
       POST /api/accounts/<id>/activate     设为该平台的当前号 = 切 cookie
       POST /api/accounts/<id>/login        {password} 会话过期时重新登录
       POST /api/accounts/<id>/update       {label?, username?}
       POST /api/accounts/<id>/delete
     每个账号一行 <details>：摘要 = 标签 / 用户名 / 当前号 / 登录状态，
     展开 = 创建时间、上次登录时间与结果、所属平台、操作按钮（用户要求的形式）。
     ================================================================== */

  function accountsUrl() {
    var url = '/api/accounts';
    if (state.platform) { url += '?platform=' + encodeURIComponent(state.platform); }
    return withOwner(url);
  }

  function accountName(a) {
    if (!a) { return '—'; }
    return String(a.label || a.display || a.username || ('#' + a.id));
  }

  function accountLoginBadge(a) {
    if (a.last_login_ok === true) { return badge('会话有效', 'badge-ok'); }
    if (a.last_login_ok === false) { return badge('会话失效', 'badge-fail'); }
    return badge('还没登录', 'badge-idle');
  }

  function setAcctError(id, message) {
    if (message) { state.acct.err[id] = message; } else { delete state.acct.err[id]; }
  }

  function loadAccounts() {
    var seq = ++state.acct.seq;
    state.acct.busy = true;
    renderAccounts();
    return api(accountsUrl()).then(function (body) {
      if (seq !== state.acct.seq) { return; }
      state.acct.list = extractList(body, ['accounts', 'items', 'list']);
      state.acct.busy = false;
      state.acct.error = '';
      renderAccounts();
      clearProblem('accounts');
    }).catch(function (e) {
      if (seq !== state.acct.seq) { return; }
      state.acct.busy = false;
      state.acct.list = null;
      state.acct.error = '账号列表加载失败：' + describeError(e, { platform: state.platform });
      renderAccounts();
    });
  }

  function renderAccounts() {
    var groups = $('account-groups');
    var empty = $('account-empty');
    var hint = $('account-hint');
    if (!groups || !empty) { return; }

    var ownerNote = isSuperadmin()
      ? (state.ownerId === null ? '全部用户' : ('只看 ' + ownerLabel(state.ownerId)))
      : '仅自己';
    if (hint) {
      hint.textContent = (state.platform ? ('平台 ' + platformLabel(state.platform)) : '全部平台') +
        ' · 归属：' + ownerNote +
        (state.acct.busy ? ' · GET /api/accounts 加载中…' : ' · 来自 GET /api/accounts');
    }
    setBoxError('account-error', state.acct.error);
    renderNeedRelogin();

    if (state.acct.list === null) {
      groups.hidden = true;
      clear(groups);
      /* 空状态也要有人话：加载中 / 加载失败 / 还没进过这一页，分三种说法。 */
      empty.hidden = false;
      empty.textContent = state.acct.busy
        ? '正在读取账号列表 GET /api/accounts …'
        : (state.acct.error
            ? '账号列表没取到，点「刷新」重试。'
            : '还没取账号数据：进入本页会自动加载，或点上面的「刷新」。');
      return;
    }
    var list = state.acct.list;
    if (!list.length) {
      groups.hidden = true;
      clear(groups);
      empty.hidden = false;
      empty.textContent = state.platform
        ? ('平台「' + platformLabel(state.platform) +
           '」下还没有账号。用下面的「新增账号」加一个（平台 + 标签 + 用户名 + 密码）。')
        : '还没有任何平台账号。用下面的「新增账号」加一个（平台 + 标签 + 用户名 + 密码）。';
      return;
    }
    empty.hidden = true;
    groups.hidden = false;
    fill(groups, accountGroups(list));
  }

  /* 按平台分组；平台顺序跟随左侧平台清单，未知平台排在后面。 */
  function accountGroups(list) {
    var order = [];
    var byPlat = {};
    for (var i = 0; i < list.length; i++) {
      var a = isObj(list[i]) ? list[i] : {};
      var p = (a.platform === undefined || a.platform === null) ? '—' : String(a.platform);
      if (!byPlat[p]) { byPlat[p] = []; order.push(p); }
      byPlat[p].push(a);
    }
    order.sort(function (x, y) {
      var px = platformByName(x), py = platformByName(y);
      var ix = px ? state.platforms.indexOf(px) : 999;
      var iy = py ? state.platforms.indexOf(py) : 999;
      if (ix !== iy) { return ix - iy; }
      return x < y ? -1 : (x > y ? 1 : 0);
    });
    var out = [];
    for (var j = 0; j < order.length; j++) {
      out.push(accountGroup(order[j], byPlat[order[j]]));
    }
    return out;
  }

  function accountGroup(platform, list) {
    var box = el('section', 'acct-group');
    var head = el('div', 'acct-group-head');
    head.appendChild(el('b', '', platform === '—' ? '未标明平台' : platformLabel(platform)));
    head.appendChild(el('span', 'mono hint', platform));
    var active = null, bad = 0;
    for (var i = 0; i < list.length; i++) {
      if (list[i].is_active === true) { active = list[i]; }
      if (list[i].last_login_ok === false) { bad++; }
    }
    head.appendChild(el('span', 'hint', list.length + ' 个账号' +
      (active ? (' · 当前号 ' + accountName(active)) : ' · 未设当前号（新号会默认成为当前号）')));
    if (bad) { head.appendChild(badge(bad + ' 个会话失效', 'badge-fail')); }
    box.appendChild(head);

    var wrap = el('div', 'acct-list');
    for (var k = 0; k < list.length; k++) { wrap.appendChild(accountDetails(list[k])); }
    box.appendChild(wrap);
    return box;
  }

  /* 一个账号 = 一个 <details>：摘要一行，展开是详情 + 操作。 */
  function accountDetails(a) {
    var id = String(a.id);
    var d = el('details', 'acct');
    d.open = !!state.acct.open[id];   // 重绘后保留展开状态
    d.addEventListener('toggle', function () { state.acct.open[id] = d.open; });

    var sum = el('summary', 'acct-sum');
    sum.appendChild(el('span', 'acct-label', accountName(a)));
    sum.appendChild(el('span', 'acct-user mono', a.username ? String(a.username) : '—'));
    if (a.is_active === true) { sum.appendChild(badge('当前号', 'badge-run')); }
    sum.appendChild(accountLoginBadge(a));
    if (isSuperadmin() && state.ownerId === null && a.owner_id !== undefined && a.owner_id !== null) {
      sum.appendChild(el('span', 'acct-owner hint', '归属 ' + ownerLabel(a.owner_id)));
    }
    sum.appendChild(el('span', 'acct-id mono', '#' + id));
    sum.title = '点开看详细信息（创建时间 / 上次登录 / 操作）';
    d.appendChild(sum);

    var body = el('div', 'acct-body');
    body.appendChild(accountFields(a));
    body.appendChild(accountPanels(a));
    var perr = state.acct.err[id];
    if (perr) {
      body.appendChild(el('div', 'err-box', perr));
    }
    d.appendChild(body);
    return d;
  }

  function accountFields(a) {
    var dl = el('dl', 'kv-grid acct-kv');
    var pairs = [
      ['所属平台', (a.platform === undefined || a.platform === null ? '—' : String(a.platform)) +
        '（' + platformLabel(a.platform) + '）'],
      ['账号 id', '#' + String(a.id)],
      ['归属 owner_id', isSuperadmin()
        ? (dash(a.owner_id) + '（' + ownerLabel(a.owner_id) + '）')
        : dash(a.owner_id)],
      ['是否当前号', a.is_active === true ? '是 —— 该平台当前用它' : '否'],
      ['创建时间', fmtTime(a.created_at)],
      ['上次登录时间', fmtTime(a.last_login_at)],
      ['上次登录结果', a.last_login_ok === true ? '成功（会话有效）'
        : (a.last_login_ok === false ? '失败 / 会话已失效，需要重新登录' : '还没登录过')],
      ['上次登录说明', a.last_login_note]
    ];
    for (var i = 0; i < pairs.length; i++) {
      var wrap = el('div');
      wrap.appendChild(el('dt', '', pairs[i][0]));
      wrap.appendChild(el('dd', monoish(pairs[i][1]) ? 'mono' : '', dash(pairs[i][1])));
      dl.appendChild(wrap);
    }
    return dl;
  }

  function accountPanels(a) {
    var id = String(a.id);
    var busy = state.acct.actId !== null && String(state.acct.actId) === id;
    var wrap = el('div', 'acct-actions');

    var act = el('button', 'btn btn-sm', a.is_active === true ? '已是当前号' : '设为当前号');
    act.type = 'button';
    act.disabled = a.is_active === true || busy;
    act.title = 'POST /api/accounts/' + id + '/activate：切换该平台的当前号（切 cookie）';
    act.addEventListener('click', function () { activateAccount(a); });
    wrap.appendChild(act);

    var relog = el('button', 'btn btn-sm' + (a.last_login_ok === false ? ' btn-warn' : ''), '重新登录');
    relog.type = 'button';
    relog.disabled = busy;
    relog.title = 'POST /api/accounts/' + id + '/login：会话过期时用，密码不落盘';
    relog.addEventListener('click', function () { toggleAcctPanel(a, 'relogin'); });
    wrap.appendChild(relog);

    var ren = el('button', 'btn btn-sm', '改标签 / 用户名');
    ren.type = 'button';
    ren.disabled = busy;
    ren.title = 'POST /api/accounts/' + id + '/update：只改本地记录，不动平台账号';
    ren.addEventListener('click', function () { toggleAcctPanel(a, 'rename'); });
    wrap.appendChild(ren);

    var del = el('button', 'btn btn-sm btn-danger', '删除');
    del.type = 'button';
    del.disabled = busy;
    del.title = 'POST /api/accounts/' + id + '/delete：只删本机记录与 cookie';
    del.addEventListener('click', function () { deleteAccount(a); });
    wrap.appendChild(del);

    if (busy) { wrap.appendChild(el('span', 'field-hint', '正在执行…')); }

    var panel = state.acct.panel[id];
    if (panel === 'relogin') { wrap.appendChild(reloginForm(a)); }
    if (panel === 'rename') { wrap.appendChild(renameForm(a)); }
    return wrap;
  }

  function toggleAcctPanel(a, which) {
    var id = String(a.id);
    state.acct.panel[id] = state.acct.panel[id] === which ? null : which;
    renderAccounts();
    if (which === 'relogin') {
      var input = $('relogin-pass-' + id);
      if (input) { input.focus(); }
    }
  }

  function focusEl(node) { if (node) { node.focus(); } }

  function reloginForm(a) {
    var id = String(a.id);
    var f = el('div', 'acct-form');
    f.appendChild(el('span', 'field-label',
      '重新登录「' + accountName(a) + '」：输入平台密码（只在登录时用，不落盘、不回显）'));
    var p = el('input', 'mono acct-input');
    p.type = 'password';
    p.id = 'relogin-pass-' + id;
    p.autocomplete = 'new-password';
    p.placeholder = '平台密码';
    p.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); reloginAccount(a, p); }
    });
    var go = el('button', 'btn btn-sm btn-run', '登录');
    go.type = 'button';
    go.addEventListener('click', function () { reloginAccount(a, p); });
    var cancel = el('button', 'btn btn-sm btn-ghost', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', function () {
      state.acct.panel[id] = null;
      renderAccounts();
    });
    f.appendChild(p);
    f.appendChild(go);
    f.appendChild(cancel);
    if (a.last_login_ok === false) {
      f.appendChild(el('span', 'field-hint hint-warn',
        '这个号的会话已失效：重新登录成功后，当前号的数据会重新拉取。'));
    }
    return f;
  }

  function renameForm(a) {
    var id = String(a.id);
    var f = el('div', 'acct-form');
    f.appendChild(el('span', 'field-label', '改本地标签 / 用户名（不动平台账号）'));
    var label = el('input', 'acct-input');
    label.type = 'text';
    label.id = 'rename-label-' + id;
    label.placeholder = '标签 label';
    label.value = a.label ? String(a.label) : '';
    var user = el('input', 'mono acct-input');
    user.type = 'text';
    user.id = 'rename-user-' + id;
    user.placeholder = '平台用户名';
    user.value = a.username ? String(a.username) : '';
    user.autocomplete = 'off';
    var save = el('button', 'btn btn-sm btn-run', '保存');
    save.type = 'button';
    save.addEventListener('click', function () { renameAccount(a, label, user); });
    var cancel = el('button', 'btn btn-sm btn-ghost', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', function () {
      state.acct.panel[id] = null;
      renderAccounts();
    });
    f.appendChild(label);
    f.appendChild(user);
    f.appendChild(save);
    f.appendChild(cancel);
    return f;
  }

  /* 账号操作统一收口：成功回 body，失败把中文错误写进该账号的折叠区并返回 null。 */
  function accountOp(a, path, payload, okText) {
    var id = String(a.id);
    state.acct.actId = id;
    setAcctError(id, '');
    renderAccounts();
    return postJson(path, payload).then(function (body) {
      state.acct.actId = null;
      state.acct.panel[id] = null;
      showToast(okText);
      /* 成功响应体可能是 null / 空：这里统一回一个非 null 值给调用方判断成败。 */
      return (body === undefined || body === null) ? {} : body;
    }).catch(function (e) {
      state.acct.actId = null;
      setAcctError(id, describeError(e, { platform: a.platform }));
      renderAccounts();
      return null;
    });
  }

  function activateAccount(a) {
    if (a.is_active === true) { return; }
    accountOp(a, '/api/accounts/' + encodeURIComponent(a.id) + '/activate', {},
      '已把「' + accountName(a) + '」设为 ' + (a.platform || '') + ' 的当前号').then(function (ok) {
      if (ok !== null) { reloadAfterAccountChange(a.platform); }
    });
  }

  function reloginAccount(a, input) {
    var id = String(a.id);
    var pw = String(input && input.value ? input.value : '');
    if (!pw) {
      setAcctError(id, '请输入该账号的平台密码（后端不落盘，必须重新输一次）。');
      renderAccounts();
      focusEl($('relogin-pass-' + id));
      return;
    }
    state.acct.actId = id;
    setAcctError(id, '');
    renderAccounts();
    var again = $('relogin-pass-' + id);
    if (again) { again.value = pw; again.focus(); }
    postJson('/api/accounts/' + encodeURIComponent(a.id) + '/login', { password: pw })
      .then(function () {
        state.acct.actId = null;
        state.acct.panel[id] = null;
        showToast('「' + accountName(a) + '」重新登录成功，会话有效');
        reloadAfterAccountChange(a.platform);
      }).catch(function (e) {
        state.acct.actId = null;
        setAcctError(id, describeError(e, { platform: a.platform }));
        renderAccounts();
        focusEl($('relogin-pass-' + id));
      });
  }

  function renameAccount(a, labelNode, userNode) {
    var id = String(a.id);
    var label = String(labelNode.value || '').trim();
    var username = String(userNode.value || '').trim();
    if (!username) {
      setAcctError(id, '平台用户名不能为空。');
      renderAccounts();
      return;
    }
    accountOp(a, '/api/accounts/' + encodeURIComponent(a.id) + '/update',
      { label: label, username: username },
      '已更新账号记录：' + (label || username)).then(function (ok) {
      if (ok !== null) { loadAccounts(); }
    });
  }

  function deleteAccount(a) {
    var what = accountName(a) + '（' + (a.platform || '') + ' / ' + (a.username || '') + '）';
    if (!window.confirm('删除账号「' + what + '」？\n\n只删掉本机这个号的记录与 cookie，' +
                        '不会动平台上的真实账号，不可撤销。')) { return; }
    accountOp(a, '/api/accounts/' + encodeURIComponent(a.id) + '/delete', {},
      '已删除账号记录「' + accountName(a) + '」').then(function (ok) {
      if (ok === null) { return; }
      delete state.acct.open[String(a.id)];
      delete state.acct.panel[String(a.id)];
      delete state.acct.err[String(a.id)];
      loadAccounts();
    });
  }

  /* 会话失效的显眼提示 + 一键「重新登录」入口（用户要求）。 */
  function renderNeedRelogin() {
    var box = $('account-needrelogin');
    if (!box) { return; }
    var list = state.acct.list || [];
    var bad = [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].last_login_ok === false) { bad.push(list[i]); }
    }
    if (!bad.length) {
      box.hidden = true;
      clear(box);
      return;
    }
    box.hidden = false;
    clear(box);
    box.appendChild(el('div', 'banner-title',
      bad.length + ' 个账号的会话已失效（last_login_ok = false），需要重新登录'));
    box.appendChild(el('p', '',
      '后端不落盘平台密码：cookie 过期后必须重新输一次密码。点「重新登录」展开输入框。'));
    var ul = el('ul');
    for (var j = 0; j < bad.length; j++) {
      ul.appendChild(reloginItem(bad[j]));
    }
    box.appendChild(ul);
  }

  function reloginItem(a) {
    var li = el('li');
    li.appendChild(el('span', 'mono',
      (a.platform || '—') + ' · ' + accountName(a) + ' · ' + (a.username || '—') + ' '));
    var b = el('button', 'btn btn-sm btn-warn', '重新登录');
    b.type = 'button';
    b.addEventListener('click', function () {
      var id = String(a.id);
      state.acct.open[id] = true;
      state.acct.panel[id] = 'relogin';
      renderAccounts();
      focusEl($('relogin-pass-' + id));
    });
    li.appendChild(b);
    return li;
  }

  /* 新增账号里的平台下拉（跟随左侧平台清单；当前平台默认选中）。 */
  function syncAccountPlatformOptions(force) {
    var sel = $('na-platform');
    if (!sel) { return; }
    var names = state.platforms.map(function (p) { return p.name; }).join(',');
    var sig = names + '|' + (state.platform || '');
    if (!force && sig === sel.getAttribute('data-sig')) { return; }
    sel.setAttribute('data-sig', sig);
    var current = String(sel.value || '');
    var kids = [el('option', '', '— 请选择平台 —')];
    kids[0].value = '';
    for (var i = 0; i < state.platforms.length; i++) {
      var p = state.platforms[i];
      var o = el('option', '', (p.display_name || p.name) + ' · ' + p.name);
      o.value = p.name;
      kids.push(o);
    }
    fill(sel, kids);
    var want = state.platform || current || '';
    sel.value = want;
    if (String(sel.value) !== want) { sel.value = ''; }
  }

  function onCreateAccount(ev) {
    ev.preventDefault();
    var platform = String($('na-platform').value || '');
    var label = String($('na-label').value || '').trim();
    var username = String($('na-username').value || '').trim();
    var password = String($('na-password').value || '');
    var login = $('na-login').checked === true;
    setBoxError('account-error', '');
    if (!platform) {
      setBoxError('account-error', '请先在「平台」下拉里选择一个平台。');
      return;
    }
    if (!username) {
      setBoxError('account-error', '请填写平台用户名。');
      focusEl($('na-username'));
      return;
    }
    if (!password) {
      setBoxError('account-error', '请填写平台密码（只在登录时用一次，后端不落盘）。');
      focusEl($('na-password'));
      return;
    }
    var payload = { platform: platform, label: label, username: username, login: login };
    if (password) { payload.password = password; }

    $('na-submit').disabled = true;
    $('na-state').textContent = '正在 POST /api/accounts …' + (login ? '（会真的登录平台，只读）' : '');
    postJson('/api/accounts', payload).then(function () {
      showToast('已新增账号「' + (label || username) + '」' + (login ? '，并把登录结果记下来了' : '（未登录）'));
      $('na-label').value = '';
      $('na-username').value = '';
      $('na-password').value = '';
      reloadAfterAccountChange(platform);
    }).catch(function (e) {
      setBoxError('account-error', '新增账号失败：' + describeError(e, { platform: platform }));
    }).then(function () {
      $('na-submit').disabled = false;
      $('na-state').textContent = '';
    });
  }

  /* 切号 / 重新登录 / 新增 / 删除之后：账号列表与当前页数据都必须重取
     （cookie 换了，课程 / 作业 / 题库的口径就跟着变）。 */
  function reloadAfterAccountChange(platform) {
    loadAccounts();
    loadOverview();
    if (state.page === 'courses') { loadActionPage('courses'); }
    else if (state.page === 'assignments') { loadActionPage('assignments'); }
    else if (state.page === 'bank') { loadBank(); }
    else if (state.page === 'tools') { resetTools(); loadActions(); }
    else if (state.page === 'logs') { loadRuns(); loadSubmissions(); loadJobs(); }
    else if (state.page === 'stats') { loadStatistics(); loadStatRuns(); }
    if (platform && state.platform && String(platform) !== String(state.platform)) {
      showToast('注意：操作的是平台 ' + platform + '，当前平台是 ' + state.platform +
                '；可在左侧「平台」里切过去。');
    }
  }

  /* ------ 账号页（下）：配置现状 /api/settings（全局，不随平台与归属过滤） ------ */

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
    renderLlm();

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

  /* ==================================================================
     大模型答题状态（只读）
     契约本轮**没有**定义 /api/overview.llm 的形状（实测后端也没返回 llm 字段），
     所以这里做只读的容错展示：有就用，没有就显示「未配置」。
     前端**不收集 api_key** —— 密钥只写服务端环境变量，界面不读、不回显、不外发。
     ================================================================== */

  function llmSource() {
    var o = state.overview, s = state.settings;
    if (isObj(o) && has(o, 'llm')) { return { llm: o.llm, from: 'GET /api/overview' }; }
    if (isObj(s) && has(s, 'llm')) { return { llm: s.llm, from: 'GET /api/settings' }; }
    return { llm: null, from: '' };
  }

  function llmEnabled(llm) {
    if (llm === true) { return true; }
    if (!llm || llm === false) { return false; }
    if (!isObj(llm)) { return !!llm; }
    if (typeof llm.enabled === 'boolean') { return llm.enabled; }
    if (typeof llm.configured === 'boolean') { return llm.configured; }
    if (typeof llm.api_key_set === 'boolean') { return llm.api_key_set; }
    return !!(llm.model || llm.model_name || llm.provider || llm.api_base || llm.base_url);
  }

  function llmField(llm, keys) {
    if (!isObj(llm)) { return null; }
    for (var i = 0; i < keys.length; i++) {
      var v = llm[keys[i]];
      if (v !== null && v !== undefined && v !== '') { return v; }
    }
    return null;
  }

  function renderLlm() {
    var body = $('llm-body');
    var grid = $('llm-grid');
    var status = $('llm-status');
    var empty = $('llm-empty');
    var hint = $('llm-hint');
    if (!body || !grid || !status) { return; }

    var src = llmSource();
    var llm = src.llm;
    var on = llmEnabled(llm);

    clear(status);
    var st = el('span', 'stat');
    st.appendChild(el('span', 'stat-label', '大模型答题'));
    st.appendChild(badge(on ? '已启用' : '未配置', on ? 'badge-ok' : 'badge-warn'));
    status.appendChild(st);
    var st2 = el('span', 'stat');
    st2.appendChild(el('span', 'stat-label', '来源'));
    st2.appendChild(el('span', 'stat-value mono', src.from || '—'));
    status.appendChild(st2);

    if (hint) {
      hint.textContent = on
        ? ('已启用 · 来自 ' + src.from + '（只读展示，本页不收集 api_key）')
        : '未配置 / 未读到 llm 字段 · 界面只读，api_key 只写服务端环境变量';
    }
    if (empty) {
      if (src.llm === null) {
        empty.hidden = false;
        empty.textContent = '后端当前没有返回大模型配置（/api/overview 与 /api/settings 都没有 ' +
          'llm 字段），按「未配置」显示。契约本轮未定义该字段的形状 —— ' +
          '启用方式是把密钥写进服务端环境变量后重启服务；本页不收集、不回显任何密钥。';
      } else {
        empty.hidden = true;
        empty.textContent = '';
      }
    }

    var pairs = [
      ['状态', on ? '已启用' : '未配置'],
      ['来源', src.from || '—（后端未返回 llm 字段）'],
      ['provider', llmField(llm, ['provider', 'vendor', 'name'])],
      ['model', llmField(llm, ['model', 'model_name', 'modelName'])],
      ['api_base', llmField(llm, ['api_base', 'base_url', 'baseUrl', 'endpoint'])],
      ['api_key', llmField(llm, ['api_key_set', 'has_api_key']) === true
        ? '已由服务端环境变量提供（界面不显示、不收集）'
        : '不在前端显示'],
      ['min_confidence', confidenceThreshold() === null ? null : confidenceThreshold()]
    ];
    var kids = [];
    for (var i = 0; i < pairs.length; i++) {
      var wrap = el('div');
      wrap.appendChild(el('dt', '', pairs[i][0]));
      var v = pairs[i][1];
      var text = (v === null || v === undefined || v === '') ? '—'
        : (isObj(v) || Array.isArray(v) ? jsonText(v, 160) : String(v));
      wrap.appendChild(el('dd', monoish(text) ? 'mono' : '', text));
      kids.push(wrap);
    }
    fill(grid, kids);
    body.hidden = false;
    fillLlmForm(llm);
  }

  /* ==================================================================
     大模型配置表单（可写）
     契约：POST /api/settings/llm {base_url, model, api_key?, timeout?, temperature?}
           POST /api/settings/llm/test            → {ok, model, reply, latency_ms}
     密钥只写不读：输入框永远留空，占位符只说「已设置 / 未设置」。
     ================================================================== */

  function fillLlmForm(llm) {
    var form = $('llm-form');
    if (!form) { return; }
    form.hidden = !(isObj(llm) && llm.editable !== false);
    if (form.hidden) { return; }
    var base = $('llm-base-url'), model = $('llm-model');
    var key = $('llm-api-key'), timeout = $('llm-timeout'), temp = $('llm-temperature');
    if (base && document.activeElement !== base) {
      base.value = String(llmField(llm, ['api_base', 'base_url', 'baseUrl', 'endpoint']) || '');
    }
    if (model && document.activeElement !== model) {
      model.value = String(llmField(llm, ['model', 'model_name', 'modelName']) || '');
    }
    if (key) {
      key.value = '';
      key.placeholder = llm.api_key_set ? '已设置（留空 = 不修改）' : '未设置，粘贴 key 后保存';
    }
    if (timeout && document.activeElement !== timeout) {
      timeout.value = llm.timeout === undefined || llm.timeout === null ? '60' : String(llm.timeout);
    }
    if (temp && document.activeElement !== temp) {
      temp.value = llm.temperature === undefined || llm.temperature === null
        ? '0' : String(llm.temperature);
    }
    var hint = $('llm-hint');
    if (hint) {
      hint.textContent = (llm.source === 'console')
        ? ('可在本页修改 · 已覆盖字段：' + ((llm.override_fields || []).join('、') || '—'))
        : (llm.enabled
          ? '当前值来自 .env · 改完保存即覆盖'
          : '未配置：填接口地址 + 模型 + key 后保存');
    }
  }

  function setLlmMsg(text, ok) {
    var node = $('llm-msg');
    if (!node) { return; }
    node.textContent = text || '';
    node.className = 'field-hint' + (ok === true ? ' llm-msg-ok' : (ok === false ? ' llm-msg-err' : ''));
  }

  function applyLlmStatus(body) {
    if (!isObj(body)) { return; }
    state.settings = state.settings || {};
    state.settings.llm = body;
    renderLlm();
  }

  function saveLlmConfig() {
    var form = $('llm-form');
    if (!form) { return; }
    var payload = {
      base_url: String(($('llm-base-url') || {}).value || '').trim(),
      model: String(($('llm-model') || {}).value || '').trim()
    };
    var key = String(($('llm-api-key') || {}).value || '').trim();
    if (key) { payload.api_key = key; }
    var timeout = String(($('llm-timeout') || {}).value || '').trim();
    if (timeout) { payload.timeout = Number(timeout); }
    var temp = String(($('llm-temperature') || {}).value || '').trim();
    if (temp) { payload.temperature = Number(temp); }

    setLlmMsg('保存中…');
    postJson('/api/settings/llm', payload).then(function (body) {
      var k = $('llm-api-key');
      if (k) { k.value = ''; }
      applyLlmStatus(body);
      setLlmMsg('已保存，立即生效（新请求就用这套配置）', true);
    }, function (err) {
      setLlmMsg('保存失败：' + describeError(err), false);
    });
  }

  function clearLlmKey() {
    if (!window.confirm('清除已保存的 API Key？（之后回落到 .env，若 .env 也没有就无法调用）')) {
      return;
    }
    setLlmMsg('清除中…');
    postJson('/api/settings/llm', { clear_api_key: true }).then(function (body) {
      applyLlmStatus(body);
      setLlmMsg('API Key 已清除', true);
    }, function (err) {
      setLlmMsg('清除失败：' + describeError(err), false);
    });
  }

  function testLlmConnection() {
    setLlmMsg('测试中…（会真发一次最小请求）');
    postJson('/api/settings/llm/test', {}).then(function (body) {
      setLlmMsg('连接成功 · ' + (body.model || '') + ' · ' + (body.latency_ms || '?') +
        'ms · 回复「' + (body.reply || '').slice(0, 20) + '」', true);
    }, function (err) {
      setLlmMsg('连接失败：' + describeError(err), false);
    });
  }

  /* ==================================================================
     浏览器会话抓取（账号页）
     契约里没有「写入 cookie」的接口，所以这里给可复制的命令行。
     重点说清：未登录的登录页也会下发匿名 csrfToken，所以要验证会话。
     ================================================================== */

  function capturePlatformName() {
    var sel = $('cap-platform');
    var v = sel ? String(sel.value || '') : '';
    if (v) { return v; }
    if (state.platform) { return state.platform; }
    return (state.platforms[0] && state.platforms[0].name) || '<平台>';
  }

  function copyCommand(text, btn) {
    var done = function (ok) {
      if (!ok) {
        showToast('复制失败：请手动选中命令行复制（浏览器不允许脚本写剪贴板）。');
        return;
      }
      showToast('命令已复制到剪贴板');
      if (!btn) { return; }
      btn.textContent = '已复制';
      setTimeout(function () { if (btn) { btn.textContent = '复制'; } }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); },
        function () { done(false); });
      return;
    }
    done(false);
  }

  function cmdItem(spec) {
    var item = el('div', 'cmd-item');
    var head = el('div', 'cmd-head');
    head.appendChild(el('span', 'cmd-label', spec.label));
    if (spec.cmd) {
      var btn = el('button', 'btn btn-sm', '复制');
      btn.type = 'button';
      btn.title = '复制这条命令（在仓库根目录执行）';
      btn.addEventListener('click', function () { copyCommand(spec.cmd, btn); });
      head.appendChild(btn);
    }
    item.appendChild(head);
    if (spec.cmd) { item.appendChild(el('pre', 'cmd', spec.cmd)); }
    if (spec.note) { item.appendChild(el('p', 'cmd-note', spec.note)); }
    return item;
  }

  function renderCaptureCmds() {
    var box = $('cap-cmds');
    if (!box) { return; }
    var plt = capturePlatformName();
    var cmds = [
      { label: '① 用 Chrome 打开该平台并登录一次（过掉验证码 / 扫码），然后完全退出 Chrome',
        cmd: '',
        note: '抓取用的是你已经登录过的浏览器用户目录，不接触密码、不碰验证码。' },
      { label: '② 用那个用户目录抓取（headless，不需要显示器）',
        cmd: 'PYTHONPATH=src python3 -m kedaibiao capture ' + plt + ' --profile ~/.config/google-chrome',
        note: '必须先完全退出 Chrome，否则用户目录被占用。' },
      { label: '③ 也可以手贴 cookie（DevTools → Network → 任一 api 请求 → Cookie 头）',
        cmd: 'PYTHONPATH=src python3 -m kedaibiao capture ' + plt +
          ' --cookie "csrfToken=…; x-token=…"',
        note: '未登录的登录页也会下发匿名 csrfToken，所以 capture 抓完会再打一次只读接口' +
          '验证会话；验证不过会删掉并报错。' }
    ];
    var kids = [];
    for (var i = 0; i < cmds.length; i++) { kids.push(cmdItem(cmds[i])); }
    fill(box, kids);
  }

  /* 「浏览器会话抓取」的平台下拉（默认跟随当前平台）。 */
  function syncCapturePlatformOptions(force) {
    var sel = $('cap-platform');
    if (!sel) { return; }
    var names = state.platforms.map(function (p) { return p.name; }).join(',');
    var sig = names + '|' + (state.platform || '');
    if (!force && sig === sel.getAttribute('data-sig')) { return; }
    sel.setAttribute('data-sig', sig);
    var current = String(sel.value || '');
    var kids = [el('option', '', '— 请选择平台 —')];
    kids[0].value = '';
    for (var i = 0; i < state.platforms.length; i++) {
      var p = state.platforms[i];
      var o = el('option', '', (p.display_name || p.name) + ' · ' + p.name);
      o.value = p.name;
      kids.push(o);
    }
    fill(sel, kids);
    var want = state.platform || current || '';
    sel.value = want;
    if (String(sel.value) !== want) { sel.value = ''; }
    renderCaptureCmds();
  }

  /* ==================================================================
     用户页（仅超管；契约 §1.5）
       GET  /api/users                  用户列表 + 每人账号数
       POST /api/users                  {username, password, role, note}
       POST /api/users/<id>/update      {username?, password?, role?, disabled?, note?}
       POST /api/users/<id>/delete      不允许删掉最后一个超管（400）
     入口只对超管显示，但**不只靠隐藏**：普通用户走到 #/users 时照样请求 /api/users，
     页面把后端的 403 与中文 error 原样显示出来。
     ================================================================== */

  function setUsersError(message) {
    state.usersError = message || '';
    renderUsersError();
  }

  function renderUsersError() {
    var box = $('users-error');
    if (!box) { return; }
    if (!state.usersError) { box.hidden = true; box.textContent = ''; return; }
    box.hidden = false;
    box.textContent = state.usersError;
  }

  /* 403 的人话：后端 error 优先，其次说明「用户管理只对超管开放」。 */
  function permissionMessage(e) {
    var base = ((e && e.error) || '').trim();
    return '后端拒绝了这次请求（HTTP ' + ((e && e.status) || 403) + ' · kind: ' + dash(e && e.kind) + '）：' +
      (base || '（后端没有给中文说明）') +
      '。用户管理只对超管开放：普通用户（role = user）请求 /api/users* 会被后端直接拒绝，' +
      '带 ?owner_id= 指向别人同样是 403。若你确实是超管，请确认是通过 ' +
      'http://127.0.0.1:<端口> 访问本页（Host / Origin 被安全策略拒绝也返回 403）。';
  }

  var identityRefreshes = 0;
  /* 403 可能意味着角色被改了：重问一次身份，别让界面卡在错的角色上。 */
  function refreshIdentity() {
    if (identityRefreshes >= 2) { return Promise.resolve(); }
    identityRefreshes++;
    return fetchIdentity().then(function () {
      identityRefreshes = 0;
      renderChrome();
      showPage();
      return loadPageData(true);
    }).catch(function () { /* 401 会走 onUnauthorized */ });
  }

  function loadUsers(opts) {
    var o = opts || {};
    if (!o.silent) {
      state.usersLoading = true;
      renderUsers();
    }
    return api('/api/users').then(function (body) {
      state.users = extractList(body, ['users', 'items', 'list']);
      state.usersLoaded = true;
      state.usersLoading = false;
      state.usersError = '';
      renderUsers();
      renderOwnerFilter();
      clearProblem('users');
    }).catch(function (e) {
      state.users = [];
      state.usersLoaded = false;
      state.usersLoading = false;
      renderOwnerFilter();
      if (e && e.status === 403) {
        /* 不吞掉 403：页面显示后端原文（这可能就是「普通用户没有这个功能」）。 */
        state.usersError = permissionMessage(e);
        setProblem('users', state.usersError, 'error');
        if (isSuperadmin()) { refreshIdentity(); }
      } else {
        setProblem('users', '用户列表加载失败：' + describeError(e), 'error');
      }
      renderUsers();
    });
  }

  function renderUsers() {
    var gate = $('users-gate');
    var body = $('users-body');
    if (!gate || !body) { return; }
    if (!state.usersLoaded) {
      gate.hidden = false;
      body.hidden = true;
      gate.textContent = state.usersLoading
        ? '正在读取 GET /api/users …'
        : (state.usersError || (isSuperadmin()
            ? '还没取到用户列表，点「刷新」重试。'
            : '「用户」是超管功能：普通用户看不到这个入口，后端对非超管请求 /api/users* 一律返回 403。'));
      renderUsersError();
      return;
    }
    gate.hidden = true;
    gate.textContent = '';
    body.hidden = false;
    renderUsersError();
    renderUserRows();
  }

  function renderUserRows() {
    var list = state.users;
    var rows = [];
    for (var i = 0; i < list.length; i++) {
      var u = isObj(list[i]) ? list[i] : {};
      rows.push(userRow(u));
      if (state.usersPwOpen !== null && String(state.usersPwOpen) === String(u.id)) {
        rows.push(userPwRow(u));
      }
    }
    $('users-count').textContent = list.length ? ('共 ' + list.length + ' 个用户') : '';
    fillTable('users-table', 'users-rows', 'users-empty', rows,
      '没有任何用户。用上面的「新建用户」加一个。');
  }

  function userRoleCell(u, id) {
    var td = el('td', 'nowrap');
    var sel = el('select', 'mono role-sel');
    var roles = ['user', 'superadmin'];
    var have = false;
    for (var i = 0; i < roles.length; i++) {
      var o = el('option', '', roles[i] + '（' + roleText(roles[i]) + '）');
      o.value = roles[i];
      if (u.role === roles[i]) { have = true; }
      sel.appendChild(o);
    }
    /* 认不出的角色：原样列出来，别静默改写用户数据。 */
    if (!have && u.role) {
      var extra = el('option', '', String(u.role));
      extra.value = String(u.role);
      sel.appendChild(extra);
    }
    sel.value = u.role || 'user';
    /* 改角色是敏感操作：先二次确认，取消则把下拉回退。 */
    sel.addEventListener('change', function () {
      var next = String(sel.value || '');
      var prev = u.role || 'user';
      if (next === prev) { return; }
      if (!window.confirm('把用户「' + (u.username || ('#' + id)) + '」的角色从 ' + roleText(prev) +
                          ' 改成 ' + roleText(next) + '？')) {
        sel.value = prev;
        return;
      }
      userUpdate(u, { role: next }, '已把「' + (u.username || id) + '」的角色改为 ' + roleText(next));
    });
    td.appendChild(sel);
    return td;
  }

  function userRow(u) {
    var id = String(u.id);
    var name = u.username ? String(u.username) : ('#' + id);

    var idCell = el('td');
    idCell.appendChild(el('span', 'mono', name));
    idCell.appendChild(el('span', 'user-id mono', '#' + id));
    idCell.title = 'user id ' + id;

    var stCell = el('td');
    stCell.appendChild(disabledBadge(u.disabled === true));

    var opCell = el('td', 'ctr nowrap');
    var pw = el('button', 'btn btn-sm', '改密');
    pw.type = 'button';
    pw.title = 'POST /api/users/' + id + '/update {password}：密码至少 ' + MIN_PASSWORD + ' 位';
    pw.addEventListener('click', function () {
      state.usersPwOpen = (state.usersPwOpen !== null && String(state.usersPwOpen) === id) ? null : id;
      renderUsers();
      focusEl($('upw-1'));
    });
    opCell.appendChild(pw);

    var next = !(u.disabled === true);
    var tg = el('button', 'btn btn-sm', next ? '停用' : '启用');
    tg.type = 'button';
    tg.title = 'POST /api/users/' + id + '/update {disabled: ' + next + '}';
    tg.addEventListener('click', function () {
      if (next && !window.confirm('停用用户「' + name + '」？停用后该账号无法登录控制台。')) { return; }
      userUpdate(u, { disabled: next }, next ? ('已停用「' + name + '」') : ('已启用「' + name + '」'));
    });
    opCell.appendChild(tg);

    var del = el('button', 'btn btn-sm btn-danger', '删除');
    del.type = 'button';
    del.title = 'POST /api/users/' + id + '/delete：不允许删掉最后一个超管';
    del.addEventListener('click', function () {
      if (!window.confirm('删除用户「' + name + '」？不可撤销。\n' +
                          '（最后一个超管会被后端拒绝，错误会显示在列表上方。）')) { return; }
      postJson('/api/users/' + encodeURIComponent(u.id) + '/delete', {}).then(function () {
        showToast('已删除用户「' + name + '」');
        setUsersError('');
        if (String(state.ownerId) === id) { state.ownerId = null; }
        return loadUsers();
      }).catch(function (e) {
        setUsersError('删除用户失败：' + describeError(e));
      });
    });
    opCell.appendChild(del);

    var countCell = el('td', 'num mono');
    var n = accountCountOf(u);
    countCell.textContent = n === null ? '—' : String(n);
    countCell.title = n === null ? '响应里没有账号数（后端契约只说「用户列表 + 每人账号数」）' : (n + ' 个平台账号');

    return row([
      idCell,
      userRoleCell(u, id),
      stCell,
      cell(fmtTime(u.created_at), 'mono'),
      countCell,
      cell(u.note, 'wrap muted'),
      opCell
    ]);
  }

  function userPwRow(u) {
    var id = String(u.id);
    var tr = el('tr', 'sub-row');
    var td = el('td');
    td.colSpan = 7;
    var form = el('div', 'acct-form');
    form.appendChild(el('span', 'field-label',
      '给「' + (u.username || ('#' + id)) + '」设置新密码（至少 ' + MIN_PASSWORD + ' 位，两次输入要一致）'));
    var p1 = el('input', 'mono acct-input');
    p1.type = 'password'; p1.id = 'upw-1'; p1.autocomplete = 'new-password'; p1.placeholder = '新密码';
    var p2 = el('input', 'mono acct-input');
    p2.type = 'password'; p2.id = 'upw-2'; p2.autocomplete = 'new-password'; p2.placeholder = '再输一次';
    var save = el('button', 'btn btn-sm btn-run', '保存密码');
    save.type = 'button';
    save.addEventListener('click', function () { submitUserPassword(u); });
    var cancel = el('button', 'btn btn-sm btn-ghost', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', function () { state.usersPwOpen = null; renderUsers(); });
    form.appendChild(p1); form.appendChild(p2); form.appendChild(save); form.appendChild(cancel);
    td.appendChild(form);
    tr.appendChild(td);
    return tr;
  }

  function submitUserPassword(u) {
    var p1 = $('upw-1'), p2 = $('upw-2');
    var a = String(p1 && p1.value ? p1.value : '');
    var b = String(p2 && p2.value ? p2.value : '');
    if (a.length < MIN_PASSWORD) {
      setUsersError('密码至少 ' + MIN_PASSWORD + ' 位（前端先拦一次，后端也会返回 400）。');
      focusEl(p1);
      return;
    }
    if (a !== b) {
      setUsersError('两次输入的密码不一致。');
      focusEl(p2);
      return;
    }
    setUsersError('');
    postJson('/api/users/' + encodeURIComponent(u.id) + '/update', { password: a }).then(function () {
      state.usersPwOpen = null;
      showToast('已更新用户「' + (u.username || u.id) + '」的密码');
      return loadUsers();
    }).catch(function (e) {
      setUsersError('改密失败：' + describeError(e));
    });
  }

  function userUpdate(u, payload, okText) {
    return postJson('/api/users/' + encodeURIComponent(u.id) + '/update', payload).then(function () {
      showToast(okText);
      setUsersError('');
      return loadUsers();
    }).catch(function (e) {
      setUsersError('修改用户失败：' + describeError(e));
      return loadUsers();
    });
  }

  function onCreateUser(ev) {
    ev.preventDefault();
    var username = String($('nu-username').value || '').trim();
    var password = String($('nu-password').value || '');
    var role = String($('nu-role').value || 'user');
    var note = String($('nu-note').value || '').trim();
    if (!username) {
      setUsersError('请填写用户名。');
      focusEl($('nu-username'));
      return;
    }
    if (password.length < MIN_PASSWORD) {
      setUsersError('密码至少 ' + MIN_PASSWORD + ' 位（前端先拦一次，后端也会返回 400）。');
      focusEl($('nu-password'));
      return;
    }
    $('nu-submit').disabled = true;
    $('nu-state').textContent = '正在 POST /api/users …';
    postJson('/api/users', { username: username, password: password, role: role, note: note })
      .then(function () {
        showToast('已创建用户「' + username + '」（' + roleText(role) + '）');
        setUsersError('');
        $('nu-username').value = '';
        $('nu-password').value = '';
        $('nu-note').value = '';
        return loadUsers();
      }).catch(function (e) {
        setUsersError('创建用户失败：' + describeError(e));
      }).then(function () {
        $('nu-submit').disabled = false;
        $('nu-state').textContent = '';
      });
  }

  /* ------------------------------------------------------------------ 启动 */

  function loadPageData(noPlatformRetry) {
    /* 未登录不发任何数据请求：登录页由 /api/auth/* 驱动（否则一屏 401 噪音）。 */
    if (!state.authed) { return Promise.resolve(); }
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
      jobs.push(loadAccounts(), loadSettings());
    } else if (state.page === 'users') {
      /* 刻意不看角色就请求：普通用户走到这里必须拿到后端的 403 并显示出来，
         这正是「不只靠隐藏」的那一半。 */
      jobs.push(loadUsers());
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
      syncAccountPlatformOptions(true);
      renderChrome();
      return loadPageData(true);
    });
  }

  /* ==================================================================
     一键自动化（iTEST）
     流程在后端跑（后台 job），前端只做三件事：
       ① 轮询 /api/jobs/<id>，把 logs 逐行铺进终端面板；
       ② 渲染 steps / verify / manual（缺答案的题给人工选择）；
       ③ 人工答案保存（POST /api/platforms/itest/manual）与确认交卷。
     ================================================================== */

  var AUTO_POLL_MS = 1200;

  function autoEl(id) { return $(id); }

  function startAuto(plt, examId, rowObj) {
    var a = state.auto;
    if (a.timer) { clearTimeout(a.timer); a.timer = null; }
    a.last = null;
    a.jobId = null;
    a.job = null;
    a.logCount = 0;
    a.manual = {};
    a.platform = plt;
    a.examId = examId;
    a.title = assignmentTitleOf(rowObj) || examId;

    var panel = autoEl('auto-panel');
    if (panel) { panel.hidden = false; panel.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    setText('auto-hint', platformLabel(plt) + ' · ' + a.title);
    setText('auto-status', '启动中…');
    setText('auto-manual-state', '');
    setAutoLog([{ at: '', level: 'info', text: '$ 请求启动自动化流程…' }]);
    renderAutoSteps([]);
    renderAutoVerify(null);
    renderAutoManual([]);
    setAutoProgress(3);
    var sb = autoEl('auto-submit');
    if (sb) { sb.disabled = true; }

    postJson('/api/platforms/itest/auto', { exam_id: examId }).then(function (job) {
      a.jobId = job && job.job_id;
      if (!a.jobId) {
        setText('auto-status', '后端没有返回 job_id');
        return;
      }
      setText('auto-status', '运行中 · ' + a.jobId);
      a.job = job;
      renderAuto(job);
      pollAuto();
    }).catch(function (e) {
      setText('auto-status', '启动失败：' + describeError(e));
      appendAutoLog('error', '✗ 启动失败：' + describeError(e));
      setAutoProgress(0);
    });
  }

  function pollAuto() {
    var a = state.auto;
    if (!a.jobId) { return; }
    api('/api/jobs/' + encodeURIComponent(a.jobId)).then(function (job) {
      a.job = job;
      renderAuto(job);
      if (job && job.status === 'running') {
        a.timer = setTimeout(pollAuto, AUTO_POLL_MS);
      } else {
        a.timer = null;
        setAutoProgress(100);
      }
    }).catch(function (e) {
      setText('auto-status', '轮询失败：' + describeError(e));
      a.timer = setTimeout(pollAuto, 4000);
    });
  }

  function setAutoProgress(pct) {
    var bar = autoEl('auto-progress-bar');
    if (bar) { bar.style.width = Math.max(0, Math.min(100, pct)) + '%'; }
  }

  function renderAuto(job) {
    if (!job) { return; }
    renderAutoSteps(job.steps || []);
    setAutoLog((job.logs || []).map(function (l) {
      return { at: l.at || '', level: l.level || 'info', text: l.text || '' };
    }));
    renderAutoVerify(job.verify);
    renderAutoManual(job.manual || []);

    var st = autoEl('auto-status');
    if (st) {
      var tail = job.status === 'running' ? '运行中'
        : (job.status === 'succeeded' ? '已完成' : '失败');
      st.textContent = tail + ' · ' + (job.job_id || '') +
        (job.error ? ' · ' + job.error : '');
    }
    if (job.paper && job.paper.title) {
      setText('auto-hint', 'iTEST · ' + job.paper.title +
        ' · ' + job.paper.question_count + ' 题 / ' + job.paper.slot_count + ' 槽位');
    }
    var missing = (job.manual || []).length;
    var sb = autoEl('auto-submit');
    if (sb) {
      sb.disabled = !(job.status !== 'running' && missing === 0);
      sb.title = sb.disabled
        ? (missing ? ('还有 ' + missing + ' 题没答案，先在下方补上') : '流程还没跑完')
        : '真的交卷（不可逆）：交前会重新填写并逐项核验';
    }
  }

  function renderAutoSteps(steps) {
    var box = autoEl('auto-steps');
    if (!box) { return; }
    clear(box);
    for (var i = 0; i < steps.length; i++) {
      var step = steps[i] || {};
      var wrap = el('span', 'stat');
      wrap.appendChild(el('span', 'stat-label', step.name || '—'));
      wrap.appendChild(badge(step.ok ? '通过' : '未通过', step.ok ? 'badge-ok' : 'badge-fail'));
      if (step.detail) { wrap.appendChild(el('span', 'hint', String(step.detail).slice(0, 40))); }
      box.appendChild(wrap);
    }
  }

  /* 终端式日志的公用渲染：两个面板（一键自动化 / 平台执行日志）共用 */
  function paintLog(node, lines) {
    if (!node) { return; }
    var atBottom = node.scrollTop + node.clientHeight + 24 >= node.scrollHeight;
    clear(node);
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i] || {};
      var row = el('div', 'ln ln-' + (line.level || 'info'));
      if (line.at) {
        row.appendChild(el('span', 'ln-time', '[' + String(line.at).slice(11, 19) + '] '));
      }
      row.appendChild(document.createTextNode(String(line.text || '')));
      node.appendChild(row);
    }
    if (atBottom) { node.scrollTop = node.scrollHeight; }
  }

  function setAutoLog(lines) {
    paintLog(autoEl('auto-log'), lines);
  }

  function appendAutoLog(level, text) {
    var node = autoEl('auto-log');
    if (!node) { return; }
    var row = el('div', 'ln ln-' + (level || 'info'));
    row.appendChild(document.createTextNode(String(text || '')));
    node.appendChild(row);
    node.scrollTop = node.scrollHeight;
  }

  function renderAutoVerify(verify) {
    var box = autoEl('auto-verify');
    if (!box) { return; }
    if (!verify) { box.hidden = true; clear(box); return; }
    box.hidden = false;
    clear(box);
    var head = el('div', 'stat-row');
    var persisted = verify.persisted !== false && (verify.matched || 0) > 0;
    var s1 = el('span', 'stat');
    s1.appendChild(el('span', 'stat-label', '服务端已保存'));
    s1.appendChild(badge(persisted ? '是' : '否（交卷时重填）',
      persisted ? 'badge-ok' : 'badge-warn'));
    head.appendChild(s1);
    var s2 = el('span', 'stat');
    s2.appendChild(el('span', 'stat-label', '缺答案'));
    s2.appendChild(badge(String((verify.missing || []).length),
      (verify.missing || []).length ? 'badge-warn' : 'badge-ok'));
    head.appendChild(s2);
    var s3 = el('span', 'stat');
    s3.appendChild(el('span', 'stat-label', '核验不符'));
    s3.appendChild(badge(String((verify.checked && verify.checked.total) || 0),
      (verify.checked && verify.checked.total) ? 'badge-fail' : 'badge-ok'));
    head.appendChild(s3);
    box.appendChild(head);

    var mism = verify.mismatched || [];
    if (mism.length) {
      var tbl = el('table', 'tbl');
      var thead = el('thead');
      var tr = el('tr');
      tr.appendChild(el('th', '', '题号'));
      tr.appendChild(el('th', '', '答案单'));
      tr.appendChild(el('th', '', '页面读回'));
      thead.appendChild(tr);
      tbl.appendChild(thead);
      var tb = el('tbody');
      for (var i = 0; i < mism.length; i++) {
        var r = el('tr');
        r.appendChild(el('td', 'mono', mism[i].qid));
        r.appendChild(el('td', 'mono', mism[i].want || '—'));
        r.appendChild(el('td', 'mono', mism[i].got || '（空）'));
        tb.appendChild(r);
      }
      tbl.appendChild(tb);
      box.appendChild(el('p', 'field-hint',
        '重进考场读回为空 —— iTEST 的自动保存不认脚本填值（实测）。' +
        '同一会话里的「逐项核验」已经确认页面值全部正确；交卷时会重新填写并再核验一次，不影响结果。'));
      box.appendChild(tbl);
    }
  }

  function renderAutoManual(rows) {
    var wrap = autoEl('auto-manual');
    var list = autoEl('auto-manual-list');
    if (!wrap || !list) { return; }
    if (!rows.length) { wrap.hidden = true; clear(list); return; }
    wrap.hidden = false;
    clear(list);
    for (var i = 0; i < rows.length; i++) {
      list.appendChild(manualQuestionNode(rows[i]));
    }
  }

  function manualQuestionNode(row) {
    var a = state.auto;
    var box = el('div', 'auto-q');
    var head = el('div', 'auto-q-head');
    head.appendChild(badge(row.kind_label || row.kind || '题目', 'badge-idle'));
    head.appendChild(el('span', 'mono', row.qid));
    if (row.slots) { head.appendChild(el('span', 'hint', row.slots + ' 空')); }
    box.appendChild(head);
    if (row.stem) { box.appendChild(el('div', 'auto-q-stem', row.stem)); }

    if (row.kind === 'radio' && row.options && row.options.length) {
      var subs = {};
      for (var i = 0; i < row.options.length; i++) {
        var opt = row.options[i] || {};
        (subs[opt.sub] = subs[opt.sub] || []).push(opt);
      }
      var keys = Object.keys(subs).sort(function (x, y) { return Number(x) - Number(y); });
      var box2 = el('div', 'auto-q-subs');
      for (var k = 0; k < keys.length; k++) {
        var sub = keys[k];
        var lab = el('label');
        lab.appendChild(el('span', '', '第 ' + (k + 1) + ' 小题'));
        var sel = el('select', 'mono');
        sel.setAttribute('data-manual-qid', row.qid);
        sel.setAttribute('data-manual-sub', sub);
        var empty = el('option', '', '— 请选择 —');
        empty.value = '';
        sel.appendChild(empty);
        for (var j = 0; j < subs[sub].length; j++) {
          var o = subs[sub][j];
          if (!o.letter) { continue; }
          var optEl = el('option', '', o.letter + '. ' + String(o.text || '').slice(0, 40));
          optEl.value = o.letter;
          sel.appendChild(optEl);
        }
        sel.addEventListener('change', function () { collectManual(); });
        lab.appendChild(sel);
        box2.appendChild(lab);
      }
      box.appendChild(box2);
    } else {
      var input = row.kind === 'write' ? el('textarea', 'mono') : el('input', 'mono');
      input.setAttribute('data-manual-qid', row.qid);
      input.setAttribute('data-manual-sub', '1');
      if (row.kind === 'write') { input.rows = 2; }
      else { input.placeholder = '按空顺序填字母，用空格分隔，如 A C D'; }
      input.addEventListener('input', function () { collectManual(); });
      box.appendChild(input);
    }
    return box;
  }

  /* 把界面上的人工输入收进 state.auto.manual（qid → 答案字符串） */
  function collectManual() {
    var a = state.auto;
    var nodes = document.querySelectorAll('[data-manual-qid]');
    var byQid = {};
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var qid = node.getAttribute('data-manual-qid');
      var sub = Number(node.getAttribute('data-manual-sub') || 1);
      var value = String(node.value || '').trim();
      if (!value) { continue; }
      if (!byQid[qid]) { byQid[qid] = {}; }
      byQid[qid][sub] = value;
    }
    var out = {};
    for (var q in byQid) {
      if (!Object.prototype.hasOwnProperty.call(byQid, q)) { continue; }
      var subs = byQid[q];
      var keys = Object.keys(subs).sort(function (x, y) { return Number(x) - Number(y); });
      out[q] = keys.map(function (k2) { return subs[k2]; }).join(' ');
    }
    a.manual = out;
    return out;
  }

  function saveManual() {
    var a = state.auto;
    if (!a.jobId || a.saving) { return; }
    var answers = collectManual();
    if (!Object.keys(answers).length) {
      setText('auto-manual-state', '还没有填任何答案');
      return;
    }
    a.saving = true;
    setText('auto-manual-state', '保存中…');
    postJson('/api/platforms/itest/manual', { exam_id: a.examId, answers: answers })
      .then(function (body) {
        a.saving = false;
        var missing = body && body.missing ? body.missing : [];
        var filled = (body && body.answered) || 0;
        setText('auto-manual-state', '已保存：共 ' + filled + ' 题有答案，还缺 ' + missing.length + ' 题');
        appendAutoLog('ok', '· 人工答案已保存（' + Object.keys(answers).length + ' 题），还缺 ' +
          missing.length + ' 题');
        /* 本地把已保存的题从清单里去掉，剩下继续显示 */
        var rows = (a.job && a.job.manual) || [];
        var left = rows.filter(function (r) { return missing.indexOf(r.qid) >= 0; });
        a.job.manual = left;
        renderAutoManual(left);
        var sb = autoEl('auto-submit');
        if (sb) { sb.disabled = missing.length > 0; }
        if (!missing.length) {
          appendAutoLog('ok', '· 全部题目都有答案了，可以「确认交卷」');
        }
      })
      .catch(function (e) {
        a.saving = false;
        setText('auto-manual-state', '保存失败：' + describeError(e));
        appendAutoLog('error', '✗ 保存人工答案失败：' + describeError(e));
      });
  }

  function submitAuto() {
    var a = state.auto;
    if (a.submitting || !a.examId) { return; }
    var id = a.examId;
    var title = a.title;
    if (!window.confirm('确认交卷：' + title + '\n\n交卷后不能再次作答，确定吗？')) { return; }
    a.submitting = true;
    var sb = autoEl('auto-submit');
    if (sb) { sb.disabled = true; }
    setText('auto-status', '交卷中…（会重新进入考场、重填并逐项核验）');
    appendAutoLog('info', '');
    appendAutoLog('info', '$ POST /api/assignments/submit confirm=true');
    postJson('/api/assignments/submit', { platform: 'itest', assignment_id: id, confirm: true })
      .then(function (body) {
        a.submitting = false;
        var finished = body && body.server_finish;
        appendAutoLog(finished === 1 || finished === '1' ? 'ok' : 'warn',
          '· 交卷返回：submitted=' + (body && body.submitted) +
          ' server_finish=' + finished);
        if (body && body.steps) {
          for (var i = 0; i < body.steps.length; i++) {
            appendAutoLog('info', '   · ' + (body.steps[i].name || '') + ' ' +
              (body.steps[i].detail || ''));
          }
        }
        setText('auto-status', finished === 1 || finished === '1'
          ? '已交卷（服务端 finish=1）'
          : '交卷结果待确认，请看日志/作业列表');
        if (body && body.screenshot) { appendAutoLog('info', '   截图：' + body.screenshot); }
      })
      .catch(function (e) {
        a.submitting = false;
        setText('auto-status', '交卷失败：' + describeError(e));
        appendAutoLog('error', '✗ 交卷失败：' + describeError(e));
      });
  }

  /** 自动化任务是否还在跑（决定要不要拦切换）。 */
  function autoRunning() {
    var a = state.auto;
    return !!(a.jobId && a.job && a.job.status === 'running');
  }

  function autoLeaveWarning() {
    var a = state.auto;
    var steps = (a.job && a.job.steps) || [];
    var logs = (a.job && a.job.logs) || [];
    var tail = logs.length ? logs[logs.length - 1].text : '';
    return '「一键自动化」还在运行（已完成 ' + steps.length + ' 步）。\n\n' +
      (tail ? '当前：' + tail + '\n\n' : '') +
      '切换界面后日志面板会收起 —— 任务仍会在后台继续跑，' +
      '回到「作业」页可以点「继续查看自动化」看进度。\n\n确定要切换吗？';
  }

  /**
   * 收起自动化面板。
   * keepRef=true 时保留 last（job 还在后台跑），作业页会显示「继续查看自动化」入口；
   * 用户主动点「关闭」则连 last 一起清掉。
   */
  /* ---- 平台执行日志（每个平台独立一份）---- */

  function openPlatLogs() {
    var p = state.plog;
    var panel = $('plog-panel');
    if (!panel || !state.platform) {
      showToast('先选一个平台，再看它的执行日志');
      return;
    }
    p.open = true;
    p.platform = state.platform;
    panel.hidden = false;
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var hint = $('plog-hint');
    if (hint) { hint.textContent = platformLabel(p.platform) + ' · 最近 200 行'; }
    fetchPlatLogs();
    panelPollStart();
  }

  function closePlatLogs() {
    var p = state.plog;
    if (p.timer) { clearInterval(p.timer); p.timer = null; }
    p.open = false;
    var panel = $('plog-panel');
    if (panel) { panel.hidden = true; }
  }

  function panelPollStart() {
    var p = state.plog;
    if (p.timer) { clearInterval(p.timer); }
    p.timer = setInterval(function () {
      if (!p.open) { return; }
      fetchPlatLogs();
    }, 2500);
  }

  function fetchPlatLogs() {
    var p = state.plog;
    if (!p.open || !state.platform) { return; }
    var plt = state.platform;
    api('/api/platforms/' + encodeURIComponent(plt) + '/logs?limit=200').then(function (body) {
      if (state.platform !== plt) { return; }
      paintLog($('plog-log'), (body && body.logs) || []);
      var hint = $('plog-hint');
      if (hint) {
        hint.textContent = platformLabel(plt) + ' · 共 ' + ((body && body.total) || 0) +
          ' 行（显示最近 200 行）';
      }
    }).catch(function (e) {
      paintLog($('plog-log'), [{ at: '', level: 'error', text: '加载失败：' + describeError(e) }]);
    });
  }

  function closeAuto(keepRef) {
    var a = state.auto;
    if (a.timer) { clearTimeout(a.timer); a.timer = null; }
    var panel = autoEl('auto-panel');
    if (panel) { panel.hidden = true; }
    if (a.jobId) {
      a.last = keepRef ? {
        jobId: a.jobId, platform: a.platform, examId: a.examId,
        title: a.title, status: (a.job && a.job.status) || 'running'
      } : null;
    }
    a.jobId = keepRef ? a.jobId : null;
    if (!keepRef) { a.job = null; a.manual = {}; }
    renderAutoReopen();
  }

  /* 作业页顶部的「继续查看自动化」入口：只在 iTEST 且有上一次任务时出现 */
  function renderAutoReopen() {
    var btn = $('auto-reopen');
    if (!btn) { return; }
    var last = state.auto.last;
    var show = !!(last && state.platform === 'itest');
    btn.hidden = !show;
    if (show) {
      btn.textContent = '继续查看自动化 · ' + (last.title || last.examId);
      btn.title = '任务 ' + last.jobId + '（' + (last.status || 'running') + '）仍在后台，点开看日志';
    }
  }

  function reopenAuto() {
    var a = state.auto;
    var last = a.last;
    if (!last) { return; }
    a.jobId = last.jobId;
    a.platform = last.platform || 'itest';
    a.examId = last.examId;
    a.title = last.title;
    var panel = autoEl('auto-panel');
    if (panel) { panel.hidden = false; panel.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    setText('auto-hint', 'iTEST · ' + (last.title || last.examId));
    setText('auto-status', '恢复查看 · ' + last.jobId);
    pollAuto();
  }

  /* 自测钩子（只在 ?debug=1 时挂）：把自动化标成「运行中」，用来验证切换拦截。
     正常访问时不存在，不参与任何业务逻辑。 */
  if (/[?&]debug=1\b/.test(location.search)) {
    window.__kdbGraderDebug = function (stopReason, fakeErr) {
      grader.error = fakeErr ? describeError(fakeErr, { platform: 'pigai' }) : '';
      grader.job = {
        job_id: 'debug-grade', status: 'failed', stop_reason: stopReason || 'timeout',
        message: '等待分析超时（180s）。稿件号 900000001 已提交，可以「继续等待 N 秒」或「手动获取一次」。',
        target_score: 90, max_attempts: 3, best_score: null,
        attempts: [{ attempt: 1, eid: '900000001', score: null, graded: false,
                     raw_state: '', word_count: 386 }],
        events: [{ at: '', kind: 'attempt.timeout', error: '等待分析超时（180s）' }],
      };
      if (typeof graderRender === 'function') { graderRender(); }
      return true;
    };

    window.__kdbAutoDebug = function (running) {
      if (running) {
        state.auto.jobId = state.auto.jobId || 'debug-job';
        state.auto.job = {
          job_id: 'debug-job', status: 'running',
          steps: [{ name: '取卷', ok: true }, { name: 'AI 答题', ok: true }],
          logs: [{ at: '', level: 'info', text: '[3/6] 音频转写 + 大模型答题…' }],
          manual: [], verify: null,
        };
      } else {
        state.auto.job = null;
        state.auto.jobId = null;
      }
      return autoRunning();
    };
  }

  function bindEvents() {
    /* 登录 / 退出：401 由请求层切登录页，这里只管表单与手动退出。 */
    $('login-form').addEventListener('submit', doLogin);
    $('login-retry').addEventListener('click', function () { startSession(); });
    $('logout-btn').addEventListener('click', function () { doLogout(); });
    /* 超管的归属筛选：选中后所有数据请求带 ?owner_id=。 */
    $('owner-filter').addEventListener('change', onOwnerChange);

    $('refresh-btn').addEventListener('click', function () {
      if (!state.authed) { startSession(); return; }
      refreshAll();
    });

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

    /* 关标签页 / 刷新 / 退出登录：自动化没跑完就让浏览器弹原生确认 */
    window.addEventListener('beforeunload', function (ev) {
      if (!autoRunning()) { return undefined; }
      ev.preventDefault();
      ev.returnValue = '「一键自动化」还没跑完，离开页面会看不到进度（任务仍在后台运行）。';
      return ev.returnValue;
    });

    /* 一键自动化面板 */
    if ($('auto-close')) { $('auto-close').addEventListener('click', function () { closeAuto(false); }); }
    if ($('auto-reopen')) { $('auto-reopen').addEventListener('click', reopenAuto); }
    if ($('plog-open')) { $('plog-open').addEventListener('click', openPlatLogs); }
    if ($('plog-close')) { $('plog-close').addEventListener('click', closePlatLogs); }
    if ($('plog-refresh')) { $('plog-refresh').addEventListener('click', fetchPlatLogs); }
    if ($('auto-manual-save')) { $('auto-manual-save').addEventListener('click', saveManual); }
    if ($('auto-submit')) { $('auto-submit').addEventListener('click', submitAuto); }

    /* 大模型配置：保存 / 测试连接 / 清除 key（settings 页） */
    if ($('llm-save')) { $('llm-save').addEventListener('click', saveLlmConfig); }
    if ($('llm-test')) { $('llm-test').addEventListener('click', testLlmConnection); }
    if ($('llm-clear-key')) { $('llm-clear-key').addEventListener('click', clearLlmKey); }

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
    /* 作业抽屉：关闭按钮 / 点遮罩 / Esc。行内按钮与双击在渲染时挂好。 */
    $('drawer-close').addEventListener('click', closeDrawer);
    $('drawer-backdrop').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && state.drawer.open) { closeDrawer(); }
    });

    /* 账号页：抓取命令行的平台下拉（换了平台就重新生成命令）。 */
    $('cap-platform').addEventListener('change', function () { renderCaptureCmds(); });

    /* 工具页：动作清单 + 参数表单 + 结果 */
    $('actions-nav').addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button[data-action]') : null;
      if (!btn) { return; }
      var name = btn.getAttribute('data-action');
      var list = state.actions.list || [];
      for (var i = 0; i < list.length; i++) {
        if (list[i].name === name) {
          state.actions.active = list[i];
          state.actions.prefill = null;
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

    /* 用户页（仅超管）：新建 + 行内操作都在渲染时挂好，这里只接表单提交。 */
    $('user-create-form').addEventListener('submit', onCreateUser);
    /* 账号页：新增账号表单；每行的操作按钮在渲染时挂好。 */
    $('account-create-form').addEventListener('submit', onCreateAccount);

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
    renderAccounts();
    renderUsers();
    renderDrawer();
    renderCaptureCmds();
    renderLlm();
    applyRunConstraints();

    /* 默认停在登录页：有会话才由 enterConsole 打开控制台。 */
    $('app').hidden = true;
    $('login-screen').hidden = false;

    if (location.protocol === 'file:') {
      /* file:// 直开：不发任何请求，页面不崩，给出人话提示。 */
      $('login-screen').hidden = true;
      $('app').hidden = false;
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
      $('account-empty').hidden = false;
      $('account-empty').textContent = '尚未连接服务：file:// 直开读不到账号数据。';
      return;
    }

    /* 会话探测：GET /api/auth/me —— 有 cookie 就进控制台，401 就停在登录页。
       进控制台后：先取平台清单（用于校验 hash 里的平台），再取概览与当前页数据；
       超管额外取一次 /api/users（owner 下拉 + 「归属」列要用户名）。 */
    $('login-state').textContent = '正在检查登录状态 GET /api/auth/me …';
    startSession();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
