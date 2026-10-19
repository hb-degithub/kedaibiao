/* 课代表 kedaibiao · Web 控制台前端
 *
 * 约束（见 docs/web-console-api.md，本文件唯一的接口契约来源）：
 *   - 零构建、零依赖、零 CDN：只用原生 DOM API 与同源 fetch，页面不外发任何数据。
 *   - 默认干跑：live 开关默认关闭，打开需二次确认；capabilities.submit=false 时禁用。
 *   - 任务运行中每 1 秒轮询 /api/jobs/<id>，结束即停；pagehide/切走不留悬挂定时器。
 *   - 所有后端文本一律用 textContent 写入，绝不拼接 HTML；界面不展示任何凭据字段。
 */
(function () {
  'use strict';

  var POLL_MS = 1000;      // 契约：运行中每 1 秒轮询一次
  var POLL_MAX_FAILS = 5;  // 连续失败达到该次数后停止轮询并报错
  var EVENT_LIMIT = 100;
  var LIVE_CONFIRM =
    '这会真的提交作业，不可撤销。\n\n' +
    '确定要把「真提交（live）」打开吗？\n' +
    '建议先用干跑确认规划结果（哪些作业会被处理），再决定是否真提交。';

  var state = {
    overview: null,
    platforms: [],
    jobs: [],
    runs: [],
    submissions: [],
    bank: null,
    currentId: null,     // 正在跟踪的 job_id
    currentJob: null,
    pollTimer: null,
    pollFails: 0,
    eventsOff: false,
    submitting: false,
    problems: {}         // key -> { message, level }
  };

  /* ------------------------------------------------------------------ 基础工具 */

  function $(id) { return document.getElementById(id); }

  function setText(id, value) {
    var node = $(id);
    if (!node) { return; }
    node.textContent = (value === null || value === undefined || value === '') ? '—' : String(value);
  }

  function td(text, cls) {
    var cell = document.createElement('td');
    if (cls) { cell.className = cls; }
    cell.textContent = (text === null || text === undefined || text === '') ? '—' : String(text);
    return cell;
  }

  function tr(cells) {
    var row = document.createElement('tr');
    for (var i = 0; i < cells.length; i++) { row.appendChild(cells[i]); }
    return row;
  }

  /* 统一填充表格：无数据时隐藏整张表并显示空状态，绝不渲染空表。 */
  function fillTable(tableId, bodyId, emptyId, rows) {
    var table = $(tableId), body = $(bodyId), empty = $(emptyId);
    if (body) { body.textContent = ''; }
    if (!rows || !rows.length) {
      if (table) { table.hidden = true; }
      if (empty) { empty.hidden = false; }
      return;
    }
    for (var i = 0; i < rows.length; i++) { body.appendChild(rows[i]); }
    if (table) { table.hidden = false; }
    if (empty) { empty.hidden = true; }
  }

  function badge(text, cls) {
    var span = document.createElement('span');
    span.className = 'badge ' + (cls || 'badge-idle');
    span.textContent = text;
    return span;
  }

  function tdBadge(text, cls) {
    var cell = document.createElement('td');
    cell.appendChild(badge(text, cls));
    return cell;
  }

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function fmtNum(v) {
    if (v === null || v === undefined || v === '') { return '—'; }
    if (!isNum(v)) { return String(v); }
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

  /* 契约时间格式为无时区的 ISO 8601（2026-10-19T11:20:00），只做本地展示替换。 */
  function fmtTime(v) {
    if (!v) { return '—'; }
    var s = String(v).replace('T', ' ');
    if (s.charAt(s.length - 1) === 'Z') { s = s.slice(0, -1); }
    return s;
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

  /* 契约：job.status 只有 running/succeeded/failed；
     run.status 另有 pending/skipped —— 两套枚举各自上色，不做互相映射。 */
  var JOB_STATUS = {
    running:  { text: '运行中', cls: 'badge-run' },
    succeeded:{ text: '成功',   cls: 'badge-ok' },
    failed:   { text: '失败',   cls: 'badge-fail' },
    pending:  { text: '待处理', cls: 'badge-run' },
    skipped:  { text: '跳过',   cls: 'badge-idle' }
  };

  function statusSpec(status) {
    return JOB_STATUS[status] || { text: status || '未知', cls: 'badge-idle' };
  }

  function liveBadge(live) {
    return live ? badge('真提交', 'badge-live') : badge('干跑', 'badge-dry');
  }

  function tdLive(live) {
    var cell = document.createElement('td');
    cell.appendChild(liveBadge(live));
    return cell;
  }

  function truncate(s, n) {
    s = (s === null || s === undefined) ? '' : String(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  /* ------------------------------------------------------------------ 请求层 */

  function HttpError(kind, message, status, path) {
    this.name = 'HttpError';
    this.kind = kind;
    this.error = message;
    this.status = status;
    this.path = path;
  }

  function api(path, options) {
    return fetch(path, options || {}).then(function (res) {
      var ct = res.headers.get('content-type') || '';
      var bodyPromise = ct.indexOf('json') >= 0
        ? res.json().catch(function () { return null; })
        : Promise.resolve(null);
      return bodyPromise.then(function (body) {
        if (!res.ok) {
          var kind = (body && body.kind) || ('http_' + res.status);
          var msg = (body && body.error) || '';
          throw new HttpError(kind, msg, res.status, path);
        }
        return body;
      });
    }, function (err) {
      throw new HttpError('network', String((err && err.message) || err), 0, path);
    });
  }

  /* 契约：失败响应的 error 字段就是可直接展示的中文，前端不重写它；
     只在后面补一句「接下来能做什么」。认不出的 kind 就原样显示 error。 */
  var ERROR_HINTS = {
    no_credentials: '请在 .env 中补全该平台的账号配置，重启服务后再试。',
    unknown_platform: '请点右上角「刷新」重新读取平台列表，并确认该适配器已注册。',
    busy: '同一时刻只允许一个跑批任务，请等当前任务结束后再触发。',
    bad_request: '请检查 platform / live / limit / kinds 的取值。',
    not_found: '目标不存在或已被清理。',
    internal: '请查看服务端日志。'
  };

  function describeError(e, ctx) {
    var kind = e && e.kind;
    if (kind === 'network') {
      return '无法连接本地控制台服务（' + ((e && e.error) || '请求失败') +
             '）。请确认服务已启动，并通过 http://127.0.0.1:<端口> 访问本页（file:// 直开读不到接口）。';
    }
    var detail = ((e && e.error) || '').trim();
    var base = detail || ('请求失败' + (e && e.status ? '（HTTP ' + e.status + '）' : ''));
    var platform = (ctx && ctx.platform) || '';
    if (platform && (kind === 'no_credentials' || kind === 'unknown_platform') &&
        base.indexOf(platform) === -1) {
      base = '平台「' + platform + '」：' + base;
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

  function renderProblems() {
    var banner = $('banner');
    var keys = Object.keys(state.problems);
    if (!keys.length) { banner.hidden = true; banner.textContent = ''; return; }
    var worst = 'warn';
    for (var i = 0; i < keys.length; i++) {
      if (state.problems[keys[i]].level === 'error') { worst = 'error'; break; }
    }
    banner.textContent = '';
    banner.className = 'banner banner-' + worst;
    var title = document.createElement('div');
    title.className = 'banner-title';
    title.textContent = worst === 'error' ? '出错了' : '注意';
    banner.appendChild(title);
    var ul = document.createElement('ul');
    for (var j = 0; j < keys.length; j++) {
      var li = document.createElement('li');
      li.textContent = state.problems[keys[j]].message;
      ul.appendChild(li);
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

  /* ------------------------------------------------------------------ 概览 */

  function renderOverview() {
    var o = state.overview || {};
    var bank = o.bank || {};
    var runs = o.runs || {};

    setText('meta-version', o.version);
    setText('meta-env', o.env);
    setText('meta-python', o.python);

    var mode = $('meta-mode');
    if (o.dry_run === false) {
      mode.className = 'badge badge-live';
      mode.textContent = '真提交环境';
      mode.title = 'dry_run = false：跑批默认仍是干跑，只有显式 live=true 才提交';
    } else if (o.dry_run === true) {
      mode.className = 'badge badge-dry';
      mode.textContent = '干跑';
      mode.title = 'dry_run = true：默认只规划不提交';
    } else {
      mode.className = 'badge badge-idle';
      mode.textContent = '—';
    }

    setText('ov-platforms', fmtNum(o.platform_count));
    var creds = o.credentialed_platforms || [];
    $('ov-platforms-sub').textContent = creds.length
      ? '已配凭据：' + creds.join('、')
      : '没有任何平台配置了凭据';

    setText('ov-bank', fmtNum(bank.total));
    $('ov-bank-sub').textContent =
      '已判分 ' + fmtNum(bank.graded) + ' · 正确 ' + fmtNum(bank.correct) +
      ' · 准确率 ' + fmtPct(bank.accuracy) + ' · 未判分 ' + fmtNum(bank.ungraded);

    setText('ov-runs', fmtNum(runs.total));
    $('ov-runs-sub').textContent =
      '成功 ' + fmtNum(runs.succeeded) + ' · 失败 ' + fmtNum(runs.failed) +
      ' · 跳过 ' + fmtNum(runs.skipped);

    setText('ov-runtime', o.max_concurrency === undefined ? null : '并发 ' + fmtNum(o.max_concurrency));
    var deps = o.optional_deps || {};
    $('ov-runtime-sub').textContent =
      'headless ' + (o.headless === true ? '是' : o.headless === false ? '否' : '—') +
      ' · 最低置信度 ' + fmtNum(o.min_confidence) +
      ' · playwright ' + (deps.playwright ? '可用' : '不可用') +
      ' · fastapi ' + (deps.fastapi ? '可用' : '不可用') +
      ' · uvicorn ' + (deps.uvicorn ? '可用' : '不可用');

    var env = $('env-list');
    env.textContent = '';
    var pairs = [
      ['repo_root', o.repo_root],
      ['db_path', o.db_path],
      ['db_exists', o.db_exists === undefined ? null : (o.db_exists ? '是' : '否')],
      ['env', o.env],
      ['python', o.python]
    ];
    for (var i = 0; i < pairs.length; i++) {
      var wrap = document.createElement('div');
      var dt = document.createElement('dt');
      dt.textContent = pairs[i][0];
      var dd = document.createElement('dd');
      dd.textContent = (pairs[i][1] === null || pairs[i][1] === undefined || pairs[i][1] === '') ? '—' : String(pairs[i][1]);
      wrap.appendChild(dt);
      wrap.appendChild(dd);
      env.appendChild(wrap);
    }
    env.hidden = false;
  }

  function loadOverview() {
    return api('/api/overview').then(function (body) {
      state.overview = body || {};
      renderOverview();
      clearProblem('overview');
      setConn(true, '已连接');
    }).catch(function (e) {
      setConn(false, '未连接');
      setProblem('overview', describeError(e), e.kind === 'network' ? 'error' : 'warn');
    });
  }

  /* ------------------------------------------------------------------ 平台 */

  function platformByName(name) {
    for (var i = 0; i < state.platforms.length; i++) {
      if (state.platforms[i].name === name) { return state.platforms[i]; }
    }
    return null;
  }

  function canSubmit(platform) {
    return !!(platform && platform.capabilities && platform.capabilities.submit === true);
  }

  function capCell(ok) {
    var cell = document.createElement('td');
    cell.className = 'ctr';
    var span = document.createElement('span');
    span.className = 'cap ' + (ok ? 'cap-yes' : 'cap-no');
    span.textContent = ok ? '✓' : '—';
    span.title = ok ? '支持' : '不支持';
    cell.appendChild(span);
    return cell;
  }

  function renderPlatforms() {
    var list = state.platforms;
    var rows = list.map(function (p) {
      var caps = p.capabilities || {};
      var actions = document.createElement('td');
      actions.className = 'ctr';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-sm';
      btn.textContent = '跑一次';
      btn.title = '干跑 ' + p.name + '（live=false，全部类型，不限量）';
      btn.addEventListener('click', function () { quickDryRun(p.name); });
      actions.appendChild(btn);

      var credCell = document.createElement('td');
      credCell.appendChild(p.has_credentials
        ? badge('已配置', 'badge-ok')
        : badge('未配置', 'badge-warn'));

      return tr([
        td(p.name, 'mono'),
        td(p.display_name || p.name),
        capCell(caps.login === true),
        capCell(caps.list_courses === true),
        capCell(caps.list_assignments === true),
        capCell(caps.fetch_questions === true),
        capCell(caps.submit === true),
        capCell(caps.course_playback === true),
        credCell,
        actions
      ]);
    });
    fillTable('platforms-table', 'platforms-body', 'platforms-empty', rows);
    if (!rows.length) {
      $('platforms-empty').textContent =
        '没有任何已注册的平台。请检查适配器是否注册（platforms/ 下的 register），然后点「刷新」。';
    }
  }

  function loadPlatforms() {
    return api('/api/platforms').then(function (body) {
      state.platforms = (body && body.platforms) || [];
      renderPlatforms();
      renderPlatformOptions();
      applyPlatformConstraints();
      clearProblem('platforms');
    }).catch(function (e) {
      state.platforms = [];
      renderPlatforms();
      renderPlatformOptions();
      applyPlatformConstraints();
      setProblem('platforms', describeError(e), 'error');
    });
  }

  function renderPlatformOptions() {
    var sel = $('run-platform');
    var prev = sel.value;
    sel.textContent = '';
    if (!state.platforms.length) {
      var opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '（没有可用平台）';
      sel.appendChild(opt);
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    for (var i = 0; i < state.platforms.length; i++) {
      var p = state.platforms[i];
      var o = document.createElement('option');
      o.value = p.name;
      o.textContent = p.display_name ? (p.name + ' · ' + p.display_name) : p.name;
      o.title = canSubmit(p) ? '' : '该平台本阶段不支持提交';
      sel.appendChild(o);
    }
    var keep = false;
    for (var j = 0; j < sel.options.length; j++) {
      if (sel.options[j].value === prev) { keep = true; break; }
    }
    sel.value = keep ? prev : sel.options[0].value;
  }

  /* 契约要求：capabilities.submit === false 时禁用 live 开关并说明原因。 */
  function applyPlatformConstraints() {
    var live = $('run-live');
    var hint = $('run-live-hint');
    var p = platformByName($('run-platform').value);
    var submitOk = canSubmit(p);

    live.disabled = !submitOk || !p;
    if (!submitOk) {
      if (live.checked) { live.checked = false; }
      hint.hidden = false;
      hint.textContent = p
        ? ('该平台本阶段不支持提交（capabilities.submit = false），只能干跑：跑批不会向平台提交任何内容。')
        : '尚未选择平台。';
    } else if (p && p.has_credentials === false) {
      hint.hidden = false;
      hint.textContent = '提示：该平台尚未配置凭据，跑批会返回 no_credentials，需要先在 .env 中补全。';
    } else {
      hint.hidden = true;
      hint.textContent = '';
    }
    updateRunButton();
  }

  function updateRunButton() {
    var btn = $('run-submit');
    var live = $('run-live').checked;
    btn.classList.toggle('is-live', live);
    btn.textContent = state.submitting
      ? '触发中…'
      : (live ? '开始跑批（真提交 · 不可撤销）' : '开始跑批（干跑）');
    btn.disabled = state.submitting || $('run-platform').disabled;

    var liveBadgeEl = $('live-badge');
    if (live) {
      liveBadgeEl.className = 'badge badge-live';
      liveBadgeEl.textContent = '当前：真提交';
    } else {
      liveBadgeEl.className = 'badge badge-dry';
      liveBadgeEl.textContent = '当前：干跑';
    }
    $('run-state').textContent = state.submitting ? '正在请求 /api/run …' : '';
  }

  function checkedKinds() {
    var nodes = document.querySelectorAll('#run-form input[name="kind"]:checked');
    var out = [];
    for (var i = 0; i < nodes.length; i++) { out.push(nodes[i].value); }
    return out;
  }

  /* ------------------------------------------------------------------ 触发跑批 */

  function buildRunPayload(platform, live, limit, kinds) {
    /* 契约字段固定为这四个，不额外发明字段。 */
    return { platform: platform, live: live, limit: limit, kinds: kinds };
  }

  function submitRun(payload) {
    return api('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      cache: 'no-store'
    });
  }

  function onRunSubmit(ev) {
    ev.preventDefault();
    if (state.submitting) { return; }

    var platform = $('run-platform').value;
    if (!platform) {
      setProblem('run', '请先选择平台。', 'error');
      return;
    }
    var live = $('run-live').checked;
    if (live && !canSubmit(platformByName(platform))) {
      setProblem('run', '平台「' + platform + '」不支持提交（capabilities.submit = false），无法真提交，只能干跑。', 'error');
      return;
    }
    if (live && !window.confirm('这会真的提交作业，不可撤销。\n\n确认以「真提交」模式跑一遍 ' + platform + '？')) {
      return;
    }

    var limit = parseInt($('run-limit').value, 10);
    if (!isFinite(limit) || limit < 0) { limit = 0; }
    var payload = buildRunPayload(platform, live, limit, checkedKinds());

    state.submitting = true;
    updateRunButton();
    clearProblem('run');

    submitRun(payload).then(function (body) {
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
    }).then(function () {
      state.submitting = false;
      updateRunButton();
    });
  }

  function quickDryRun(platform) {
    if (state.submitting) { return; }
    $('run-platform').value = platform;
    applyPlatformConstraints();
    if ($('run-live').checked) { $('run-live').checked = false; updateRunButton(); }
    state.submitting = true;
    updateRunButton();
    clearProblem('run');
    var payload = buildRunPayload(platform, false, 0, []);
    submitRun(payload).then(function (body) {
      var jobId = body && body.job_id;
      showToast('已触发 ' + platform + ' 干跑（live=false，全部类型，不限量）' + (jobId ? '：' + jobId : ''));
      if (jobId) { trackJob(jobId); }
      loadJobs();
    }).catch(function (e) {
      setProblem('run', describeError(e, { platform: platform }),
                 e.kind === 'busy' ? 'warn' : 'error');
      if (e.kind === 'busy') { loadJobs().then(resumeRunningJob); }
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
      var job = body || {};
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
        loadEvents();
        loadJobs();
        loadOverview();
        loadRuns();
        loadSubmissions();
        loadBank();
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
    liveCell.textContent = '';
    liveCell.appendChild(liveBadge(job.live === true));

    var spec = statusSpec(job.status);
    var statusCell = $('job-status');
    statusCell.textContent = '';
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
    counts.textContent = '';
    var order = ['succeeded', 'failed', 'skipped', 'running', 'pending'];
    var shown = [];
    for (var k = 0; k < order.length; k++) {
      if (tally[order[k]]) { shown.push(order[k]); }
    }
    for (var key in tally) {
      if (order.indexOf(key) === -1) { shown.push(key); }
    }
    if (!shown.length) {
      var span = document.createElement('span');
      span.className = 'muted';
      span.textContent = '暂无记录明细' +
        (job.status === 'running' ? '（任务刚开始或尚未产出记录）' : '');
      counts.appendChild(span);
    } else {
      for (var m = 0; m < shown.length; m++) {
        var item = document.createElement('span');
        item.className = 'stat';
        var label = document.createElement('span');
        label.className = 'stat-label';
        label.textContent = statusSpec(shown[m]).text;
        var value = document.createElement('span');
        value.className = 'stat-value mono';
        value.textContent = String(tally[shown[m]]);
        item.appendChild(label);
        item.appendChild(value);
        counts.appendChild(item);
      }
    }
    $('job-records-count').textContent = records.length ? ('共 ' + records.length + ' 条') : '';

    var rows = records.map(function (rec) {
      var spec2 = statusSpec(rec.status);
      return tr([
        td(rec.assignment_id, 'mono'),
        tdBadge(spec2.text, spec2.cls),
        td(rec.note, 'wrap')
      ]);
    });
    fillTable('job-records-table', 'job-records-body', 'job-records-empty', rows);
    if (!rows.length) {
      $('job-records-empty').textContent = job.status === 'running'
        ? '任务还在运行，暂时没有产出记录。'
        : '该任务没有产生任何记录。';
    }
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

  /* ------------------------------------------------------------------ 列表区 */

  function loadJobs() {
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

  function renderJobs() {
    var rows = state.jobs.map(function (job) {
      var spec = statusSpec(job.status);
      var actions = document.createElement('td');
      actions.className = 'ctr';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-sm';
      btn.textContent = job.status === 'running' ? '跟踪' : '查看';
      btn.addEventListener('click', function () { viewJob(job.job_id); });
      actions.appendChild(btn);
      return tr([
        td(job.job_id, 'mono'),
        td(job.platform, 'mono'),
        tdLive(job.live === true),
        tdBadge(spec.text, spec.cls),
        td(fmtTime(job.started_at), 'mono'),
        td(fmtTime(job.finished_at), 'mono'),
        td(job.summary, 'wrap'),
        td(job.error, 'err')
      ].concat([actions]));
    });
    $('jobs-count').textContent = state.jobs.length ? ('共 ' + state.jobs.length + ' 个') : '';
    fillTable('jobs-table', 'jobs-body', 'jobs-empty', rows);
  }

  function viewJob(jobId) {
    clearProblem('job');
    api('/api/jobs/' + encodeURIComponent(jobId)).then(function (body) {
      var job = body || {};
      renderCurrentJob(job);
      if (job.status === 'running') {
        trackJob(jobId);
      } else {
        cancelTimer();
        state.currentId = null;
        $('job-poll').textContent = '任务已结束（' + statusSpec(job.status).text + '）';
        $('job-body').scrollIntoView({ block: 'nearest' });
      }
    }).catch(function (e) {
      setProblem('job', describeError(e), 'error');
    });
  }

  function loadRuns() {
    return api('/api/runs?limit=50').then(function (body) {
      state.runs = (body && body.runs) || [];
      renderRuns();
      clearProblem('runs');
    }).catch(function (e) {
      setProblem('runs', '跑批历史加载失败：' + describeError(e), 'error');
    });
  }

  function renderRuns() {
    var rows = state.runs.map(function (r) {
      var spec = statusSpec(r.status);
      var detailText = '';
      if (r.detail && typeof r.detail === 'object' && Object.keys(r.detail).length) {
        try { detailText = JSON.stringify(r.detail); } catch (e) { detailText = ''; }
      }
      return tr([
        td(r.id, 'mono'),
        td(r.platform, 'mono'),
        td(r.assignment_id, 'mono'),
        tdBadge(spec.text, spec.cls),
        td(fmtNum(r.attempts), 'num mono'),
        td(fmtTime(r.started_at), 'mono'),
        td(fmtTime(r.finished_at), 'mono'),
        td(r.error, 'err'),
        td(truncate(detailText, 120), 'wrap muted mono')
      ]);
    });
    $('runs-count').textContent = state.runs.length ? ('共 ' + state.runs.length + ' 条') : '';
    fillTable('runs-table', 'runs-body', 'runs-empty', rows);
  }

  function loadSubmissions() {
    return api('/api/submissions?limit=50').then(function (body) {
      state.submissions = (body && body.submissions) || [];
      renderSubmissions();
      clearProblem('subs');
    }).catch(function (e) {
      setProblem('subs', '提交记录加载失败：' + describeError(e), 'error');
    });
  }

  function renderSubmissions() {
    var rows = state.submissions.map(function (s) {
      return tr([
        td(s.id, 'mono'),
        td(s.platform, 'mono'),
        td(s.assignment_id, 'mono'),
        tdBadge(s.ok === true ? '成功' : '失败', s.ok === true ? 'badge-ok' : 'badge-fail'),
        td(fmtScore(s.score), 'num mono'),
        td(s.message, 'wrap'),
        td(fmtTime(s.created_at), 'mono')
      ]);
    });
    $('subs-count').textContent = state.submissions.length ? ('共 ' + state.submissions.length + ' 条') : '';
    fillTable('subs-table', 'subs-body', 'subs-empty', rows);
  }

  function loadBank() {
    return api('/api/bank').then(function (body) {
      /* 契约：打平对象 {total, graded, correct, accuracy, ungraded, by_source, db_path}。 */
      var bank = body || {};
      state.bank = bank;
      renderBank(bank, bank.db_path);
      clearProblem('bank');
    }).catch(function (e) {
      setProblem('bank', '题库统计加载失败：' + describeError(e), 'error');
    });
  }

  function renderBank(bank, dbPath) {
    var total = bank.total;
    $('bank-hint').textContent = isNum(total) ? ('共 ' + total + ' 题') : '';

    var isEmpty = isNum(total) && total === 0;
    $('bank-empty').hidden = !isEmpty;
    $('bank-body').hidden = isEmpty;
    if (isEmpty) { return; }

    setText('bank-total', fmtNum(bank.total));
    setText('bank-graded', fmtNum(bank.graded));
    setText('bank-correct', fmtNum(bank.correct));
    setText('bank-accuracy', fmtPct(bank.accuracy));
    setText('bank-ungraded', fmtNum(bank.ungraded));
    setText('bank-dbpath', dbPath);

    var bySource = bank.by_source || {};
    var keys = Object.keys(bySource);
    var rows = keys.map(function (k) {
      return tr([td(k, 'mono'), td(fmtNum(bySource[k]), 'num mono')]);
    });
    fillTable('bank-source-table', 'bank-source-body', 'bank-source-empty', rows);
  }

  /* ------------------------------------------------------------------ 事件流 */

  function loadEvents() {
    if (state.eventsOff) { return Promise.resolve(); }
    return api('/api/events?limit=' + EVENT_LIMIT).then(function (body) {
      var events = (body && body.events) || [];
      renderEvents(events);
      clearProblem('events');
    }).catch(function (e) {
      /* 正常路径失败：说清楚，并暂停 1 秒一次的重试，避免每秒刷错误。
         点「刷新」会重置。 */
      state.eventsOff = true;
      $('events-hint').textContent = '已暂停轮询';
      setProblem('events', '事件流读取失败：' + describeError(e) + '（已暂停轮询，点「刷新」重试）', 'warn');
    });
  }

  function renderEvents(events) {
    var list = $('events-list');
    list.hidden = false;
    list.textContent = '';
    var recent = events.slice(-EVENT_LIMIT).reverse();
    if (!recent.length) {
      $('events-empty').hidden = false;
      return;
    }
    $('events-empty').hidden = true;
    for (var i = 0; i < recent.length; i++) {
      var ev = recent[i] || {};
      var li = document.createElement('li');
      var at = document.createElement('span');
      at.className = 'ev-at mono';
      at.textContent = fmtTime(ev.at);
      var name = document.createElement('span');
      name.className = 'ev-name mono';
      name.textContent = ev.name || '—';
      var payload = document.createElement('span');
      payload.className = 'ev-payload mono';
      var raw = '';
      if (ev.payload && typeof ev.payload === 'object') {
        try { raw = JSON.stringify(ev.payload); } catch (err) { raw = ''; }
      } else if (ev.payload !== undefined && ev.payload !== null) {
        raw = String(ev.payload);
      }
      payload.textContent = truncate(raw, 160);
      payload.title = truncate(raw, 500);
      li.appendChild(at);
      li.appendChild(name);
      li.appendChild(payload);
      list.appendChild(li);
    }
  }

  /* ------------------------------------------------------------------ 启动 */

  function refreshAll() {
    return Promise.all([
      loadOverview(),
      loadPlatforms().then(function () { return loadJobs(); }),
      loadRuns(),
      loadSubmissions(),
      loadBank(),
      loadEvents()
    ]);
  }

  function bindEvents() {
    $('refresh-btn').addEventListener('click', function () {
      clearProblem('events');
      state.eventsOff = false;
      $('events-hint').textContent = '每 1 秒轮询';
      refreshAll();
    });

    $('run-form').addEventListener('submit', onRunSubmit);
    $('run-platform').addEventListener('change', applyPlatformConstraints);

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
    });

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
    updateRunButton();

    if (location.protocol === 'file:') {
      /* file:// 直开：不发任何请求，页面不崩，给出人话提示。 */
      $('job-empty').textContent = '尚未连接服务：file:// 直开无法访问本地接口。';
      return;
    }
    refreshAll();
    /* 初始加载一次全量数据；此后只有「有任务在跑」时才存在定时器：
       每秒拉一次 /api/jobs/<id>，顺带拉一次事件流，任务结束即全部停掉。 */
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
