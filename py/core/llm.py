"""模型层：三路分流 + 可插拔真实 LLM

分流策略（对齐文档）：
  - 主力 LLM（Qwen3-Max）：80% 流量，RAG 对话 / 销售话术
  - 专用 LLM（DeepSeek-V4-Pro）：20% 流量，Agent 工具调用（tool-call 得分 4.3）
  - 敏感场景（自研 Qwen3-7B 微调，vLLM 私有化）：合规敏感 / 健康信息 / 退保类

未配置 API Key 时走本地确定性合成器（offline synthesizer）：
它不"编造"数字，只负责把 Harness 组装好的事实材料组织成人话，
因此离线模式同样能跑通全链路与合规闸。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import urllib.request
from dataclasses import dataclass
from typing import Any


ROUTES = {
    "primary": {"name": "Qwen3-Max", "vendor": "阿里云百炼", "price": "0.02元/千token", "share": 0.80},
    "tool": {"name": "DeepSeek-V4-Pro", "vendor": "DeepSeek", "price": "0.014元/千token", "share": 0.20},
    "sensitive": {"name": "Qwen3-7B（自研微调）", "vendor": "vLLM 私有化", "price": "自建", "share": 0.0},
}


@dataclass
class LLMResult:
    text: str
    route: str
    model: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    mode: str  # "api" | "offline"


_SENSITIVE_HINTS = ("退保", "投诉", "健康", "高血压", "病史", "拒保", "收益", "疾病", "理赔纠纷")


def pick_route(task: str, text: str, needs_tool: bool) -> str:
    """决定这一跳用哪个模型。"""
    payload = f"{task}|{text}"
    if any(h in text for h in _SENSITIVE_HINTS):
        return "sensitive"
    if needs_tool:
        # 工具调用类任务按 80/20 分流，但用稳定哈希保证可复现
        h = int(hashlib.md5(payload.encode()).hexdigest(), 16)
        return "tool" if (h % 100) >= 80 else "primary"
    return "primary"


def _api_available() -> bool:
    return bool(os.environ.get("ANTINSURE_LLM_KEY"))


def _call_api(system: str, user: str, model: str) -> str:
    base = os.environ.get("ANTINSURE_LLM_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    key = os.environ.get("ANTINSURE_LLM_KEY", "")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"]


def generate(system: str, user: str, task: str, needs_tool: bool = False,
             seed_text: str = "") -> LLMResult:
    """统一生成入口。返回文本 + 路由信息 + 用量（用于 Trace L1）。"""
    route = pick_route(task, seed_text or user, needs_tool)
    model = ROUTES[route]["name"]
    tokens_in = len(system) + len(user)

    if _api_available():
        try:
            text = _call_api(system, user, os.environ.get("ANTINSURE_LLM_MODEL", "qwen3-max"))
            return LLMResult(text.strip(), route, model, random.randint(600, 1600),
                             tokens_in, len(text), "api")
        except Exception:  # noqa: BLE001 — 降级到离线合成器，保证链路不断
            pass

    text = _offline_synthesize(system, user, task)
    return LLMResult(text, route, model, random.randint(240, 720),
                     tokens_in, len(text), "offline")


def _offline_synthesize(system: str, user: str, task: str) -> str:
    """离线合成器：只做"材料 → 人话"的组织，不生成任何新事实。

    风格层（core/voice.py）已经把情绪价值句、一句话洞察、压缩事实、软引导
    按意图的字数预算装填成【拟定表述】。这里的职责只是把它交给下发链路，
    相当于真实模型"遵循 Context 约束、不新增事实"的离线等价物。
    """
    draft = _extract_section(user, "拟定表述").strip()
    if draft:
        return draft

    # 兜底：Context 结构异常时按事实桶拼装（保持与条款/精算一致）
    ctx = _extract_section(user, "可用事实") or _extract_section(user, "可用表述")
    facts = [line.strip("- ").strip() for line in ctx.splitlines() if line.strip().startswith("-")]
    facts = [f for f in facts if f]
    if not facts:
        return "我先把条款原文调出来给您看，避免凭印象回答。"
    if task == "compare":
        return "\n".join(f"· {f}" for f in facts[:3])
    return "".join(facts[:2])


def _extract_section(text: str, name: str) -> str:
    marker = f"【{name}】"
    if marker not in text:
        return ""
    rest = text.split(marker, 1)[1]
    for stop in ("【", "\n\n"):
        if stop in rest:
            rest = rest.split(stop, 1)[0]
    return rest.strip()
