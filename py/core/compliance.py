"""4 道合规闸 —— 对齐文档「任何对外输出必过 4 道闸」

G1 规则引擎     ：硬规则（数字一致性 / 必带风险提示 / 禁用句式结构）
G2 NLI 蕴含     ：输出是否被「条款原文 + 精算结果」蕴含，矛盾句直接摘除
G3 关键词审查   ：监管敏感词 / 绝对化用语 / 收益承诺类表述
G4 风险话术检测 ：风险等级评分，中风险改写、高风险转人工

拦截策略分三级：
  PASS      —— 原样输出
  SANITIZE  —— 摘除或改写违规片段后输出（记入"误杀率/改写率"）
  HANDOFF   —— 整条转人工，不下发
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------- G3 术语表 ----------------
BANNED_EXACT = {
    "稳赚不赔": "删除收益承诺类表述",
    "稳赚": "删除收益承诺类表述",
    "零风险": "删除绝对化表述",
    "保证收益": "不得承诺收益",
    "保证赔付": "不得承诺赔付",
    "一定能赔": "不得承诺赔付",
    "肯定能赔": "不得承诺赔付",
    "最好的": "不得使用绝对化用语",
    "最好的产品": "不得使用绝对化用语",
    "最便宜": "不得使用绝对化用语",
    "绝对": "不得使用绝对化用语",
    "100%赔付": "不得承诺赔付",
    "包赔": "不得承诺赔付",
    "稳赚不亏": "删除收益承诺类表述",
    "比银行存款划算": "不得与储蓄存款混同",
    "等于存款": "不得与储蓄存款混同",
    "代替存款": "不得与储蓄存款混同",
    "诱导退保": "不得诱导退保",
    "直接退了": "不得诱导退保",
}

# 需要上下文才能判定的绝对化用语。裸词匹配会误杀"第一次投保""第一步"这类
# 中性表述（实测把「续保比第一次投保简单得多」改成了「续保比次投保简单得多」）。
BANNED_PATTERNS = [
    (r"(全网|行业|市场|销量|排名|业内|市面上)\s*第一", "不得使用绝对化用语"),
    (r"第一\s*(名|品牌|选择)", "不得使用绝对化用语"),
]

# ---------------- G4 风险话术模式 ----------------
RISK_PATTERNS = [
    (r"退保.{0,6}(更|比较)?(划算|合适|推荐)", 0.45, "诱导退保倾向"),
    (r"(收益|回报).{0,8}(一定|必然|确定会|稳定|保证)", 0.40, "收益确定性表述"),
    # 弱化如实告知义务 —— 保险销售最严重的红线之一。
    # 早期只覆盖「不用告知健康状况」，实测漏掉了口语化的「不用写高血压」「随便填就行」。
    (r"(不用|无需|不必|可以不|没必要).{0,8}(写|填|报|提|告知|说明|申报).{0,10}"
     r"(高血压|糖尿病|病史|既往|疾病|住院|就医|手术|健康)", 0.60, "弱化如实告知义务"),
    # 语序倒装：健康情况在前，弱化动作在后（「您有高血压也不用告知」）
    (r"(高血压|糖尿病|病史|既往|疾病|住院|就医|手术|结节|体检异常).{0,10}"
     r"(不用|无需|不必|可以不|没必要).{0,6}(写|填|报|提|告知|说明|申报)", 0.60, "弱化如实告知义务"),
    (r"(不用|无需|不必).{0,6}(告知|说明).{0,4}(健康|病史)", 0.50, "弱化如实告知义务"),
    (r"健康告知.{0,10}(随便|随意|大致|大概)?(填|写|勾).{0,6}(就行|即可|没事|没关系)", 0.45,
     "弱化如实告知义务"),
    (r"(保险公司|公司|平台|后台|系统).{0,6}(查不到|查不出|发现不了|看不到|不会查)",
     0.55, "暗示隐瞒可行"),
    (r"(代替|等于).{0,4}(做|下).{0,4}决定", 0.35, "越权代客决策"),
    (r"(保证|承诺)(?!续保).{0,4}(赔付|收益|赚|不亏)", 0.45, "承诺性表述"),
    (r"限时.{0,4}(优惠|福利).{0,10}(失效|结束)", 0.15, "紧迫感话术（可控）"),
    (r"(贷款|借钱).{0,6}(买|投保)", 0.55, "不当引导融资投保"),
]

# 收益类产品必须出现的非承诺提示（触发条件 → 必须出现的关键词）
YIELD_GUARD = {
    "trigger_re": r"(收益|回报).{0,10}(稳定|确定|保证|一定|划算|高)|分红|万能险|演示利益",
    "required": "不构成收益承诺",
    "note": "（提示：以上仅为合同约定内容，不构成收益承诺。）",
}

REQUIRED_DISCLOSURE_RULES = [
    # (触发条件, 必须出现的关键词, 说明)
    (("分红", "万能", "年金", "收益"), ("不构成收益承诺", "不是存款"), "收益类产品必须提示非收益承诺"),
    (("退保",), ("现金价值", "可能低于已交保费"), "提及退保必须提示损失"),
]


@dataclass
class GateResult:
    gate: str
    name: str
    status: str  # PASS | SANITIZE | BLOCK
    hits: list[dict] = field(default_factory=list)
    note: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate, "name": self.name, "status": self.status,
            "hits": self.hits, "note": self.note, "latency_ms": self.latency_ms,
        }


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；\n])", text)
    return [p.strip() for p in parts if p.strip()]


def _segments(text: str) -> list[tuple[str, str]]:
    """切句并保留原分隔符。逐句校验后必须能还原排版结构（换行 / 分点 / 顿号），
    否则摘句会把「· 分点列表」压成一整行，破坏可读性。
    """
    parts = re.split(r"([。！？；\n])", text)
    segs: list[tuple[str, str]] = []
    for i in range(0, len(parts), 2):
        body = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        segs.append((body, sep))
    return segs


# ---------------- G1 规则引擎 ----------------
def numbers_from_evidence(evidence: list[str]) -> set[str]:
    """从条款证据 / 精算结果 / 产品页事实中提取所有合法数字。"""
    out: set[str] = set()
    for e in evidence:
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", e):
            out.add(m.group().replace(",", ""))
    return out


def _money_numbers(text: str) -> list[str]:
    """只抽取"钱"类数字（金额 / 保额 / 比例），临床区间值不参与数字一致性校验。"""
    out: list[str] = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(万元|万|元|块)(?![\d])", text):
        if re.search(rf"{re.escape(m.group(1))}\s*[-~—]\s*", text):
            continue  # 区间值（如 140-159）视为临床描述，不做金额校验
        out.append(m.group(1).replace(",", ""))
    for m in re.finditer(r"(\d{1,3})\s*%", text):
        out.append(m.group(1))
    return out


def gate1_rules(text: str, actuary_numbers: set[str], has_risk_disclosure_required: bool) -> tuple[str, GateResult]:
    hits: list[dict] = []
    # 1) 数字一致性：输出里的金额/保额/比例必须能对齐精算结果或条款证据
    offenders: list[str] = []
    for digits in _money_numbers(text):
        if digits in actuary_numbers:
            continue
        offenders.append(digits)

    if offenders:
        hits.append({"type": "数字一致性", "value": sorted(set(offenders))[:5],
                     "action": "摘除未经精算API/条款证据校验的金额表述"})
        for o in sorted(set(offenders), key=len, reverse=True):
            text = re.sub(rf"{re.escape(o)}\s*(万元|万|元|块|%)", "（金额以投保页为准）", text, count=1)

    # 2) 必带风险提示
    if has_risk_disclosure_required:
        if "退保" in text and "现金价值" not in text:
            text += "（提示：犹豫期后退保仅退还现金价值，可能低于已交保费，请注意损失。）"
            hits.append({"type": "必带提示", "value": "退保损失提示", "action": "自动补全"})

    # 3) 禁用句式结构
    if re.search(r"(最好|唯一|一定).{0,6}买", text):
        hits.append({"type": "句式结构", "value": "施压式表达", "action": "标记待改写"})

    status = "SANITIZE" if hits else "PASS"
    return text, GateResult("G1", "规则引擎", status, hits,
                            "数字一致性与必带提示校验")


# ---------------- G2 NLI 蕴含 ----------------
CLAIM_MARKERS = ("赔", "保", "续保", "免赔", "等待期", "条款", "费率", "理赔", "保费",
                 "保额", "承保", "核保", "退保", "现金价值", "犹豫期", "免责", "保障")


def _is_claim_bearing(sent: str) -> bool:
    """只有"事实性论断"才需要被条款证据蕴含，过渡句/引导句不参与校验。"""
    if re.search(r"\d", sent):
        return True
    return any(m in sent for m in CLAIM_MARKERS)


def _tokens(text: str) -> list[str]:
    """词元化：汉字用 2-gram + 数字串。

    早期实现用 re.findall(r"[\\u4e00-\\u9fff]{2,}") 把整段连续汉字当成一个词元，
    结果任何一句正常长度的话都必然与证据零匹配（"续保比第一次投保简单得多"
    整体不是证据里的子串），使 G2 大面积误杀。改成字符 bigram 后，
    蕴含判断回到"用词是否被证据覆盖"的本意。
    """
    han = re.findall(r"[\u4e00-\u9fff]", text)
    grams = [han[i] + han[i + 1] for i in range(len(han) - 1)]
    return grams + re.findall(r"\d[\d,]*(?:\.\d+)?", text)


def gate2_nli(text: str, evidence: list[str], exempt: list[str] | None = None
              ) -> tuple[str, GateResult]:
    """判断输出的每个事实句是否被证据（条款原文 + 精算结果）蕴含。

    简化实现：以 bigram 覆盖率做蕴含打分，并对否定冲突做显式检测。

    exempt 是豁免清单（情绪价值句 / 一句话洞察）。这两类句子是关系性表述，
    不承载"保障范围 / 金额 / 责任"这类事实主张，用条款证据去校验它们
    属于闸门越界——早期实现会把「续保比第一次投保简单得多」整句摘掉，
    导致主动触达链路下发空文案。
    """
    exempt = [e.strip() for e in (exempt or []) if e and e.strip()]
    ev_tokens = set()
    for e in evidence:
        ev_tokens |= set(_tokens(e))

    # 没有条款证据可比对时不能凭空空删 —— 此时该由 G3 关键词闸兜底。
    # 早期实现在这里把整段删空，导致下游 G3/G4 面对空文本全部 PASS，
    # 对抗样本「稳赚不赔 / 不用告知」反而被放行。
    if not ev_tokens:
        return text, GateResult("G2", "NLI 蕴含", "PASS", [],
                                "无条款证据可比对，跳过蕴含校验（交由 G3 兜底）")

    kept, dropped = [], []
    for body, sep in _segments(text):
        sent = body.strip()
        if not sent:
            kept.append((body, sep))
            continue
        if len(sent) < 8 or not _is_claim_bearing(sent) or _is_exempt(sent, exempt):
            kept.append((body, sep))
            continue
        toks = _tokens(sent)
        if not toks:
            kept.append((body, sep))
            continue
        covered = sum(1 for t in toks if t in ev_tokens) / len(toks)
        contradiction = _has_negation_conflict(sent, evidence)
        if covered < 0.30 or contradiction:
            dropped.append({"sentence": sent, "coverage": round(covered, 3),
                            "reason": "与条款/精算证据矛盾或缺乏支撑"})
        else:
            kept.append((body, sep))

    new_text = "".join(b + s for b, s in kept)
    status = "SANITIZE" if dropped else "PASS"
    gate = GateResult("G2", "NLI 蕴含", status, dropped, "逐句校验是否被条款原文蕴含")
    gate.hits = dropped
    return new_text, gate


def _is_exempt(sent: str, exempt: list[str]) -> bool:
    """句子是否落在豁免清单里（互为子串即视为同一句，容忍标点差异）。"""
    key = re.sub(r"[，。；、！？\s]", "", sent)
    for e in exempt:
        ekey = re.sub(r"[，。；、！？\s]", "", e)
        if key and (key in ekey or ekey in key):
            return True
    return False


_NEG_PAIRS = [("不赔", "赔"), ("不能", "可以"), ("不会", "会"), ("不保证", "保证"), ("无需", "需要")]


def _has_negation_conflict(sent: str, evidence: list[str]) -> bool:
    for neg, pos in _NEG_PAIRS:
        if neg in sent:
            pos_form = sent.replace(neg, pos)
            for e in evidence:
                if pos_form[:12] in e and neg not in e:
                    return True
    return False


# ---------------- G3 关键词审查 ----------------
def gate3_keywords(text: str, detect_text: str | None = None) -> tuple[str, GateResult]:
    """监管敏感词与绝对化用语审查。

    命中判定始终基于 detect_text（原始输出）：如果某句先被 G2 当作无证据句摘掉，
    这里就再也看不到「稳赚不赔」，拦截记录会凭空消失，压测面板上表现为"没拦住"。
    改写动作仍作用在当前文本上；收益类提示的补全判定用当前文本
    （给已被删掉的句子补提示没有意义）。
    """
    probe = detect_text if detect_text is not None else text
    hits: list[dict] = []
    for word, reason in BANNED_EXACT.items():
        if word in probe:
            hits.append({"word": word, "reason": reason})
            text = text.replace(word, "")
    for pattern, reason in BANNED_PATTERNS:
        if re.search(pattern, probe):
            hits.append({"word": pattern, "reason": reason})
            text = re.sub(pattern, "", text)
    # 补全收益类必带提示
    if re.search(YIELD_GUARD["trigger_re"], text) and YIELD_GUARD["required"] not in text:
        text += YIELD_GUARD["note"]
        hits.append({"word": "—", "reason": "收益类表述补全非承诺提示", "type": "补全"})
    text = re.sub(r"[，、]{2,}", "，", text)
    text = re.sub(r"（\s*）", "", text)
    status = "SANITIZE" if hits else "PASS"
    return text, GateResult("G3", "关键词审查", status, hits, "监管敏感词与绝对化用语")


def clean_residue(text: str) -> str:
    """清理多道闸连续摘除后留下的标点残渣。

    例：一段被判违规整句摘除后，只剩 G4 补的提示被 G2 删掉留下的一个「）」。
    这类残渣会被当成正常文案下发，看起来像系统故障。
    """
    if not text:
        return ""
    text = re.sub(r"[（(]\s*[）)]", "", text)
    text = re.sub(r"[，、；]{2,}", "，", text)
    text = re.sub(r"^[\s，、；。！？）)】》]+", "", text)
    text = re.sub(r"[\s（(【《]+$", "", text)
    text = text.strip()
    # 通篇没有一个汉字/字母/数字，说明实质内容已被全部摘除
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
        return ""
    return text


# ---------------- G4 风险话术检测 ----------------
RISK_DISCLAIMER = "（以上信息供您参考，不代替保险公司的核保与承保结论。）"


def gate4_risk(text: str, user_type: str, handoff_signals: list[str],
               detect_text: str | None = None) -> tuple[str, GateResult, float]:
    """风险话术检测。

    detect_text 是「检测用文本」：始终传原始输出（未经过 G1~G3 摘除改写的那份）。
    否则一条「退保更划算」若先被 G2 当作无证据句摘掉，G4 就看不到风险信号，
    高风险会话会被静默放行 —— 这是安全底线，不能依赖上游改写结果。
    """
    probe = detect_text if detect_text is not None else text
    score = 0.0
    hits: list[dict] = []
    for pattern, weight, desc in RISK_PATTERNS:
        if re.search(pattern, probe):
            score += weight
            hits.append({"pattern": desc, "weight": weight})
    for sig in handoff_signals:
        weight = 0.65 if "高风险语义" in sig else 0.25
        score += weight
        hits.append({"pattern": f"转人工信号：{sig}", "weight": weight})
    if user_type == "高风险" or (user_type == "了解型" and "退保" in probe):
        score += 0.1

    score = min(1.0, score)
    if score >= 0.6:
        status = "BLOCK"
        note = "高风险：整条转人工，AI 不直接回复"
    elif score >= 0.2:
        status = "SANITIZE"
        note = "中风险：已改写/降级，并附风险提示"
        if "请注意" not in text and RISK_DISCLAIMER not in text:
            text += RISK_DISCLAIMER
    else:
        status = "PASS"
        note = "低风险"
    return text, GateResult("G4", "风险话术检测", status, hits, note), round(score, 3)


# ---------------- 闸门统一入口 ----------------
def run_gates(text: str, evidence: list[str], actuary_numbers: set[str],
              user_type: str, handoff_signals: list[str],
              needs_disclosure: bool, exempt: list[str] | None = None) -> dict[str, Any]:
    import time
    results: list[GateResult] = []
    original = text  # 风险检测基准：G1~G3 改写前的原始输出

    t0 = time.time()
    text, g1 = gate1_rules(text, actuary_numbers, needs_disclosure)
    g1.latency_ms = int((time.time() - t0) * 1000)
    results.append(g1)

    t0 = time.time()
    text, g2 = gate2_nli(text, evidence, exempt)
    g2.latency_ms = int((time.time() - t0) * 1000)
    results.append(g2)

    t0 = time.time()
    text, g3 = gate3_keywords(text, detect_text=original)
    g3.latency_ms = int((time.time() - t0) * 1000)
    results.append(g3)

    t0 = time.time()
    text, g4, risk = gate4_risk(text, user_type, handoff_signals, detect_text=original)
    g4.latency_ms = int((time.time() - t0) * 1000)
    results.append(g4)

    text = clean_residue(text)
    # 正文已被各闸摘光、只剩 G4 补的那句免责提示时，整条不再下发 ——
    # 单独甩一句免责声明出去，用户看到的是系统故障而不是合规保护。
    if text.strip() == RISK_DISCLAIMER:
        text = ""
    blocked = any(r.status == "BLOCK" for r in results)
    if blocked:
        # BLOCK 语义 = 整条不下发，交人工处理。保留原文会让"压下"看起来像"照发"。
        text = ""
    sanitized = any(r.status == "SANITIZE" for r in results)
    return {
        "gates": [r.to_dict() for r in results],
        "final_text": text,
        "blocked": blocked,
        "sanitized": sanitized,
        "risk_score": risk,
        "intercepted": blocked or sanitized,
        "summary": "已拦截并改写" if sanitized and not blocked else ("已拦截并转人工" if blocked else "4 道闸全部通过"),
    }
