"""从 A 股公司（深交所上市）的定期报告和业绩快报（中文原文 PDF）中提取业绩数据（海康威视已在 2014 年至今的全部报告上验证）。

报告类型决定取哪些数：
  一季度报告   合并利润表的本期、上期（单季数）
  半年度报告   合并利润表的本期、上期（年初至今的累计数，上半年）
  三季度报告   第三季度单季数只在“主要会计数据和财务指标”表里（表里第一列就是本报告期）
  年度报告     合并利润表的本期、上期（全年累计数）
  业绩快报     年度的初步数据（未经会计师审计），在年报之前发布：营收、净利润、EPS、净资产、股本
营收取营业总收入，净利润取归属于母公司股东的净利润，EPS 取基本每股收益；金额单位是元，换算成百万元。
净资产取资产负债表的归属于母公司所有者权益合计；股数取资产负债表的股本（期末总股本，面值 1 元，股数 = 股本金额）。
定期报告的主要数据表里的第一列（本报告期）与合并报表同一项必须一致，否则报错。
"""

from __future__ import annotations

import io
import re

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.parsed import Parsed

NUM = r"(-?[\d,]+(?:\.\d+)?|\(\s*[\d,]+(?:\.\d+)?\s*\))"
MILLION = 1e6
REPORT_TITLE_RE = re.compile(r"^(?P<year>\d{4})年(?P<kind>第一季度|一季度|半年度|第三季度|三季度|年度)报告")
EXPRESS_TITLE_RE = re.compile(r"^(?P<year>\d{4})年(?P<kind>半年度|度)业绩快报")
QUARTER_OF = {"第一季度": 1, "一季度": 1, "半年度": 2, "第三季度": 3, "三季度": 3, "年度": 4, "度": 4}


def clean_text(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"(?<=[一-鿿（）]) (?=[一-鿿（）])", "", text)  # PDF 在汉字之间断行时会插入空格
    text = re.sub(r"(\d,\d{3}(?:,\d{3})*\.\d) (\d)(?![\d,])", r"\1\2", text)  # 小数的最后一位被拆开，如“158.1 1”
    # 整数部分最后一组被拆开，如“9,335,806,1 14.00”：前面不足三位、加上后面刚好三位的才合并
    return re.sub(
        r"(?<![\d,])(\d{1,3}(?:,\d{3})*,)(\d{1,2}) (\d{1,2})(?![\d,])",
        lambda m: m.group(1) + m.group(2) + m.group(3) if len(m.group(2) + m.group(3)) == 3 else m.group(0),
        text,
    )


def read_text(pdf: bytes) -> str:
    return clean_text(" ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages))


def to_number(token: str) -> float:
    token = token.strip()
    if token.startswith("("):
        return -float(token.strip("()").replace(",", ""))
    return float(token.replace(",", ""))


# 年度报告和半年度报告的报表里，项目名后面有一列“附注”，如“(五)45”“七、61”
NOTE = r"(?:(?:[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、)\s?\d+(?:、\d+)*\s+)?"
FOOTNOTE = r"(?:\[\s*注\s*\d*\s*\])?"  # 主要数据表里的脚注标记，如“[注 2]”


def row(text: str, label: str, count: int, start: int = 0, optional: str = "") -> tuple[list[float], int]:
    """label 后面紧跟的 count 个数（中间可以有脚注标记或附注编号），返回（数值, 这一行在文本里的结束位置）。找不到就报错。"""
    m = re.compile(rf"(?:{label})\s*{optional}\s*{NOTE}" + r"\s+".join([NUM] * count)).search(text, start)
    if not m:
        raise ValueError(f"没有找到「{label}」这一行")
    return [to_number(g) for g in m.groups()], m.end()


def parse_cn_report(title: str, pdf: bytes) -> Parsed:
    m = REPORT_TITLE_RE.match(title)
    if not m:
        raise ValueError(f"无法从标题识别报告期: {title}")
    quarter = QUARTER_OF[m.group("kind")]
    period = pd.Period(year=int(m.group("year")), quarter=quarter, freq="Q")
    text = read_text(pdf)

    # 主要会计数据和财务指标表：第一列是本报告期（第三季度单季数只能从这里取）
    key_start = text.find("主要会计数据和财务指标")
    if key_start < 0:
        raise ValueError("没有找到“主要会计数据和财务指标”")
    key = {
        "revenue": row(text, r"营业收入（元）", 1, key_start, FOOTNOTE)[0][0],
        "profit": row(text, r"归属于上市公司股东的净利润（元）", 1, key_start, FOOTNOTE)[0][0],
        "eps": row(text, r"基本每股收益（元\s*/\s*股）", 1, key_start, FOOTNOTE)[0][0],
    }

    # 合并报表：本期、上期（上期按本期报告里的口径，已含重述）
    revenue, pos = row(text, r"一、营业(?:总)?收入", 2)
    profit, pos = row(text, r"归属于母公司(?:所有者|股东)的净利润", 2, pos)
    eps, _ = row(text, r"基本每股收益(?:\s*[（(]\s*元\s*/\s*股\s*[)）])?", 2, pos)
    values = {"revenue": [v / MILLION for v in revenue], "profit": [v / MILLION for v in profit], "eps": eps}

    equity, _ = row(text, r"归属于母公司(?:所有者|股东)权益合计", 2)
    share_capital, _ = row(text, r"(?:实收资本（或股本）|(?<!总)股本)", 2)
    values["equity"] = [equity[0] / MILLION, equity[1] / MILLION]
    values["wa_shares"] = [share_capital[0] / MILLION]  # 期末总股本（百万股）

    quarterly = quarter in (1, 3)
    if quarter == 3:
        # 第三季度的合并利润表是年初至今，单季数取主要数据表的第一列
        values["revenue"] = [key["revenue"] / MILLION]
        values["profit"] = [key["profit"] / MILLION]
        values["eps"] = [key["eps"]]
    else:
        for name, scale in (("revenue", MILLION), ("profit", MILLION), ("eps", 1.0)):
            if abs(values[name][0] - key[name] / scale) > (1e-6 if scale > 1 else 0.0006) * max(1.0, abs(values[name][0])):
                raise ValueError(f"{name}: 主要数据表 {key[name]} 与合并利润表 {values[name][0] * scale} 不一致")
    return Parsed("CNY", values, period, quarterly)


def parse_cn_express(title: str, pdf: bytes) -> Parsed:
    """业绩快报：年度（或半年度）的初步数据。数值是累计数（本期、上年同期）。"""
    m = EXPRESS_TITLE_RE.match(title)
    if not m:
        raise ValueError(f"无法从标题识别报告期: {title}")
    quarter = 2 if m.group("kind") == "半年度" else 4
    period = pd.Period(year=int(m.group("year")), quarter=quarter, freq="Q")
    text = read_text(pdf)
    revenue, _ = row(text, r"营业总收入", 2, 0, FOOTNOTE)
    profit, _ = row(text, r"(?<!后的)归属于上市公司股东的净利润", 2, 0, FOOTNOTE)
    eps, _ = row(text, r"基本每股收益", 2, 0, r"(?:（元）)?" + FOOTNOTE)
    equity, _ = row(text, r"归属于上市公司股东的所有者权益", 2, 0, FOOTNOTE)
    shares, _ = row(text, r"股本", 2, 0, FOOTNOTE)
    values = {
        "revenue": [v / MILLION for v in revenue],
        "profit": [v / MILLION for v in profit],
        "eps": eps,
        "equity": [equity[0] / MILLION, equity[1] / MILLION],
        "wa_shares": [shares[0] / MILLION],
    }
    return Parsed("CNY", values, period, quarterly=False)
