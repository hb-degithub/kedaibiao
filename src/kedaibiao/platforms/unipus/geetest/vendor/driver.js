/*
 * Geetest v3 载荷计算驱动（本项目原创，非 vendored 代码）
 *
 * 做一件事：把 jiyan.js 里的 get_encode_trace() / get_w() 暴露成
 * 「读 stdin 的 JSON → 写 stdout 的 JSON」，供 Python 侧 subprocess 调用。
 *
 * 为什么不翻译成 Python：jiyan.js 经过控制流平坦化 + 字符串表混淆，
 * 手工翻译的正确性无法保证；而它是 MIT 许可、可以原样 vendor。
 *
 * 输入：
 *   {"op":"encode_trace","trace":[[x,y,t],...],"s":"..."}
 *   {"op":"w","slide_length":N,"trace":[[x,y,t],...],"s":"...",
 *    "challenge":"<完整>","passtime":N,"imgload":N,"gt":"..."}
 * 输出：
 *   {"ok":true,"value":"..."}   或   {"ok":false,"error":"..."}
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CODE = fs.readFileSync(path.join(__dirname, 'jiyan.js'), 'utf8');

/** 极验脚本会摸一些浏览器全局，给最小可用的替身。 */
function makeSandbox() {
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    Date, Math, JSON, String, Number, Boolean, Array, Object, RegExp, Error,
    parseInt, parseFloat, isNaN, isFinite,
    encodeURIComponent, decodeURIComponent, escape, unescape,
    setTimeout, clearTimeout, setInterval, clearInterval,
    window: {}, document: {}, navigator: { userAgent: 'Mozilla/5.0' },
    location: { href: 'https://www.geetest.com/' },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return sandbox;
}

function loadGeetest() {
  const sandbox = makeSandbox();
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'jiyan.js', timeout: 20000 });
  if (typeof sandbox.get_w !== 'function') {
    throw new Error('jiyan.js 里没有 get_w —— vendor 的文件可能不完整');
  }
  if (typeof sandbox.get_encode_trace !== 'function') {
    throw new Error('jiyan.js 里没有 get_encode_trace');
  }
  return sandbox;
}

function main() {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { input += chunk; });
  process.stdin.on('end', () => {
    let out;
    try {
      const req = JSON.parse(input || '{}');
      const gt = loadGeetest();
      const trace = req.trace || [];
      const s = req.s || '';

      if (req.op === 'encode_trace') {
        out = { ok: true, value: gt.get_encode_trace(trace, s) };
      } else if (req.op === 'w') {
        const challenge = String(req.challenge || '');
        const encoded = gt.get_encode_trace(trace, s);
        // get_w(滑动距离, 编码后轨迹, 完整 challenge, challenge 前 32 位, 耗时, 图片加载耗时, gt)
        const w = gt.get_w(
          req.slide_length,
          encoded,
          challenge,
          challenge.slice(0, 32),
          req.passtime,
          req.imgload,
          req.gt
        );
        out = { ok: true, value: w, encoded_trace: encoded };
      } else {
        out = { ok: false, error: `未知 op: ${req.op}` };
      }
    } catch (err) {
      out = { ok: false, error: `${err && err.name ? err.name : 'Error'}: ${err && err.message ? err.message : String(err)}` };
    }
    process.stdout.write(JSON.stringify(out));
  });
}

main();
