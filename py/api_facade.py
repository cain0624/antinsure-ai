"""浏览器端 API 门面 —— server.py 的路由逻辑的纯函数等价物。

Pyodide 里没有 HTTP 栈，`bridge.js` 把前端的 `api(path, body, method)` 调用
直接转成 `_bridge_call(path, body_json, method)`。

设计原则：**逐字对齐 server.py 的响应结构**。
任何一侧改了返回字段，另一侧必须同步，否则「本地 FastAPI 版」与
「线上纯静态版」会行为分叉，而这种分叉在浏览器里很难被发现。
`tools/parity_check.py` 会用同一批输入对拍两侧，防止漂移。
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import parse_qs, urlparse

from core.harness import (
    ALL_TOOLS,
    PROMPT_VERSIONS,
    STATES,
    TOOL_REGISTRY,
    Harness,
    Orchestrator,
)
from core.llm import ROUTES
from core.trace import TraceStore

BASE = os.path.dirname(os.path.abspath(__file__))

# 与 server.py 一致：模块级单例，会话状态跨请求保留
_store = TraceStore()
_harness = Harness(_store)
_orchestrator = Orchestrator(_harness, _store)


# ---------------- 数据读取 ----------------
def _load(name: str):
    with open(os.path.join(BASE, "data", name), encoding="utf-8") as f:
        return json.load(f)


# ---------------- 路由实现（对齐 server.py） ----------------
def _config() -> dict:
    products = _load("products.json")["products"]
    profiles_raw = _load("profiles.json")
    cases = _load("cases.json")
    return {
        "profiles": profiles_raw["profiles"],
        "personas": profiles_raw["personas"],
        "products": products,
        "prompt_versions": PROMPT_VERSIONS,
        "models": ROUTES,
        "states": STATES,
        "tool_count": len(ALL_TOOLS),
        "tools": [{"name": k, "desc": v["desc"], "perm": v["perm"]}
                  for k, v in TOOL_REGISTRY.items()],
        "badcases": cases["badcases"],
        "cases": cases["cases"],
    }


def _session(body: dict) -> dict:
    user_id = body.get("user_id", "u_1001")
    prompt_version = body.get("prompt_version", "v3.0")
    sid = "ss_" + uuid.uuid4().hex[:10]
    session = _orchestrator.get_session(sid, user_id, prompt_version)
    profile = _harness.profiles.get(user_id, {})
    return {
        "session_id": sid,
        "user_id": user_id,
        "bucket": _harness.bucket(sid),
        "prompt_version": session.prompt_version,
        "profile": profile,
        "persona_playbook": _harness.personas.get(profile.get("persona", ""), {}),
    }


def _chat(body: dict) -> dict:
    message = (body.get("message") or "").strip()
    if not message:
        raise ValueError("message 不能为空")
    session = _orchestrator.get_session(body.get("session_id"), body.get("user_id", "u_1001"))
    return _orchestrator.run_turn(session, message, body.get("behaviors") or [])


def _trigger(body: dict) -> dict:
    session = _orchestrator.get_session(body.get("session_id"), body.get("user_id", "u_1001"))
    return _orchestrator.trigger(session, body.get("key", ""))


def _compliance_test(body: dict) -> dict:
    session = _orchestrator.get_session("gate_test", "u_1001")
    return _orchestrator.adversarial(session, body.get("text", ""))


def _traces(query: dict) -> dict:
    try:
        limit = int(query.get("limit", ["30"])[0])
    except (TypeError, ValueError):
        limit = 30
    items = [_store.records[t].to_dict(with_steps=False) for t in _store.order[-limit:]]
    return {"items": items[::-1], "count": len(_store.order)}


def _trace_detail(trace_id: str) -> dict:
    rec = _store.records.get(trace_id)
    if not rec:
        raise LookupError("trace 不存在（可能已被滚动淘汰）")
    return rec.to_dict()


def _replay(trace_id: str) -> dict:
    res = _store.replay(trace_id)
    if not res:
        raise LookupError("trace 不存在")
    return res


def _metrics() -> dict:
    return _store.metrics()


def _regression() -> dict:
    return _store.regression()


# ---------------- 分发 ----------------
def _route(path: str, body: dict, method: str):
    parsed = urlparse(path)
    p = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)

    if p == "/api/config":
        return _config()
    if p == "/api/session":
        return _session(body)
    if p == "/api/chat":
        return _chat(body)
    if p == "/api/trigger":
        return _trigger(body)
    if p == "/api/compliance/test":
        return _compliance_test(body)
    if p == "/api/traces":
        return _traces(query)
    if p == "/api/metrics":
        return _metrics()
    if p == "/api/regression":
        return _regression()
    if p.startswith("/api/trace/"):
        return _trace_detail(p[len("/api/trace/"):])
    if p.startswith("/api/replay/"):
        return _replay(p[len("/api/replay/"):])
    raise LookupError(f"未知接口 {p}")


def _bridge_call(path: str, body_json: str = "", method: str = "POST") -> str:
    """bridge.js 的唯一入口。返回信封 JSON，前端解包后throw 或直接用 data。

    用信封而不是直接返回数据，是为了让「失败」也能带着可读原因回到前端 ——
    直接抛异常会被 Pyodide 包成一大坨 traceback，前端只看到 "PythonError"。
    """
    try:
        body = json.loads(body_json) if body_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"请求体不是合法 JSON: {e}"}, ensure_ascii=False)
    try:
        data = _route(path, body, method)
        return json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str)
    except LookupError as e:
        return json.dumps({"ok": False, "error": str(e), "kind": "not_found"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — 任何异常都要回到前端，不能沉在 Python 里
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "kind": "error"},
                          ensure_ascii=False)


def _warmup() -> str:
    """预跑一次配置读取，把 JSON 解析与 Prompt 装配的开销挪到启动阶段。"""
    cfg = _config()
    return f"ok tools={cfg['tool_count']} products={len(cfg['products'])}"
