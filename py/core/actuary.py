"""精算数字 API（Mock）

设计红线来自项目文档：「LLM 只理解 + 只调工具，不算数、不决策」。
所有金额、保额、年限、比例一类"数字"必须由本模块给出，
LLM 输出中出现的数字一律要与之逐位对齐，否则由 G1 规则引擎拦截。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

# 精算口径：以产品 × 年龄段 × 性别 给出确定性数值
_RATE_TABLE: dict[str, dict[str, dict[str, Any]]] = {
    "P-MED-001": {
        "base": {
            "premium_month": 32,
            "premium_year": 384,
            "coverage": 4000000,
            "deductible": 10000,
            "renew_guarantee_years": 20,
        },
        "factors": {"age": 0.021, "male": 1.06},
    },
    "P-CI-002": {
        "base": {
            "premium_year": 3180,
            "premium_month": 265,
            "coverage": 500000,
            "pay_years": 20,
            "waiting_days": 90,
        },
        "factors": {"age": 0.038, "male": 1.12},
    },
    "P-ANN-003": {
        "base": {"pay_per_year": 50000, "pay_years": 10, "start_age": 60},
        "factors": {"age": 0.01, "male": 1.0},
    },
    "P-ACC-004": {
        "base": {"premium_year": 158, "coverage": 1000000, "medical": 50000, "deductible": 0},
        "factors": {"age": 0.0, "male": 1.0},
    },
    "P-CHI-005": {
        "base": {"premium_year": 480, "coverage": 2000000, "deductible": 0, "reimburse": 0.8},
        "factors": {"age": 0.03, "male": 1.0},
    },
}


@dataclass
class Quote:
    product_id: str
    product_name: str
    premium_display: str
    coverage_display: str
    key_numbers: dict[str, Any]
    source: str = "actuary-service v2.4.1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _round_to(x: float, step: int) -> int:
    return int(round(x / step) * step)


def quote(product_id: str, product_name: str, age: int, gender: str) -> Quote:
    """返回确定性精算结果。同一入参永远得到同一数字。"""
    cfg = _RATE_TABLE.get(product_id)
    if not cfg:
        return Quote(product_id, product_name, "以投保页为准", "以条款为准", {}, source="actuary-service v2.4.1")

    base = dict(cfg["base"])
    f = cfg["factors"]
    age_delta = max(0, age - 30) * f.get("age", 0.0)
    sex_factor = f.get("male", 1.0) if gender == "男" else 1.0

    if "premium_year" in base:
        base["premium_year"] = _round_to(base["premium_year"] * (1 + age_delta) * sex_factor, 10)
    if "premium_month" in base:
        base["premium_month"] = _round_to(base["premium_year"] / 12 * (1 + age_delta) * sex_factor, 1)
    if "premium_month" in base and "premium_year" not in cfg["base"]:
        base["premium_year"] = base["premium_month"] * 12

    display_map = {
        "premium_month": f"{base.get('premium_month')}元/月",
        "premium_year": f"{base.get('premium_year')}元/年",
        "pay_per_year": f"{base.get('pay_per_year')}元/年 × {base.get('pay_years')}年",
    }
    premium_display = "；".join(v for k, v in display_map.items() if k in base and v)

    if "coverage" in base:
        coverage_display = f"最高{base['coverage'] // 10000}万"
    else:
        coverage_display = f"{base.get('start_age')}岁起领取"

    numbers = {
        "premium_month": base.get("premium_month"),
        "premium_year": base.get("premium_year"),
        "coverage": base.get("coverage"),
        "coverage_wan": (base["coverage"] // 10000) if base.get("coverage") else None,
        "deductible": base.get("deductible"),
        "deductible_wan": (base["deductible"] // 10000) if base.get("deductible") is not None else None,
        "renew_guarantee_years": base.get("renew_guarantee_years"),
        "waiting_days": base.get("waiting_days"),
        "pay_years": base.get("pay_years"),
        "reimburse_pct": int(base["reimburse"] * 100) if base.get("reimburse") else None,
        "start_age": base.get("start_age"),
        "pay_per_year": base.get("pay_per_year"),
        "medical": base.get("medical"),
    }
    numbers = {k: v for k, v in numbers.items() if v is not None}
    return Quote(product_id, product_name, premium_display, coverage_display, numbers)


def allowed_numbers(q: Quote) -> set[str]:
    """允许出现在对外话术里的数字集合（字符串形式，含常见单位写法）。"""
    out: set[str] = set()
    for k, v in q.key_numbers.items():
        out.add(str(v))
        if k.endswith("_wan"):
            continue
        if isinstance(v, int) and v >= 10000:
            out.add(str(v // 10000))
        if isinstance(v, int):
            out.add(f"{v:,}")
    return out


def batch_quotes(profile: dict, products: list[dict], product_ids: list[str]) -> list[Quote]:
    age = int(profile.get("age", 30))
    gender = profile.get("gender", "女")
    index = {p["id"]: p for p in products}
    result = []
    for pid in product_ids:
        p = index.get(pid)
        if not p:
            continue
        result.append(quote(pid, p["name"], age, gender))
    return result
