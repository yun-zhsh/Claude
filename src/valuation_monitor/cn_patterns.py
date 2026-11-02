"""Chinese-language patterns for reading Chinese filings.

CNINFO announcement titles and the text of Chinese periodic reports are in Chinese, so the patterns that
recognise them have to be too. This is the only module that contains Chinese, and none of it is ever displayed:
everything the user sees (titles, messages, column names) is English.
"""

from __future__ import annotations

import re

# --- CNINFO announcement titles -------------------------------------------------------------------------
TITLE_RE = re.compile(r"^(?P<year>\d{4})年(?P<kind>第一季度|一季度|半年度|第三季度|三季度|年度)报告(?:全文|正文)?(?:（更新后）)?$")
EXPRESS_RE = re.compile(r"^(?P<year>\d{4})年(?P<kind>半年度|度)业绩快报$")
QUARTER_OF = {"第一季度": 1, "一季度": 1, "半年度": 2, "第三季度": 3, "三季度": 3, "年度": 4, "度": 4}
# Titles containing any of these are summaries, English translations, ESG reports, corrections and the like.
EXCLUDED_TITLE_WORDS = ("摘要", "英文", "ESG", "社会责任", "内部控制", "审计", "环境", "更正", "取消", "补充", "说明", "预告")
EXPRESS_SEARCH_WORD = "业绩快报"
BODY_TEXT_WORD = "正文"  # a report is published twice, as "full text" and as "body text"; the full text is preferred

# --- text of the reports ---------------------------------------------------------------------------------
CJK_GAP_RE = r"(?<=[一-鿿（）]) (?=[一-鿿（）])"  # PDF text extraction inserts spaces where lines wrap inside Chinese text
# Annual and half-year statements have a note column after the item name: a Chinese numeral in brackets or followed by a separator, then the note number.
NOTE = r"(?:(?:[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、)\s?\d+(?:、\d+)*\s+)?"
FOOTNOTE = r"(?:\[\s*注\s*\d*\s*\])?"  # footnote marker in the key-data table, e.g. "[note 2]"

KEY_TABLE = "主要会计数据和财务指标"
KEY_REVENUE = r"营业收入（元）"
KEY_PROFIT = r"归属于上市公司股东的净利润（元）"
KEY_EPS = r"基本每股收益（元\s*/\s*股）"
INCOME_REVENUE = r"一、营业(?:总)?收入"
INCOME_PROFIT = r"归属于母公司(?:所有者|股东)的净利润"
INCOME_EPS = r"基本每股收益(?:\s*[（(]\s*元\s*/\s*股\s*[)）])?"
EQUITY = r"归属于母公司(?:所有者|股东)权益合计"
SHARE_CAPITAL = r"(?:实收资本（或股本）|(?<!总)股本)"

EXPRESS_REVENUE = r"营业总收入"
EXPRESS_PROFIT = r"(?<!后的)归属于上市公司股东的净利润"
EXPRESS_EPS = r"基本每股收益"
EXPRESS_EPS_UNIT = r"(?:（元）)?"
EXPRESS_EQUITY = r"归属于上市公司股东的所有者权益"
EXPRESS_SHARES = r"股本"


def is_wanted_page(text: str) -> bool:
    """Is this page of a report one the parser needs (key-data table, income statement, balance sheet)?"""
    key_table = KEY_TABLE in text and "营业收入" in text
    income = re.search(INCOME_REVENUE, text)
    balance = ("归属于母公司" in text and "权益合计" in text) or (re.search(r"(?<!总)股本", text) and "资本公积" in text)
    return bool(key_table or income or balance)
