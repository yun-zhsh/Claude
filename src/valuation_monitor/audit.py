"""把估值指标的计算过程写成文字，供下载文件和走势图上的提示框使用，方便用户逐项核对。

一份“业绩记录”（detail）对应一份公告发布后可用的全部数据，由 filings._ttm_row 生成：
  periods          TTM 覆盖的各期，从早到晚，如 ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]
  eps / non_ifrs_eps / revenue   各期的数值（Component），某期缺失时整项为 None
  profit, eps_latest, shares     最新一期的归母净利润、基本 EPS，以及反推的股数
  equity                         最新一期末的归母净资产
所有公式都由这里统一生成，下载的 CSV 和网页提示框看到的是同一套文字。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    period: str
    value: float
    derived: bool = False  # 是否由累计数相减得到（如第四季度 = 全年 − 前三季度）
    raw: float | None = None  # 拆股调整前公告里的原始数值（没有调整过为 None）


def num(value: float | None) -> str:
    """千位分隔，最多 4 位小数，去掉多余的 0；负数加括号，方便放进加法算式。"""
    if value is None:
        return "—"
    text = f"{abs(value):,.4f}".rstrip("0").rstrip(".")
    return f"({'-' + text})" if value < 0 else text


def sum_terms(components: list[Component]) -> str:
    return " + ".join(f"{c.period} {num(c.value)}{'*' if c.derived else ''}" for c in components)


def sum_formula(components: list[Component] | None, total: float | None) -> str:
    if not components or total is None:
        return ""
    return f"{sum_terms(components)} = {num(total)}"


def shares_formula(rec: dict) -> str:
    """股数的来源：公告披露的加权平均股数优先，公告没有披露才用净利润 ÷ 基本 EPS 反推。"""
    if rec.get("shares") is None:
        return ""
    unit = "百万股；ADR 为百万份 ADS"
    implied = rec.get("shares_implied")
    parts = rec.get("shares_parts")
    if rec.get("shares_source") == "derived" and parts:
        earlier = " + ".join(f"{x['period']} {num(x['shares'])} × {x['days']} 天" for x in parts["parts"])
        check = f"；对照：归母净利润 {num(rec['profit'])} ÷ 基本 EPS {num(rec['eps_latest'])} 反推 {num(implied)}" if implied else ""
        return (
            f"公告只披露了全年加权平均股数 {num(parts['full_year'])}，{rec['period_label']}由全年数推算："
            f"（{num(parts['full_year'])} × {parts['year_days']} 天 − （{earlier}））÷ {parts['days']} 天 = {num(rec['shares'])}"
            f"（百万股；ADR 为百万份 ADS）{check}"
        )
    if rec.get("shares_source") == "reported":
        basis = "，公告披露的是全年数" if rec.get("shares_basis") else ""
        check = f"；对照：归母净利润 {num(rec['profit'])} ÷ 基本 EPS {num(rec['eps_latest'])} 反推 {num(implied)}" if implied else ""
        return f"公告披露的{rec['period_label']}{rec.get('shares_label', '基本 EPS 加权平均股数')} {num(rec['shares'])}（{unit}{basis}）{check}"
    if implied is None:
        return ""
    return f"公告没有披露股数，用归母净利润 {num(rec['profit'])} ÷ 基本 EPS {num(rec['eps_latest'])} 反推 = {num(rec['shares'])}（{rec['period_label']}，{unit}）"


def ratio_formula(numerator_name: str, numerator: float | None, denominator_name: str, denominator: float | None, result: float | None) -> str:
    if numerator is None or denominator is None or result is None:
        return ""
    return f"{numerator_name} {num(numerator)} ÷ {denominator_name} {num(denominator)} = {num(result)}"


def split_notes(components: list[Component] | None) -> list[str]:
    return [f"{c.period} 公告原文是 {num(c.raw)}，按拆股记录调整为 {num(c.value)}" for c in components or [] if c.raw is not None]


def eps_lines(rec: dict, series: str, name: str) -> list[str]:
    comps = rec.get(series)
    lines = [f"{name} = {sum_formula(comps, rec.get(series + '_ttm'))}"]
    return lines + split_notes(comps)


def ps_lines(rec: dict) -> list[str]:
    return [
        f"每股营收 TTM = {ratio_formula('营收 TTM', rec.get('revenue_ttm'), '股数', rec.get('shares'), rec.get('revenue_per_share_ttm'))}",
        f"营收 TTM = {sum_formula(rec.get('revenue'), rec.get('revenue_ttm'))}（百万）",
        f"股数 = {shares_formula(rec)}",
    ]


def pb_lines(rec: dict) -> list[str]:
    return [
        f"每股净资产 = {ratio_formula('期末归母净资产', rec.get('equity'), '股数', rec.get('shares'), rec.get('book_value_per_share'))}",
        f"期末归母净资产（{rec['period_label']}末，百万）= {num(rec.get('equity'))}",
        f"股数 = {shares_formula(rec)}",
    ]


EXPLAINERS = {
    "eps_ttm": lambda rec: eps_lines(rec, "eps", "TTM EPS（基本 EPS 各期之和）"),
    "non_ifrs_eps_ttm": lambda rec: eps_lines(rec, "non_ifrs_eps", "TTM Non-IFRS/Non-GAAP EPS（各期之和）"),
    "revenue_per_share_ttm": ps_lines,
    "book_value_per_share": pb_lines,
}


def explain(column: str, rec: dict) -> list[str]:
    """某项每股数据（TTM EPS、每股营收、每股净资产……）是怎么算出来的，逐行说明。"""
    lines = [line for line in EXPLAINERS[column](rec) if line]
    if any(c.derived for key in ("eps", "non_ifrs_eps", "revenue") for c in rec.get(key) or []):
        lines.append("* 由累计数相减得到（如第四季度 = 全年 − 前三季度）")
    if rec.get("note"):
        lines.append(rec["note"])
    if rec.get("provisional"):
        lines.append("本期数据暂用初步数据（业绩发布会材料或业绩快报，审阅/审计前），正式报告发布后自动替换")
    return lines
