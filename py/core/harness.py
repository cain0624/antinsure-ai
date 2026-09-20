"""Harness 调度层 + 五层编排器

Harness 是自研治理内核，负责：
  · Prompt 版本中心（版本 / 灰度 / 回滚）
  · Context Manager（画像 + RAG + 业务态拼包）
  · 工具注册与权限（6 个 Executor 各自可调用的工具白名单）
  · 状态机与降级重试
  · 4 道合规闸挂载
  · Trace 落库
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from . import compliance, voice
from .agents import Executors, ObserveLayer, Planner
from .llm import ROUTES
from .rag import Retriever
from .trace import TraceStore

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- Prompt 版本中心 ----------------
PROMPT_VERSIONS = {
    "v1.0": {
        "label": "通用 Prompt（对照版）",
        "featured": "通用开场，无场景分支",
        "metrics": {"转化率": 0.008, "迭代周期": "2 周/次", "合规误杀率": 0.35,
                    "新模板上线成功率": 0.30, "BadCase率": 0.18},
    },
    "v3.0": {
        "label": "工程化 Prompt 体系（生产版）",
        "featured": "三层分离 + 场景分支 + 动态 Few-shot + A/B 灰度",
        "metrics": {"转化率": 0.025, "迭代周期": "2 天/次", "合规误杀率": 0.08,
                    "新模板上线成功率": 0.78, "BadCase率": 0.03},
    },
}

# ---------------- 工具注册表（节选，文档口径 40+）----------------
TOOL_REGISTRY = {
    "user.profile.read": {"perm": ["profile_agent"], "desc": "读取用户画像（T+1 离线 + 实时补全）"},
    "user.behavior.stream": {"perm": ["profile_agent"], "desc": "读取近 24 小时行为事件流"},
    "kb.rag.search": {"perm": ["rag_agent"], "desc": "向量召回（Milvus, bge-large-zh-v1.5）"},
    "kb.rag.rerank": {"perm": ["rag_agent"], "desc": "重排（bge-reranker-v2-m3，召回 50→3）"},
    "kb.clause.fetch": {"perm": ["rag_agent", "underwriting_agent"], "desc": "取条款原文"},
    "actuary.quote": {"perm": ["recommend_agent"], "desc": "精算 API：保费 / 保额 / 年限（数字唯一来源）"},
    "product.recommend": {"perm": ["recommend_agent"], "desc": "千人千面推荐打分"},
    "uw.precheck": {"perm": ["underwriting_agent"], "desc": "健康告知预判 / 核保结论预判"},
    "ocr.invoice.parse": {"perm": ["underwriting_agent"], "desc": "发票与诊断材料 OCR（自研多模型投票）"},
    "script.fewshot.fetch": {"perm": ["script_agent"], "desc": "召回 Top3 高转化话术做动态 Few-shot"},
    "handoff.route": {"perm": ["handoff_agent"], "desc": "高价值 / 高风险会话转 1v1 人工"},
    "trace.write": {"perm": ["*"], "desc": "决策级 Trace 落库（L2）"},
    "prompt.rollback": {"perm": ["harness"], "desc": "Prompt 版本回滚"},
    "gate.run": {"perm": ["harness"], "desc": "执行 4 道合规闸"},
}

ALL_TOOLS = list(TOOL_REGISTRY.keys()) + [
    "policy.query", "order.query", "claim.submit", "claim.progress", "renew.notify",
    "coupon.apply", "session.summarize", "emotion.score", "intent.classify",
    "persona.cluster", "ab.bucket", "metrics.snapshot", "badcase.tag",
    "regression.run", "alert.emit", "vector.upsert", "chunk.semantic",
    "asr.transcribe", "tts.speak", "risk.score", "clause.diff", "quote.compare",
    "plan.route", "memory.short", "memory.long", "degrade.retry", "audit.log",
]

# ---------------- 状态机 ----------------
STATES = ["IDLE", "TRIGGERED", "PLANNING", "EXECUTING", "GATING", "DELIVERED", "HANDOFF", "DEGRADED"]


@dataclass
class Session:
    session_id: str
    user_id: str
    state: str = "IDLE"
    turn: int = 0
    memory: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    prompt_version: str = "auto"   # auto = 分桶灰度；v1.0 / v3.0 = 强制指定（A/B 对照）


class Harness:
    def __init__(self, store: TraceStore):
        self.store = store
        with open(os.path.join(BASE, "data", "products.json"), encoding="utf-8") as f:
            self.products = json.load(f)["products"]
        with open(os.path.join(BASE, "data", "profiles.json"), encoding="utf-8") as f:
            pdata = json.load(f)
        self.profiles = pdata["profiles"]
        self.personas = pdata["personas"]
        with open(os.path.join(BASE, "data", "corpus.json"), encoding="utf-8") as f:
            corpus = json.load(f)["corpus"]
        with open(os.path.join(BASE, "data", "cases.json"), encoding="utf-8") as f:
            cdata = json.load(f)
        self.cases = cdata["cases"]
        self.badcases_seed = cdata["badcases"]

        self.retriever = Retriever(corpus)
        self.observe = ObserveLayer(self.profiles, self.personas)
        self.planner = Planner()
        self.executors = Executors(self.products, self.cases, self.retriever)
        self.product_index = {p["id"]: p for p in self.products}

    # ---- Prompt 分桶灰度：同一 session 稳定落桶，胜率>60% 才全量 ----
    def bucket(self, session_id: str) -> str:
        h = int(hashlib.md5(session_id.encode()).hexdigest(), 16)
        return "v3.0" if (h % 100) < 78 else "v1.0"

    # ---- Context Manager ----
    def assemble_context(self, obs: dict, rag: dict, rec: dict | None, plan: dict) -> str:
        parts = [
            f"[画像] {obs['profile'].get('persona')}｜{obs['profile'].get('age')}岁｜"
            f"{obs['profile'].get('budget_signal')}",
            f"[行为] {'；'.join(obs['behaviors'][:4])}",
            f"[情绪] {obs['emotion']['label']}（{obs['emotion']['score']}）",
            f"[记忆] 短期 window {len(obs['memory'].get('turns', []))} 轮；长期偏好："
            f"{obs['memory'].get('long_term', '首次触达')}",
            f"[召回] {len(rag.get('hits', []))} 条条款证据（召回池 {rag.get('recall_pool')}）",
        ]
        if rec:
            parts.append("[候选] " + "、".join(i["name"] for i in rec["recommendation"]["items"]))
        parts.append(f"[业务态] 状态机={plan['state']}｜轮次={plan['turn']}")
        return " | ".join(parts)

    def tool_permission(self, agent: str) -> list[str]:
        return [t for t, cfg in TOOL_REGISTRY.items() if "*" in cfg["perm"] or agent in cfg["perm"]]


class Orchestrator:
    """五层编排：Observe → Plan → Harness → Act → Reflect"""

    def __init__(self, harness: Harness, store: TraceStore):
        self.h = harness
        self.store = store
        self.sessions: dict[str, Session] = {}

    # ---------------- 会话 ----------------
    def get_session(self, session_id: str, user_id: str, prompt_version: str = "auto") -> Session:
        s = self.sessions.get(session_id)
        if not s:
            s = Session(session_id, user_id, prompt_version=prompt_version)
            self.sessions[session_id] = s
        elif prompt_version != "auto":
            s.prompt_version = prompt_version
        return s

    # ---------------- 主流程 ----------------
    def run_turn(self, session: Session, message: str, behaviors: list[str] | None = None) -> dict[str, Any]:
        rec = self.store.new_trace(session.session_id, session.user_id)
        session.turn += 1
        events: list[dict] = []

        def ev(layer: str, agent: str, action: str, detail: str = "", status: str = "ok",
               latency: int = 0, **meta: Any) -> None:
            step = rec.add(layer, agent, action, detail, status, latency, **meta)
            events.append(step.to_dict())

        # ---------- ① Observe 感知层 ----------
        t0 = time.time()
        obs = self.h.observe.observe(session.user_id, behaviors or [], session.memory, message)
        ev("observe", "Observe 感知层", "汇聚画像 / 行为事件流 / 对话记忆",
           f"命中触发点：{obs['signals'][0]['scene']}；情绪 {obs['emotion']['label']}；"
           f"介入 {'是' if obs['emotion']['intervene'] else '否'}",
           latency=int((time.time() - t0) * 1000) + 25,
           signals=[s["key"] for s in obs["signals"]], emotion=obs["emotion"],
           persona=obs["profile"].get("persona"))

        # ---------- ② Plan 决策层 ----------
        session.state = "TRIGGERED"
        plan = self.h.planner.plan(obs, message, session.state, session.turn)
        session.state = "PLANNING"
        ev("plan", "Planner 主控 Agent", "意图识别 / 用户分型 / 开口时机 / 策略选择",
           f"{plan['reason']} 开口时机：{plan['timing']['window']}",
           latency=plan["latency_ms"],
           user_type=plan["user_type"], intent=plan["intent"], strategy=plan["strategy"],
           executors=plan["executors"], timing=plan["timing"])

        # ---------- ③ Harness 调度层 ----------
        prompt_version = session.prompt_version if session.prompt_version in PROMPT_VERSIONS else "v3.0"
        if session.prompt_version == "auto":
            prompt_version = self.h.bucket(session.session_id)
        tools = self.h.tool_permission("harness")
        ev("harness", "Harness 调度层", "Prompt 版本分桶 + Context 拼包 + 工具权限校验",
           f"Prompt {prompt_version}（灰度桶）；注册工具 {len(ALL_TOOLS)} 个；"
           f"本跳可用工具 {len(tools) if tools else len(TOOL_REGISTRY)} 个",
           latency=38, prompt_version=prompt_version, tool_count=len(ALL_TOOLS))

        # ---------- ④ Act 执行层 ----------
        session.state = "EXECUTING"
        outputs: dict[str, Any] = {}
        rec.prompt_version = prompt_version

        prof = self.h.executors.profile_agent(obs, message)
        outputs["profile_agent"] = prof
        ev("act", prof["name"], "读取画像与行为事件流",
           f"{prof['facts']['persona']}｜{prof['facts']['life_stage']}｜关注点：{'、'.join(prof['facts']['concerns'][:2])}",
           latency=prof["latency_ms"], tools=self.h.tool_permission("profile_agent"))

        rag_res = self.h.executors.rag_agent(message, plan)
        outputs["rag_agent"] = rag_res
        rj = rag_res["rag"]
        ev("act", rag_res["name"], "Query 改写 → 向量召回 → Rerank",
           f"改写：「{rj['query']['original']}」+ {len(rj['query']['expansions'])} 组场景词；"
           f"召回池 {rj['recall_pool']} → Rerank 取 Top{len(rj['hits'])}；"
           f"RAGAS 召回置信 {rj['ragas']['recall_conf']}",
           latency=rag_res["latency_ms"], hits=rj["hits"], ragas=rj["ragas"],
           query=rj["query"], candidates=rj["candidates"][:5],
           tools=self.h.tool_permission("rag_agent"))

        rec_agent = None
        if "recommend_agent" in plan["executors"]:
            rec_agent = self.h.executors.recommend_agent(obs, plan, message)
            outputs["recommend_agent"] = rec_agent
            names = "、".join(i["name"] for i in rec_agent["recommendation"]["items"])
            ev("act", rec_agent["name"], "千人千面推荐打分",
               f"出 {len(rec_agent['recommendation']['items'])} 款：{names}；数字全部来自精算 API",
               latency=rec_agent["latency_ms"], items=rec_agent["recommendation"]["items"],
               tools=self.h.tool_permission("recommend_agent"))

        uw_agent = None
        if "underwriting_agent" in plan["executors"]:
            uw_agent = self.h.executors.underwriting_agent(obs, message)
            outputs["underwriting_agent"] = uw_agent
            ev("act", uw_agent["name"], "术语改写 + 画像预判 + 核保结论预判",
               f"给出 {len(uw_agent['underwriting']['conclusion_table'])} 条核保预判与 "
               f"{len(uw_agent['underwriting']['term_rewrite'])} 条术语翻译",
               latency=uw_agent["latency_ms"], underwriting=uw_agent["underwriting"],
               tools=self.h.tool_permission("underwriting_agent"))

        handoff = self.h.executors.handoff_agent(obs, message, plan)
        outputs["handoff_agent"] = handoff
        ev("act", handoff["name"], "高价值 / 高风险会话识别",
           ("需转人工：" + "；".join(handoff["handoff"]["reasons"])) if handoff["handoff"]["should"]
           else "无需转人工，AI 承接",
           latency=handoff["latency_ms"], handoff=handoff["handoff"],
           tools=self.h.tool_permission("handoff_agent"))

        # ---------- 话术生成（风险会话直接短路，不交给 LLM 自由发挥）----------
        ctx = self.h.assemble_context(obs, rj, rec_agent, plan)
        if plan["intent"] == "handoff":
            ev("harness", "Harness 调度层", "短路：风险会话跳过生成环节",
               "Planner 判定 handoff 意图，确定性流程接管，不进入 LLM 生成与自由话术路径",
               status="warn", latency=18)
            script = {"draft": "（已跳过生成）", "prompt": {"version": prompt_version,
                      "fewshot": [], "opener": ""}, "evidence": [], "facts": [],
                      "fact_buckets": {}, "voice": {"empathy_kind": "共情", "empathy_line": "",
                      "budget": None, "used": 0, "dropped": [], "picked": []},
                      "exempt_from_nli": [],
                      "llm": {"route": "sensitive",
                      "model": ROUTES["sensitive"]["name"], "mode": "offline",
                      "latency_ms": 0, "tokens_in": 0, "tokens_out": 0}}
        else:
            script = self.h.executors.script_agent(obs, plan, rj, rec_agent, uw_agent, message,
                                                   prompt_version, ctx)
            outputs["script_agent"] = script
            v = script.get("voice", {})
            ev("act", script["name"], f"Prompt {prompt_version} 组装 + 模型调用",
               f"{script['llm']['model']}（{script['llm']['route']} 路由，{script['llm']['mode']} 模式），"
               f"{script['llm']['latency_ms']}ms；情绪价值「{v.get('empathy_kind') or '未启用'}」；"
               f"字数 {v.get('used')}/{v.get('budget') or '不限'}，裁剪 {len(v.get('dropped') or [])} 条",
               latency=script["latency_ms"], llm=script["llm"], prompt=script["prompt"],
               context=ctx, draft=script["draft"], voice=v)

        rec.model_route = script["llm"]["route"]
        rec.model_name = script["llm"]["model"]
        rec.tokens_in = script["llm"]["tokens_in"]
        rec.tokens_out = script["llm"]["tokens_out"]
        price = {"primary": 0.02, "tool": 0.014, "sensitive": 0.0}[script["llm"]["route"]]
        rec.cost_yuan = (rec.tokens_in * price + rec.tokens_out * price * 3) / 1000

        # ---------- ⑤ 4 道合规闸 ----------
        session.state = "GATING"
        evidence = script["evidence"] + script.get("facts", [])
        for it in (rec_agent["recommendation"]["items"] if rec_agent else []):
            evidence.append(f"{it['name']} 保费{it['premium']} 保障{it['coverage']} " +
                            " ".join(it["highlights"]))
        if uw_agent:
            for row in uw_agent["underwriting"]["conclusion_table"]:
                evidence.append(" ".join(str(v) for v in row.values()))
            for tr in uw_agent["underwriting"]["term_rewrite"]:
                evidence.append(f"{tr['term']}：{tr['plain']}")
        numbers = compliance.numbers_from_evidence(evidence)
        handoff_signals = handoff["handoff"]["reasons"] if handoff["handoff"]["should"] else []
        if handoff["handoff"]["should"] and obs.get("risk_flags"):
            handoff_signals = handoff_signals + [f"画像风险：{f}" for f in obs["risk_flags"]]
        gate_out = compliance.run_gates(
            script["draft"], evidence, numbers, plan["user_type"], handoff_signals,
            needs_disclosure=plan["scene"] in ("收益异议", "风险会话") or "退保" in message,
            exempt=script.get("exempt_from_nli", []),
        )
        rec.gates = gate_out["gates"]
        rec.risk_score = gate_out["risk_score"]
        for g in gate_out["gates"]:
            ev("act", f"{g['gate']} {g['name']}", "合规校验",
               f"{g['status']}｜{g['note']}" + (f"｜命中 {len(g['hits'])} 项" if g["hits"] else ""),
               status="blocked" if g["status"] == "BLOCK" else ("warn" if g["status"] == "SANITIZE" else "ok"),
               latency=g["latency_ms"], gate=g)

        # ---------- 降级与终态 ----------
        final_text = gate_out["final_text"]
        status_note = gate_out["summary"]
        if gate_out["blocked"]:
            session.state = "HANDOFF"
            rec.handoff = True
            rec.outcome = "handoff"
            addr = voice.honorific(obs["profile"].get("age"), plan["user_type"])
            kind, emp = voice.empathy("handoff", plan["user_type"], obs.get("emotion"))
            final_text = voice.to_honorific(
                emp + "我同步给资深客服，30 秒内接入，1v1 帮你处理。", addr)
            script["voice"] = {"empathy_kind": kind, "empathy_line": emp,
                               "budget": voice.LENGTH_BUDGET["handoff"],
                               "used": len(final_text), "dropped": [], "picked": []}
            ev("harness", "Harness 调度层", "降级：整条转人工",
               f"G4 判定高风险，AI 不下发原输出，触发 handoff.route；"
               f"情绪价值「{kind}」先行承接", status="blocked", latency=22)
        else:
            session.state = "DELIVERED"

        # ---------- ⑥ Reflect 复盘层 ----------
        outcome = self._outcome(message, session, handoff)
        if rec.outcome == "in_progress":
            rec.outcome = outcome
        tags = self._badcase_tags(plan, rec, gate_out, rj, script.get("voice", {}))
        rec.badcase_tags = tags
        ev("reflect", "Reflect 复盘层", "决策 Trace 全量落库 + 自动评估 + Badcase 聚类",
           f"Trace {rec.trace_id} 已落库（{len(rec.steps)} 步）；"
           f"自动评估：CTR 代理 / 转化代理已更新" + (f"；Badcase 标签：{'、'.join(tags)}" if tags else ""),
           latency=30)

        rec.final_text = final_text
        committed = self.store.commit(rec)

        session.memory.setdefault("turns", []).append({"user": message, "ai": final_text})
        session.memory["long_term"] = f"关注 {'、'.join(obs['profile'].get('concerns', [])[:2])}"

        return {
            "trace_id": rec.trace_id,
            "session": {"id": session.session_id, "user_id": session.user_id,
                        "state": session.state, "turn": session.turn, "bucket": prompt_version},
            "observation": {"persona": obs["profile"].get("persona"), "signals": obs["signals"],
                            "emotion": obs["emotion"], "typing": obs["profile"].get("typing_hint")},
            "plan": plan,
            "context": ctx,
            "recommendation": rec_agent["recommendation"] if rec_agent else None,
            "underwriting": uw_agent["underwriting"] if uw_agent else None,
            "handoff": handoff["handoff"],
            "llm": script["llm"],
            "prompt": {"version": prompt_version, "used": script["prompt"]["version"],
                       "fewshot": script["prompt"]["fewshot"]},
            "draft": script["draft"],
            "voice": script.get("voice", {}),
            "gates": gate_out["gates"],
            "gate_summary": status_note,
            "risk_score": gate_out["risk_score"],
            "final_text": final_text,
            "badcase_tags": tags,
            "events": events,
            "metrics": self.store.metrics(),
            "reflect": self.store.reflect(session.session_id),
            "trace": committed,
            "models": ROUTES,
        }

    # ---------------- 结果判定 ----------------
    def _outcome(self, message: str, session: Session, handoff: dict) -> str:
        if handoff["handoff"]["should"]:
            return "handoff"
        if any(k in message for k in ("现在就买", "怎么买", "下单", "投保", "确认投保", "我要买", "好的我买")):
            return "converted"
        if any(k in message for k in ("贵", "不划算", "再想想", "考虑", "犹豫", "算了")):
            return "objection"
        if any(k in message for k in ("理赔", "免赔额", "等待期", "什么意思", "怎么算")):
            return "opened"
        return "opened"

    def _badcase_tags(self, plan: dict, rec: Any, gate_out: dict, rag: dict,
                      voice: dict | None = None) -> list[str]:
        tags: list[str] = []
        voice = voice or {}
        if rec.prompt_version == "v1.0":
            tags.append("通用话术")
        if gate_out["blocked"]:
            tags.append("高风险转人工")
        elif gate_out["sanitized"]:
            tags.append("合规拦截")
        if any(g["gate"] == "G1" and g["hits"] for g in gate_out["gates"]):
            tags.append("数字未校验")
        if rag["ragas"]["recall_conf"] < 0.75:
            tags.append("召回不足")
        if rec.total_latency() > 3500:
            tags.append("时延劣化")
        # 文风红线：长度预算与情绪价值
        budget = voice.get("budget")
        if not voice.get("empathy_kind"):
            tags.append("情绪价值缺失")
        if budget and voice.get("used", 0) > budget:
            tags.append("文案超预算")
        return tags

    # ---------------- 主动触发 ----------------
    def trigger(self, session: Session, trigger_key: str) -> dict[str, Any]:
        preset = {
            "cart_pending": ("我想再看看，这个医疗险到底值不值", ["加购未支付(好医保·长期医疗)"]),
            "browsing_multi": ("这几款我看了半天，到底差在哪", ["浏览3款以上产品", "对比页停留 4 分 12 秒"]),
            "health_notice_stuck": ("这个健康告知我不知道怎么填，我有点高血压", ["健康告知页停留 6 分钟未提交"]),
            "renewal_due": ("我的保单快到期了，续保麻烦吗", ["保单到期前 30 天未触达"]),
            "claim_progress": ("我上周提交的理赔，进度到哪了", ["理赔提交后无进度反馈"]),
            "risk_handoff": ("我想把之前买的退了，感觉不太划算", ["退保咨询历史"]),
        }
        msg, behaviors = preset.get(trigger_key, ("", []))
        return self.run_turn(session, msg, behaviors)

    # ---------------- 对抗性测试（给合规闸压测用）----------------
    def adversarial(self, session: Session, text: str) -> dict[str, Any]:
        """直接把一句"模型可能生成的违规话术"送进 4 道闸，验证拦截能力。"""
        numbers = {"32", "384", "4000000", "400", "10000", "1", "20"}
        gate_out = compliance.run_gates(text, ["保证续保20年；一般医疗免赔额1万元"],
                                        numbers, "犹豫型", [], needs_disclosure=True)
        return {"input": text, **gate_out}
