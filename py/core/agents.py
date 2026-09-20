"""Agent 层：感知层 Observe + Planner 决策层 + 6 个 Executor 执行层"""
from __future__ import annotations

import re
import time
from typing import Any

from . import actuary, llm, voice
from .rag import Retriever

# =====================================================================
# ① Observe 感知层
# =====================================================================
TRIGGER_RULES = [
    ("cart_pending", "加购未付", "加购物车 5 分钟未付款", "推「为什么这个适合你」"),
    ("browsing_multi", "浏览多款", "浏览 3 款以上产品", "主动问预算帮对比"),
    ("health_notice_stuck", "健康告知卡壳", "健康告知页停留超 3 分钟未提交", "即时引导 + 术语改写"),
    ("renewal_due", "续保到期", "保单到期前 30 天未触达", "一键续保提醒"),
    ("claim_progress", "理赔进度", "理赔提交后无进度反馈", "OCR 识别 + 进度主动通知"),
    ("risk_handoff", "风险会话", "出现退保投诉 / 大额复杂咨询语义", "平滑转 1v1 人工"),
]


class ObserveLayer:
    """实时汇聚画像 / 行为事件流 / 历史对话记忆。任何"主动开口"决策先过这一层。"""

    def __init__(self, profiles: dict, personas: dict):
        self.profiles = profiles
        self.personas = personas

    def observe(self, user_id: str, behaviors: list[str], memory: dict,
                latest_message: str = "") -> dict[str, Any]:
        p = self.profiles.get(user_id, {})
        signals = self._detect_triggers(behaviors, p, latest_message)
        emotion = self._emotion_curve(p, latest_message, signals)
        return {
            "user_id": user_id,
            "profile": p,
            "persona": p.get("persona", ""),
            "behaviors": behaviors,
            "memory": memory,
            "signals": signals,
            "emotion": emotion,
            "persona_playbook": self.personas.get(p.get("persona", ""), {}),
            "typing_hint": p.get("typing_hint", "犹豫型"),
            "risk_flags": p.get("risk_flags", []),
        }

    def _detect_triggers(self, behaviors: list[str], profile: dict, message: str) -> list[dict]:
        hits: list[dict] = []
        blob = " ".join(behaviors) + " " + message
        checks = {
            "cart_pending": ("加购未支付" in blob) or ("购物车" in message),
            "browsing_multi": bool(re.search(r"浏览3款|浏览 3 款|浏览过 3|看了三款|浏览3款以上|看了半天|看了好几款", blob)),
            "health_notice_stuck": ("健康告知页停留" in blob) or ("健康告知" in message and ("怎么填" in message or "不知道" in message)),
            "renewal_due": "到期" in blob or "续保" in message,
            "claim_progress": "理赔" in message and ("进度" in message or "到哪" in message),
            "risk_handoff": any(w in message for w in
                                ("退保", "退了", "退掉", "不想买了", "投诉", "拒赔", "纠纷", "起诉", "维权"))
                            or ("退保" in blob and any(w in message for w in ("退", "不划算", "亏"))),
        }
        for key, scene, cond, action in TRIGGER_RULES:
            if checks.get(key):
                hits.append({"key": key, "scene": scene, "condition": cond, "action": action})
        if not hits:
            hits.append({"key": "user_initiated", "scene": "用户主动咨询",
                         "condition": "用户先开口", "action": "被动应答 + 顺势给建议"})
        if profile.get("risk_flags"):
            hits.append({"key": "profile_risk", "scene": "画像风险标记",
                         "condition": "；".join(profile["risk_flags"]),
                         "action": "会话安全等级上调，优先人工兜底"})
        return hits

    def _emotion_curve(self, profile: dict, message: str, signals: list[dict]) -> dict[str, Any]:
        base = {"新手妈妈": 0.55, "30+ 理性白领": 0.5, "50岁准退休人群": 0.35}.get(profile.get("persona", ""), 0.5)
        if any(s["key"] == "health_notice_stuck" for s in signals):
            base -= 0.12
        if any(s["key"] == "cart_pending" for s in signals):
            base -= 0.05
        if any(w in message for w in ("怕", "担心", "看不懂", "贵", "坑")):
            base -= 0.08
        if any(s["key"] == "risk_handoff" for s in signals):
            base -= 0.2
        base = max(0.05, min(0.95, base))
        return {
            "score": round(base, 2),
            "label": "低" if base < 0.4 else ("中" if base < 0.7 else "高"),
            "intervene": base < 0.55,
            "note": "情绪低落节点，AI 应主动介入" if base < 0.55 else "情绪平稳，按需介入",
        }


# =====================================================================
# ② Planner 决策层（只决策，不直接说话）
# =====================================================================
class Planner:
    """主控 Agent（销售大脑）：意图识别、用户分型、开口时机判断、策略选择。"""

    INTENTS = ("opening", "qa", "objection", "compare", "underwriting", "renew", "handoff")

    def plan(self, obs: dict, message: str, state: str, turn: int) -> dict[str, Any]:
        t0 = time.time()
        scene = obs["signals"][0]["scene"]
        user_type = self._classify(obs, message)
        intent = self._intent(scene, message, user_type, obs)
        strategy = obs["persona_playbook"].get("strategy", "陪伴型")
        timing = self._timing(obs, intent)
        executors = self._route(intent, scene, user_type)
        decision = {
            "scene": scene,
            "user_type": user_type,
            "intent": intent,
            "strategy": strategy,
            "timing": timing,
            "executors": executors,
            "state": state,
            "turn": turn,
            "keywords": self._keywords(message),
            "reason": self._reason(scene, user_type, intent, strategy),
            "latency_ms": int((time.time() - t0) * 1000) + 40,
        }
        return decision

    def _classify(self, obs: dict, message: str) -> str:
        hint = obs.get("typing_hint", "")
        if any(k in message for k in ("对比", "哪个划算", "性价比", "差在哪", "条款", "费率")):
            return "比价型"
        if any(k in message for k in ("怎么选", "不懂", "看不懂", "该买什么", "第一次")):
            return "了解型"
        if any(k in message for k in ("再想想", "贵", "犹豫", "考虑一下", "纠结")):
            return "犹豫型"
        return hint or "犹豫型"

    def _intent(self, scene: str, message: str, user_type: str, obs: dict) -> str:
        # 判定顺序 = 优先级：场景 > 用户明确表达的问题 > 分型
        # 分型只在「用户主动咨询」这类没有具体场景时，才用来决定走对比还是问答
        if scene == "风险会话":
            return "handoff"
        # 场景本身已是健康告知卡壳：场景优先，不被消息里的"怎么填"截获成普通问答
        if scene == "健康告知卡壳":
            return "underwriting"
        # 用户明确提出事实型问题：即使有行为触发，也先按问答处理
        if re.search(r"赔不赔|能不能赔|会赔吗|免赔额|等待期|什么意思|怎么算|多久|怎么填|怎么选", message):
            return "qa"
        if "健康告知" in message and user_type == "了解型":
            return "underwriting"
        if re.search(r"太贵|买不起|不划算|骗人|不靠谱|便宜点|贵了|有点贵", message):
            return "objection"
        # 场景专属意图（不受分型覆盖）
        if scene == "加购未付":
            return "opening" if obs["emotion"]["intervene"] else "objection"
        if scene == "续保到期":
            return "renew"
        if scene == "浏览多款":
            return "compare"
        # 无特定场景：分型接管
        if user_type == "比价型":
            return "compare"
        return "qa"

    def _timing(self, obs: dict, intent: str) -> dict[str, Any]:
        e = obs["emotion"]
        score = 0.9 if intent in ("handoff", "underwriting") else (0.8 if e["intervene"] else 0.55)
        return {
            "open_now": score >= 0.6,
            "score": score,
            "window": "用户情绪低谷 + 行为触发" if score >= 0.6 else "等待下一个行为触发点",
            "principle": "不打扰原则：只在行为触发点主动开口",
        }

    def _route(self, intent: str, scene: str, user_type: str) -> list[str]:
        route = ["profile_agent", "rag_agent"]
        if intent in ("opening", "objection", "renew"):
            route += ["recommend_agent", "script_agent"]
        elif intent == "compare":
            route += ["recommend_agent", "script_agent"]
        elif intent == "underwriting":
            route += ["underwriting_agent", "script_agent"]
        elif intent == "handoff":
            route = ["profile_agent", "handoff_agent"]
        else:  # qa
            route += ["script_agent"]
        if intent != "handoff":
            route += ["handoff_agent"]
        seen, ordered = set(), []
        for r in route:
            if r not in seen:
                ordered.append(r)
                seen.add(r)
        return ordered

    def _keywords(self, message: str) -> list[str]:
        kw = []
        for w in ("免赔额", "保证续保", "等待期", "健康告知", "理赔", "退保", "预算",
                  "保额", "保费", "重疾", "医疗", "孩子", "高血压"):
            if w in message:
                kw.append(w)
        return kw

    def _reason(self, scene: str, user_type: str, intent: str, strategy: str) -> str:
        return (f"场景「{scene}」× 分型「{user_type}」→ 意图「{intent}」；"
                f"采用「{strategy}」策略，先化解再推单。")


# =====================================================================
# ③ Act 执行层 —— 6 个专职 Executor
# =====================================================================
class Executors:
    def __init__(self, products: list[dict], cases: list[dict], retriever: Retriever):
        self.products = products
        self.product_index = {p["id"]: p for p in products}
        self.cases = cases
        self.retriever = retriever

    # ---- 1. 画像 Executor ----
    def profile_agent(self, obs: dict, message: str) -> dict[str, Any]:
        t0 = time.time()
        p = obs["profile"]
        interests = [h for h in p.get("history", []) if any(
            k in h for k in ("医疗", "重疾", "年金", "意外", "少儿"))]
        facts = {
            "persona": p.get("persona"),
            "age": p.get("age"),
            "gender": p.get("gender"),
            "life_stage": p.get("persona_desc"),
            "budget_signal": p.get("budget_signal"),
            "concerns": p.get("concerns", []),
            "interests": interests[:3],
            "evidence": f"画像库 T+1 离线 + 实时补全，命中 {len(interests)} 条产品兴趣标签",
        }
        return {"agent": "profile_agent", "name": "画像 Executor", "facts": facts,
                "latency_ms": int((time.time() - t0) * 1000) + 30}

    # ---- 2. RAG 检索 Executor ----
    def rag_agent(self, message: str, plan: dict) -> dict[str, Any]:
        t0 = time.time()
        res = self.retriever.retrieve(message, plan)
        return {"agent": "rag_agent", "name": "RAG 检索 Executor", "rag": res,
                "latency_ms": int((time.time() - t0) * 1000) + 60}

    # ---- 3. 推荐 Executor ----
    def recommend_agent(self, obs: dict, plan: dict, message: str) -> dict[str, Any]:
        t0 = time.time()
        p = obs["profile"]
        persona = p.get("persona", "")
        budget_sensitive = "低预算" in p.get("budget_signal", "")
        picks: list[str] = []
        if "孩子" in message or persona == "新手妈妈":
            picks = ["P-CHI-005", "P-MED-001", "P-ACC-004"]
        elif persona == "30+ 理性白领":
            picks = ["P-CI-002", "P-MED-001", "P-ANN-003"]
        elif persona == "50岁准退休人群":
            picks = ["P-MED-001", "P-ACC-004", "P-ANN-003"]
        else:
            picks = ["P-MED-001", "P-CI-002", "P-ACC-004"]
        if budget_sensitive:
            picks = sorted(picks, key=lambda pid: self._monthly(pid))
        # 与用户当下行为对齐：加购 / 收藏 / 刚浏览过的那款优先主推。
        # 否则会出现"用户嫌医疗险贵，AI 却在推意外险"的答非所问。
        focus = self._focused_product(obs, message)
        if focus and focus in self.product_index and focus in picks:
            picks = [focus] + [pid for pid in picks if pid != focus]
        elif focus and focus in self.product_index:
            picks = [focus] + picks[:2]
        quotes = actuary.batch_quotes(p, self.products, picks)
        items = []
        for q in quotes:
            prod = self.product_index[q.product_id]
            items.append({
                "id": q.product_id, "name": q.product_name, "category": prod["category"],
                "premium": q.premium_display, "coverage": q.coverage_display,
                "highlights": prod["highlights"][:2],
                "reason": self._reason_for(prod, plan, p),
                "actuary_numbers": q.key_numbers,
            })
        return {"agent": "recommend_agent", "name": "推荐 Executor",
                "recommendation": {"items": items, "basis": f"画像分型「{plan['user_type']}」+ 行为触发「{plan['scene']}」",
                                   "click_rate_benchmark": "该分型历史点击率 16.4%"},
                "latency_ms": int((time.time() - t0) * 1000) + 80}

    def _focused_product(self, obs: dict, message: str) -> str | None:
        """找出用户此刻真正在看的那个产品。

        按信号强度加权，而不是简单取最长名称：本次会话的行为 > 本次提问
        > 历史轨迹，同类信号里"加购/已购" > "收藏" > "浏览/搜索"。
        早期实现只按名称长度排序，结果用户加购的是医疗险，
        却因为历史里浏览过一款名字更长的少儿医疗而推错了产品。
        """
        profile = obs.get("profile", {})
        sources = [
            (list(obs.get("behaviors", [])), 10),   # 当前会话行为信号
            ([message], 5),                          # 本次提问
            (list(profile.get("history", [])), 0),   # 历史轨迹
        ]
        best: tuple[int, int, str] | None = None
        for entries, base in sources:
            for entry in entries:
                if any(k in entry for k in ("加购", "已购", "持有", "确认投保")):
                    weight = 3
                elif "收藏" in entry:
                    weight = 2
                else:
                    weight = 1
                for prod in self.products:
                    name = prod.get("name") or ""
                    if name and name in entry:
                        cand = (base + weight, len(name), prod["id"])
                        if best is None or cand[:2] > best[:2]:
                            best = cand
        return best[2] if best else None

    def _monthly(self, pid: str) -> int:
        q = actuary.quote(pid, "", 30, "女")
        return q.key_numbers.get("premium_month") or q.key_numbers.get("premium_year", 0) // 12

    def _reason_for(self, prod: dict, plan: dict, profile: dict) -> str:
        if plan["user_type"] == "比价型":
            return "同价位下免责条款更宽，长期总成本可算清"
        if plan["user_type"] == "了解型":
            return "条款最容易看懂，核保结论明确"
        return "月缴压力小，覆盖住院大额支出"

    # ---- 4. 智能核保 Executor ----
    def underwriting_agent(self, obs: dict, message: str) -> dict[str, Any]:
        t0 = time.time()
        p = obs["profile"]
        age = int(p.get("age", 30))
        table = []
        if age >= 50 or "高血压" in message or "高血压" in str(p.get("concerns")):
            table = [
                {"condition": "高血压 一级（140-159/90-99mmHg）无并发症", "medical": "标准体或加费承保",
                 "ci": "加费承保", "accident": "正常承保"},
                {"condition": "高血压 二级及以上或伴并发症", "medical": "除外承保", "ci": "拒保",
                 "accident": "正常承保"},
                {"condition": "结节 / 息肉（未手术）", "medical": "除外承保", "ci": "除外或延期",
                 "accident": "正常承保"},
            ]
            term_rewrite = [
                ("既往症", "买之前就已经有的病，通常不在赔付范围"),
                ("除外承保", "这项病不赔，其余照赔，保费不变"),
                ("加费承保", "能保，但保费比标准体高一点"),
                ("延期承保", "暂时不能买，观察一段时间后再试"),
            ]
        else:
            table = [
                {"condition": "无既往症、体检指标正常", "medical": "标准体承保", "ci": "标准体承保",
                 "accident": "正常承保"},
                {"condition": "轻度异常指标（BMI 偏高 / 轻度脂肪肝）", "medical": "标准体或加费承保",
                 "ci": "加费承保", "accident": "正常承保"},
            ]
            term_rewrite = [
                ("健康告知", "投保前保险公司问你的健康问题，照实回答就行"),
                ("如实告知", "有一说一，反而能保护你后面的理赔"),
                ("等待期", "刚买的一段时间内生病不赔，意外马上生效"),
            ]
        return {"agent": "underwriting_agent", "name": "智能核保 Executor",
                "underwriting": {
                    "conclusion_table": table,
                    "term_rewrite": [{"term": t, "plain": p_} for t, p_ in term_rewrite],
                    "note": "核保结论以保险公司正式承保结果为准，本页仅作预判",
                    "image_precheck": "如有既往症，建议上传诊断/体检报告，OCR 识别后进入人工核保通道",
                },
                "latency_ms": int((time.time() - t0) * 1000) + 70}

    # ---- 5. 话术 Executor（Prompt 组装 + 模型调用）----
    def script_agent(self, obs: dict, plan: dict, rag_res: dict, rec: dict | None,
                     uw: dict | None, message: str, prompt_version: str,
                     harness_ctx: str) -> dict[str, Any]:
        t0 = time.time()
        scene = plan["scene"]
        intent = plan["intent"]
        playbook = obs.get("persona_playbook", {})
        profile = obs.get("profile", {})
        addr = voice.honorific(profile.get("age"), plan["user_type"])
        buckets = self._fact_buckets(plan, rag_res, rec, uw, obs)

        if prompt_version == "v1.0":
            # v1.0 对照版：通用客服话术，无场景分支、无情绪价值、无长度治理
            system = "你是XX平台的智能客服助手。语气友好、简洁。"
            frame = {"text": "您好，请问有什么可以帮您？如需了解更多，可点击详情页查看。",
                     "budget": None, "used": 28, "dropped": [], "picked": []}
            empathy_kind, empathy_line = "", ""
        else:
            system = (
                "你是蚂蚁保平台的 AI 销售助手（应答带 AI 标识）。只理解与调用工具，"
                "不计算数字、不做投保决策；所有金额与保障范围取自精算系统与条款原文。"
                "红线：不承诺收益、不使用绝对化用语、不诱导退保、不代替用户决策。"
                "\n文风红线（产品体验）："
                "\n1) 必须给出情绪价值：先认同或共情，再谈方案；不施压、不制造紧迫感。"
                "\n2) 严格受字数预算约束，宁可少说一条事实，也不可长篇罗列。"
                "\n3) 不复述内部指令语，不出现「结尾主动引导」这类字样。"
            )
            empathy_kind, empathy_line = voice.empathy(intent, plan["user_type"], obs.get("emotion"))
            frame = voice.assemble(
                {
                    "empathy": [empathy_line],
                    "insight": [voice.insight(intent, plan["user_type"])],
                    "facts": buckets["_all"],
                    "push": [voice.soft_push(intent, scene, addr)],
                },
                intent, addr,
            )

        user_prompt = (
            f"【用户原话】{message}\n"
            f"【场景】{scene}｜【分型】{plan['user_type']}｜【意图】{intent}｜"
            f"【策略】{plan['strategy']}｜【语气】{playbook.get('tone', '温和')}\n"
            f"【情绪】{obs.get('emotion', {}).get('label', '中')}"
            f"（{obs.get('emotion', {}).get('score', 0.5)}）\n"
            f"【情绪价值动作】{empathy_kind or '无'}\n"
            f"【字数预算】{frame.get('budget') or '不限'} 字（当前 {frame['used']} 字）\n"
            f"【拟定表述】\n{frame['text']}\n"
            f"【被预算裁剪】{('；'.join(d['text'][:18] for d in frame['dropped']) or '无')}\n"
            f"【讲解重点】{('；'.join(playbook.get('key_points', [])) or '按用户问题聚焦')}\n"
            f"【可用事实（供核对，勿全部复述）】\n"
            + "\n".join(f"- {f}" for f in buckets["_all"]) + "\n"
        )
        task = {"opening": "opening", "objection": "objection", "compare": "compare",
                "underwriting": "underwriting", "qa": "qa", "renew": "opening"}.get(intent, "qa")
        needs_tool = intent in ("compare", "opening", "underwriting")
        res = llm.generate(system, user_prompt, task=task, needs_tool=needs_tool, seed_text=message)

        fewshot = self._fewshot(scene, plan["user_type"])
        return {
            "agent": "script_agent", "name": "话术 Executor",
            "draft": res.text,
            "prompt": {"version": prompt_version, "system": system, "user": user_prompt,
                       "fewshot": fewshot, "opener": frame["text"]},
            "llm": {"route": res.route, "model": res.model, "mode": res.mode,
                    "latency_ms": res.latency_ms, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out},
            "evidence": [h["text"] for h in rag_res.get("hits", [])],
            "facts": buckets["_all"],
            "fact_buckets": buckets,
            # 情绪价值句 / 洞察句 / 推动作都是关系性表述，不承载事实主张，豁免 G2
            "exempt_from_nli": [p["text"] for p in frame["picked"]
                                if p["bucket"] in ("empathy", "insight", "push")],
            "voice": {"empathy_kind": empathy_kind, "empathy_line": empathy_line,
                      "budget": frame.get("budget"), "used": frame["used"],
                      "dropped": frame["dropped"], "addr": addr,
                      "picked": frame["picked"]},
            "latency_ms": int((time.time() - t0) * 1000) + res.latency_ms,
        }



    def _fact_buckets(self, plan: dict, rag: dict, rec: dict | None, uw: dict | None,
                      obs: dict) -> dict[str, list[str]]:
        """组装事实桶。每条都压成"一句话级别"——明细交给气泡下方的卡片，
        气泡里重复罗列只会稀释情绪价值、拉长阅读负担。
        """
        product: list[str] = []
        clause: list[str] = []
        uw_facts: list[str] = []
        persona: list[str] = []
        intent = plan["intent"]

        items = rec["recommendation"]["items"] if rec else []
        if intent in ("opening", "objection"):
            # 主推一款，且只给月缴 + 最有力的一条卖点
            product = [voice.compact_product(it, "lead") for it in items[:1]]
        elif intent == "compare":
            product = [voice.compact_product(it, "brief") for it in items[:3]]
        elif intent == "renew":
            product = [voice.compact_product(it, "lead") for it in items[:1]]

        for h in rag.get("hits", [])[:3]:
            clause.append(voice.compact_clause(h))
        if uw:
            for row in uw["underwriting"]["conclusion_table"][:2]:
                uw_facts.append(voice.compact_uw_row(row))
            for tr in uw["underwriting"]["term_rewrite"][:2]:
                uw_facts.append(voice.compact_term(tr))

        kp = obs.get("persona_playbook", {}).get("key_points", [])
        if kp:
            persona.append(voice.clip("讲解重点：" + "、".join(kp), 34))

        # 与当前话题无关的条款不入选（例如加购场景不该夹带退保条款）
        if not any(k in plan.get("keywords", []) for k in ("退保",)):
            clause = [c for c in clause if not c.startswith(("犹豫期与退保规则", "退保"))]

        if intent in ("opening", "objection", "renew"):
            ordered = product + clause[:1]
        elif intent == "compare":
            # 对比场景给出三条不同款的差异点，各自成行
            ordered = product + clause[:1]
        elif intent == "underwriting":
            ordered = uw_facts[:3] + clause[:1]
        else:  # qa
            ordered = clause[:2]
        ordered = [f for f in ordered if f]

        buckets = {"product": product, "clause": clause, "uw": uw_facts, "persona": persona,
                   "_all": ordered}
        return buckets

    def _fewshot(self, scene: str, user_type: str) -> list[dict]:
        matched = [c for c in self.cases if c["scene"] == scene and c["user_type"] == user_type]
        if not matched:
            matched = sorted(self.cases, key=lambda c: -c["ctr"])[:3]
        return [{"id": c["id"], "ctr": c["ctr"], "conversion": c["conversion"],
                 "text": c["text"], "why": c["why"]} for c in matched[:3]]

    # ---- 6. 转人工路由 Executor ----
    def handoff_agent(self, obs: dict, message: str, plan: dict) -> dict[str, Any]:
        t0 = time.time()
        p = obs["profile"]
        high_value = p.get("ltv_tier") == "高价值" or int(p.get("age", 30)) >= 50
        risk_terms = [w for w in ("退保", "退了", "退掉", "不想买了", "投诉", "拒赔", "纠纷", "起诉", "维权")
                      if w in message]
        big_amount = bool(re.search(r"(\d+)\s*万", message)) or bool(re.search(r"[1-9]\d{2,}", message))
        should = bool(risk_terms) or big_amount or plan["intent"] == "handoff"
        reasons = []
        if risk_terms:
            reasons.append("高风险语义：" + "、".join(risk_terms))
        if big_amount:
            reasons.append("大额复杂咨询")
        if p.get("risk_flags"):
            reasons.append("安全等级上调（画像风险：" + "、".join(p["risk_flags"]) + "）")
        if high_value:
            reasons.append(f"高价值客户（{p.get('ltv_tier')}）")
        if not should and p.get("risk_flags"):
            reasons = []
        return {"agent": "handoff_agent", "name": "转人工路由 Executor",
                "handoff": {
                    "should": should,
                    "reasons": reasons,
                    "route": "1v1 资深客服（保险经纪资质）" if should else "无",
                    "sla": "30 秒内接入" if should else "",
                    "async_note": "转接同时异步推送会话摘要给客服" if should else "",
                },
                "latency_ms": int((time.time() - t0) * 1000) + 35}


# 推动作话术已迁移至 core/voice.py（soft_push）：
# 原实现把「结尾主动引导：」这类内部指令前缀直接拼进了用户可见文案，
# 且话术带紧迫感施压（“优惠窗口就在今晚”），与情绪价值目标相冲突。
