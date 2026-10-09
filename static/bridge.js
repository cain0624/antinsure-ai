/* ================= AntFund AI · 浏览器端运行时桥 =================
 *
 * 作用：让同一份前端（app.js）既能在本地跑真 FastAPI 后端，也能在
 * GitHub Pages 这种「只能托管静态文件」的环境里独立运行。
 *
 * 做法：探测存在真实后端 → 走 http 分支；否则用 Pyodide 在浏览器里
 * 把 core/ 那套 Python 五层逻辑跑起来，直接把 /api/* 映射到 Python 函数。
 *
 * 对外契约（app.js 只依赖这两件事）：
 *   window.ANTFUND_RUNTIME.ready  → Promise<'http' | 'bridge'>
 *   window.ANTFUND_RUNTIME.call(path, body, method) → Promise<data>
 *
 * 必须同步挂载 window.ANTFUND_RUNTIME：app.js 是普通 script，
 * 解析到就会立即 init()，晚一步挂载就会掉进 http 分支拿 404。
 */
(function () {
  'use strict';

  var PYODIDE_VERSION = 'v0.26.4';
  // 主用 jsdelivr 的 pyodide 专用路径；失败换 npm 镜像（两者文件布局一致）
  var PYODIDE_BASES = [
    'https://cdn.jsdelivr.net/pyodide/' + PYODIDE_VERSION + '/full/',
    'https://cdn.jsdelivr.net/npm/pyodide@' + PYODIDE_VERSION.replace(/^v/, '') + '/',
  ];
  var PY_ROOT = '/app/py';
  var BOOT_TIMEOUT_MS = 120000;

  /* ---------------- 加载遮罩 ----------------
   * 只在确实要用浏览器内引擎时才插入：本地起 FastAPI 后端时不该闪过这个遮罩。
   * 样式随桥一起注入，避免为了一个遮罩去改 style.css（那样两处都要同步维护）。
   */
  var BOOT_CSS = [
    '#bootOverlay{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;',
    'justify-content:center;background:rgba(9,12,20,.92);backdrop-filter:blur(6px);',
    'transition:opacity .35s ease;font-family:inherit}',
    '#bootOverlay.boot-done{opacity:0;pointer-events:none}',
    '.boot-card{width:min(520px,88vw);padding:30px 32px;border-radius:16px;',
    'background:#141a26;border:1px solid #24304a;box-shadow:0 18px 50px rgba(0,0,0,.5);color:#e8eefc}',
    '.boot-title{font-size:17px;font-weight:600;letter-spacing:.2px;margin-bottom:6px}',
    '.boot-title span{font-size:12px;font-weight:400;color:#7f8ea8;margin-left:8px}',
    '.boot-msg{font-size:13px;color:#9fb0cc;margin:10px 0 14px;min-height:18px}',
    '.boot-bar{height:4px;border-radius:99px;background:#1d2740;overflow:hidden}',
    '.boot-bar i{display:block;height:100%;width:0;border-radius:99px;',
    'background:linear-gradient(90deg,#4c8dff,#6ad0a8);transition:width .3s ease}',
    '.boot-hint{font-size:11.5px;color:#63718c;margin-top:14px;line-height:1.7}',
    '.boot-detail{font-size:11px;color:#ffaeaa;background:#1b1216;border:1px solid #3a2126;',
    'border-radius:8px;padding:8px 10px;margin:10px 0 0;white-space:pre-wrap;word-break:break-all}',
    '.boot-error .boot-title{color:#ff9d97}',
    'code{background:#1d2740;padding:1px 5px;border-radius:4px;font-size:11px}',
  ].join('');

  var _p = 0;
  function mountOverlay() {
    if (!document.body) {   // 极端情况下脚本早于 body 执行
      document.addEventListener('DOMContentLoaded', mountOverlay, { once: true });
      return;
    }
    if (document.getElementById('bootOverlay')) return;
    var st = document.createElement('style');
    st.textContent = BOOT_CSS;
    document.head.appendChild(st);
    var overlay = document.createElement('div');
    overlay.id = 'bootOverlay';
    overlay.innerHTML =
      '<div class="boot-card">' +
      '<div class="boot-title">AntFund AI <span>五层 Multi-Agent</span></div>' +
      '<div class="boot-msg" id="bootMsg">检测运行环境…</div>' +
      '<div class="boot-bar"><i id="bootBar"></i></div>' +
      '<div class="boot-hint" id="bootHint">纯静态部署：Python 运行时在浏览器内启动，首次约需数秒</div>' +
      '</div>';
    document.body.appendChild(overlay);
  }
  function setProgress(pct, msg) {
    _p = Math.max(_p, pct);
    var bar = document.getElementById('bootBar');
    var m = document.getElementById('bootMsg');
    if (bar) bar.style.width = _p + '%';
    if (m && msg) m.textContent = msg;
  }
  function hideOverlay() {
    var el = document.getElementById('bootOverlay');
    if (!el) return;
    el.classList.add('boot-done');
    setTimeout(function () { el.remove(); }, 400);
  }
  function fail(msg, detail) {
    mountOverlay();
    var el = document.getElementById('bootOverlay');
    if (!el) return;
    el.innerHTML =
      '<div class="boot-card boot-error">' +
      '<div class="boot-title">引擎启动失败</div>' +
      '<div class="boot-msg">' + msg + '</div>' +
      (detail ? '<pre class="boot-detail">' + String(detail).slice(0, 600) + '</pre>' : '') +
      '<div class="boot-hint">本页是纯静态版本，Python 运行时需要从 CDN 下载。<br>' +
      '若网络受限，可在本地启动后端后访问：<code>python3 -m http.server 8895</code></div>' +
      '</div>';
  }

  /* ---------------- 工具 ---------------- */
  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = resolve;
      s.onerror = function () { reject(new Error('无法加载 ' + src)); };
      document.head.appendChild(s);
    });
  }

  function withTimeout(promise, ms, what) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () { reject(new Error(what + ' 超时（' + Math.round(ms / 1000) + 's）')); }, ms);
      promise.then(function (v) { clearTimeout(t); resolve(v); },
                   function (e) { clearTimeout(t); reject(e); });
    });
  }

  /* ---------------- 探测：有没有真实后端？ ---------------- */
  async function detectBackend() {
    try {
      var res = await fetch('./api/config', { method: 'GET', cache: 'no-store' });
      if (!res.ok) return false;
      var ct = res.headers.get('content-type') || '';
      if (ct.indexOf('application/json') < 0) return false;
      await res.json();           // 确认真的是配置体，而不是被 SPA 兜底吐回的 index.html
      return true;
    } catch (e) {
      return false;
    }
  }

  /* ---------------- Pyodide 启动 ---------------- */
  async function bootPyodide() {
    mountOverlay();
    setProgress(12, '加载 Python 运行时…');
    var base = null, lastErr = null;
    for (var i = 0; i < PYODIDE_BASES.length; i++) {
      try {
        await loadScript(PYODIDE_BASES[i] + 'pyodide.js');
        base = PYODIDE_BASES[i];
        break;
      } catch (e) { lastErr = e; }
    }
    if (!base) throw new Error('Pyodide 脚本加载失败：' + (lastErr && lastErr.message));

    setProgress(25, '初始化 Python 解释器…');
    var pyodide = await withTimeout(
      window.loadPyodide({ indexURL: base }), BOOT_TIMEOUT_MS, 'Python 解释器初始化');
    setProgress(55, '装载 Multi-Agent 引擎…');

    // 文件清单由 deploy.sh 生成，避免文件增删后两边漂移
    var manifestRes = await fetch('./py-manifest.json?v=antfund-1', { cache: 'no-store' });
    if (!manifestRes.ok) throw new Error('缺少 py-manifest.json（请用 deploy.sh 生成发布目录）');
    var manifest = await manifestRes.json();

    var FS = pyodide.FS;
    var enc = new TextEncoder();
    var loaded = 0;
    for (var j = 0; j < manifest.length; j++) {
      var rel = manifest[j];                       // 形如 "py/core/agents.py"
      var url = './' + rel + '?v=antfund-1';
      var r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) throw new Error('取不到 ' + url + '（' + r.status + '）');
      var text = await r.text();
      var abs = '/app/' + rel;
      var parts = abs.split('/');
      parts.pop();
      FS.mkdirTree(parts.join('/'));
      FS.writeFile(abs, enc.encode(text));
      loaded++;
      setProgress(55 + Math.round((loaded / manifest.length) * 30),
                  '装载引擎模块 ' + loaded + '/' + manifest.length);
    }

    setProgress(88, '启动五层链路…');
    // _bridge_call 的 __file__ 决定 BASE，故必须先落到 /app/py 再 import
    pyodide.runPython(
      "import sys, os\n" +
      "sys.path.insert(0, '" + PY_ROOT + "')\n" +
      "os.chdir('" + PY_ROOT + "')\n" +
      "import api_facade\n" +
      "from api_facade import _bridge_call\n" +
      "print(api_facade._warmup())\n"
    );

    var callFn = pyodide.globals.get('_bridge_call');
    setProgress(100, '就绪');
    return {
      pyodide: pyodide,
      call: function (path, body, method) {
        var raw = callFn(path, body === undefined || body === null ? '' : JSON.stringify(body),
                         method || 'POST');
        var env;
        try {
          env = JSON.parse(raw);
        } catch (e) {
          throw new Error('引擎返回的不是 JSON：' + String(raw).slice(0, 200));
        }
        if (!env.ok) {
          var err = new Error(env.error || '引擎调用失败');
          err.kind = env.kind || 'error';
          throw err;
        }
        return env.data;
      },
    };
  }

  /* ---------------- 装配 ---------------- */
  var resolveReady;
  var readyPromise = new Promise(function (r) { resolveReady = r; });

  var runtime = {
    mode: null,
    ready: readyPromise,
    pyodide: null,
    call: function () {
      return Promise.reject(new Error('运行时尚未就绪'));
    },
  };
  // 同步挂载：app.js 解析后立刻会调用 api()
  window.ANTFUND_RUNTIME = runtime;

  (async function main() {
    var hasBackend = await detectBackend();
    if (hasBackend) {
      runtime.mode = 'http';
      resolveReady('http');
      console.log('[AntFund] 检测到本地后端，走 FastAPI 模式');
      return;
    }
    try {
      var bridge = await bootPyodide();
      runtime.mode = 'bridge';
      runtime.pyodide = bridge.pyodide;
      runtime.call = function (path, body, method) {
        try {
          return Promise.resolve(bridge.call(path, body, method));
        } catch (e) {
          return Promise.reject(e);
        }
      };
      resolveReady('bridge');
      hideOverlay();
      console.log('[AntFund] 纯静态模式：Python 引擎已在浏览器内启动');
    } catch (e) {
      console.error('[AntFund] 引擎启动失败', e);
      fail('无法在浏览器内启动 Python 引擎。', e && e.message);
      // 让已挂起的 init() 明确失败，而不是永远转圈
      resolveReady('bridge');
      runtime.call = function () { return Promise.reject(e); };
    }
  })();
})();
