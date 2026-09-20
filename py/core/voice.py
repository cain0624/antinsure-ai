"""话术风格治理层（Voice Layer）—— 情绪价值 + 长度预算 + 事实压缩

在文档的 4 道合规闸之外，补两条"产品体验红线"：

  R1 长度预算：每条对外文案必须落在意图对应的字数预算内。
     超长时按「情绪价值 > 一句话洞察 > 推动作 > 事实明细」的优先级裁剪，
     而不是让模型自由发挥。事实明细在气泡下方的卡片里已有，气泡里重复即噪音。

  R2 情绪价值必带：每条对外文案至少要携带 1 个情绪价值动作。
     四类动作：认同 / 共情 / 减压 / 赋能。
     情绪分越低，减压与共情权重越高（不给方案先卸压力）。

设计原则：气泡说的是"人话"，卡片放的是"数据"。
气泡 | 情绪价值句 + 一句话洞察 + 1~3 条压缩事实 + 软引导
"""
from __future__ import annotations

import re

# =====================================================================
# 长度预算（字符数，含标点）—— 按意图给，不是一刀切
# =====================================================================
LENGTH_BUDGET = {
    "opening": 100,      # 加购未付要带出购物车里那一款的关键数字
    "objection": 100,
    "qa": 82,
    "compare": 152,      # 三款差异各占一行，需要更多预算
    "underwriting": 140,  # 核保结论表按分点排，还要留出「走智能核保」的引导
    "renew": 96,         # 续保要给出费率与保障是否变化
    "handoff": 66,
}

# 组装顺序：预算不够时，排在后面的优先被裁掉。
# facts 排在 push 之前 —— 事实是用户真正要看的信息，软引导只是收尾，可省。
FRAME_ORDER = {
    "opening": ["empathy", "insight", "facts", "push"],
    "objection": ["empathy", "insight", "facts", "push"],
    "qa": ["empathy", "facts", "push"],
    "compare": ["empathy", "insight", "facts", "push"],
    "underwriting": ["empathy", "insight", "facts", "push"],
    "renew": ["empathy", "insight", "facts", "push"],
    "handoff": ["empathy"],
}

# =====================================================================
# 情绪价值句库：四类动作 —— 认同 / 共情 / 减压 / 赋能
# 键为 (意图, 分型)，分型 "*" 表示通配
# =====================================================================
EMPATHY_LIB: dict[tuple[str, str], tuple[str, str]] = {
    # ---- 加购未付：核心情绪是"怕买错"，先卸掉"我在拖延"的自责 ----
    ("opening", "犹豫型"): ("认同", "放进购物车却没急着付款，这不是拖延，是你在替家人多想一步。"),
    ("opening", "比价型"): ("认同", "还没付款，说明你在算这笔钱值不值——这个习惯挺好。"),
    ("opening", "了解型"): ("减压", "第一次看这类产品，慢一点才是对的。"),

    # ---- 价格异议：先承认"贵"是真话，再重构价格感知 ----
    ("objection", "*"): ("认同", "觉得贵是真话，说明你在认真算这笔钱，不是在随便挑。"),

    # ---- 浏览多款：把"选不出来"重新定义成"挑得认真" ----
    ("compare", "比价型"): ("赋能", "三款都翻了一遍还没定，不是你不会选，是你挑得比别人认真。"),
    ("compare", "了解型"): ("共情", "看了几款还是没头绪很正常，条款本身就不好读。"),
    ("compare", "*"): ("认同", "看来看去定不下来，说明这几款确实像，值得花点时间分清。"),

    # ---- 健康告知卡壳：这是情绪最低的场景，先给安全感 ----
    ("underwriting", "了解型"): ("共情", "卡在这一步太正常了，不是你不会填，是没人把这个说清楚过。"),
    ("underwriting", "*"): ("减压", "健康告知不用一次填对，我们可以边填边问。"),

    # ---- 用户提问：肯定对方的提问质量（赋能） ----
    ("qa", "*"): ("赋能", "你问到了关键点上，这个确实是最容易被忽略的一条。"),

    # ---- 续保 ----
    ("renew", "*"): ("减压", "续保比第一次投保简单得多，不用重新考试。"),

    # ---- 风险会话：只承接情绪，不给任何方案 ----
    ("handoff", "*"): ("共情", "你说的情况我认真记下了，先不急着下结论。"),
}


def empathy(intent: str, user_type: str, emotion: dict | None = None) -> tuple[str, str]:
    """返回 (动作类型, 情绪价值句)。

    情绪分低于 0.45 时，去掉"赋能"类的表述 —— 对方正低落，讲"你已经很棒了"
    会显得敷衍，此时只该给减压或共情。
    """
    hit = EMPATHY_LIB.get((intent, user_type)) or EMPATHY_LIB.get((intent, "*"))
    if not hit:
        hit = ("共情", "你在这个环节停下来，一定有你的顾虑，我们慢慢说。")
    kind, line = hit
    low = bool(emotion and emotion.get("score", 0.5) < 0.45)
    if low and kind == "赋能":
        kind, line = ("减压", "不用急着做决定，我们先把不清楚的地方说清楚。")
    return kind, line


# =====================================================================
# 一句话洞察：解释"你为什么卡在这儿"，把用户从情绪拉回问题
# =====================================================================
INSIGHT_LIB: dict[str, str] = {
    "opening": "你犹豫的大概率不是这几十块，而是怕买错。",
    "objection": "真正贵的不是这笔保费，是生病时那笔一次性要掏的钱。",
    "compare": "差别其实就集中在三个变量上：免赔额、保证续保年限、免责条款宽严。",
    "underwriting": "如实告知不会让你更容易被拒保，反而是在保护你后面的理赔。",
    "renew": "续保看两件事就够：费率变了没、保障变了没。",
    "qa": "",
    "handoff": "",
}


def insight(intent: str, user_type: str = "") -> str:
    return INSIGHT_LIB.get(intent, "")


# =====================================================================
# 软引导（推动作）：低压力，把决定权交还用户
# =====================================================================
PUSH_LIB = {
    "加购未付": "不着急定，关键数字我先放这儿。",
    "浏览多款": "预算给我，我按现金流排成表。",
    "健康告知卡壳": "我陪你逐题过，拿不准的走智能核保。",
    "续保到期": "一键续保入口我放消息里了，不用重填资料。",
    "理赔进度": "进度我盯着，有节点第一时间告诉你。",
    "风险会话": "我先把你的情况同步给人工客服，30 秒内接入。",
    "用户主动咨询": "想细看哪条，我把条款原文调给你。",
    "画像风险标记": "想细看哪条，我把条款原文调给你。",
}

_PUSH_BY_INTENT = {
    "qa": "想细看哪条，我把条款原文调给你。",
    "compare": "预算给我，我按现金流排成表。",
    "underwriting": "我陪你逐题过，拿不准的直接走智能核保。",
}


def soft_push(intent: str, scene: str, addr: str = "你") -> str:
    """推动作。不带紧迫感施压，不复述内部指令语。"""
    if intent == "handoff":
        return ""
    # 纯问答场景不该出现"排成表"这类销售动作，按意图优先
    base = _PUSH_BY_INTENT.get(intent) or PUSH_LIB.get(scene) or "有问题随时喊我。"
    return base.replace("你", addr)


# =====================================================================
# 事实压缩：气泡只保留"一句话级别"的事实，明细交给下方卡片
# =====================================================================
def price_of(premium_display: str) -> str:
    """从 "32元/月；380元/年" 里取月缴；没有月缴则取首段。"""
    segs = [s.strip() for s in (premium_display or "").split("；") if s.strip()]
    for s in segs:
        if "/月" in s:
            return s
    return segs[0] if segs else ""


def clip(text: str, n: int) -> str:
    """按句读截断，尽量不在句子中间砍断；不留下悬空的逗号/顿号。"""
    text = (text or "").strip()
    if len(text) <= n:
        return _close(text)
    head = text[:n]
    for mark in ("。", "；", "，", "、"):
        idx = head.rfind(mark)
        if idx >= n * 0.5:
            return _close(head[:idx + 1])
    return head.rstrip("，、；") + "…"


def _close(text: str) -> str:
    """截断句收口：补句号、把结尾的逗号/顿号换成句号，避免读起来像没说完。"""
    text = re.sub(r"[，、]$", "。", (text or "").strip())
    if text and text[-1] not in "。！？；：…":
        text += "。"
    return text


# 分点展示的意图：结构化内容排成列表，读起来不累
BULLET_INTENTS = {"compare", "underwriting"}


def compact_product(item: dict, style: str = "lead") -> str:
    """把产品事实压成一句话。

    lead  —— 主推一款：名称 + 价格 + 最有力的一条卖点
    brief —— 对比用：名称 + 一条核心差异 + 价格
    """
    name = item.get("name", "")
    price = price_of(item.get("premium", ""))
    hl = (item.get("highlights") or [""])[0]
    if style == "brief":
        return clip(f"{name}：{hl}，{price}", 30)
    if price:
        return clip(f"{name}：{price}，{hl}", 34)
    return clip(f"{name}：{hl}", 30)


def compact_clause(hit: dict) -> str:
    """条款证据压成一句人话，不整段搬运原文。"""
    title = hit.get("title", "")
    body = hit.get("text", "")
    first = re.split(r"(?<=[。；])", body)[0].strip() if body else ""
    return clip(f"{title}：{first}", 40)


def compact_uw_row(row: dict) -> str:
    """核保预判压成一句。条件里的临床区间（如 140-159/90-99mmHg）对用户无意义，
    去掉后只留分级结论，避免既占字数又在句子中间被砍断。
    """
    cond = re.sub(r"[（(][^）)]*[）)]", "", row.get("condition", ""))
    cond = re.sub(r"\s+", "", cond)
    return clip(f"{cond}：医疗{row.get('medical', '')}，重疾{row.get('ci', '')}", 40)


def compact_term(tr: dict) -> str:
    return clip(f"{tr.get('term', '')}就是{tr.get('plain', '')}", 32)


# =====================================================================
# 称谓：年纪偏长用「您」，其余用「你」
# =====================================================================
def honorific(age: int | None, user_type: str = "") -> str:
    if user_type == "了解型":
        return "您"
    return "您" if (age or 0) >= 45 else "你"


def to_honorific(text: str, addr: str) -> str:
    """统一改写第二人称为目标称谓。"""
    if addr == "您":
        return text.replace("你", "您")
    return text.replace("您", "你")


# =====================================================================
# 预算执行器：按优先级贪心装填，保证情绪价值一定入选
# =====================================================================
def assemble(parts: dict[str, list[str] | str], intent: str, addr: str = "你") -> dict:
    """把各桶内容装进长度预算，返回成品文案与装填明细（供 Trace 展示）。

    判定用的是"渲染后真实长度"，而不是各段字面长度之和 —— 分点意图会额外
    产生换行与「· 」前缀，按字面长度记账会稳定超预算。
    """
    budget = LENGTH_BUDGET.get(intent, 100)
    order = FRAME_ORDER.get(intent, ["empathy", "insight", "facts", "push"])

    norm: dict[str, list[str]] = {}
    for k, v in parts.items():
        items = v if isinstance(v, list) else ([v] if v else [])
        norm[k] = [to_honorific(i, addr) for i in items if i]

    # 情绪价值永不裁剪
    picked: list[tuple[str, str]] = [("empathy", t) for t in norm.get("empathy", [])]
    dropped: list[dict] = []

    for bucket in order:
        if bucket == "empathy":
            continue
        for item in norm.get(bucket, []):
            trial = picked + [(bucket, item)]
            if len(_join(trial, intent)) <= budget:
                picked = trial
            else:
                dropped.append({"bucket": bucket, "text": item,
                                "reason": f"超出 {intent} 预算 {budget} 字"})

    if not picked:
        return {"text": "我在，你说。", "budget": budget, "used": 3, "dropped": dropped,
                "picked": []}

    text = _join(picked, intent)
    return {
        "text": text,
        "budget": budget,
        "used": len(text),
        "dropped": dropped,
        "picked": [{"bucket": b, "text": t} for b, t in picked],
    }


def _join(picked: list[tuple[str, str]], intent: str) -> str:
    """结构化意图（对比 / 核保结论表）排成分点列表，其余场景连成一段自然话。"""
    if intent not in BULLET_INTENTS:
        return "".join(t for _, t in picked).strip()

    lead = "".join(t for b, t in picked if b in ("empathy", "insight"))
    facts = [t for b, t in picked if b == "facts"]
    tail = "".join(t for b, t in picked if b == "push")
    blocks = [b for b in (lead,
                          "\n".join(f"· {f}" for f in facts),
                          tail) if b]
    return "\n".join(blocks).strip()


def emotion_label(kind: str) -> str:
    return {"认同": "认同感", "共情": "共情感", "减压": "减压感", "赋能": "赋能在"}.get(kind, kind)
