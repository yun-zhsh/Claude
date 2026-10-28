"""东方财富数据中心：港股财务主要指标（TTM 每股收益等）。

注意：接口的 CURRENCY 字段对港股一律标为 HKD，但数值实际是财报币种
（腾讯为人民币），所以财报币种由配置文件指定，不信任该字段。
数值也没有按拆股调整，需要调用方处理。
"""

from __future__ import annotations

import pandas as pd

from valuation_monitor.providers.http import HttpClient

DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
ANNUAL_DATE_TYPE = "001"


def to_secucode(symbol: str) -> str:
    """Yahoo 代码转东方财富代码：0700.HK -> 00700.HK。"""
    code, _, market = symbol.upper().partition(".")
    if market != "HK":
        raise ValueError(f"暂只支持港股，收到 {symbol}")
    return f"{code.zfill(5)}.HK"


class EastmoneyProvider:
    def __init__(self, http: HttpClient):
        self.http = http

    def financial_indicators(self, symbol: str) -> pd.DataFrame:
        """返回每个报告期的指标，列：report_date, is_annual, eps_ttm。按报告期升序。"""
        secucode = to_secucode(symbol)
        rows: list[dict] = []
        page = 1
        while True:
            data = self.http.get_json(
                DATACENTER_URL,
                params={
                    "reportName": "RPT_HKF10_FN_MAININDICATOR",
                    "columns": "REPORT_DATE,DATE_TYPE_CODE,EPS_TTM",
                    "filter": f'(SECUCODE="{secucode}")',
                    "pageNumber": page,
                    "pageSize": 500,
                    "sortTypes": 1,
                    "sortColumns": "REPORT_DATE",
                    "source": "F10",
                    "client": "PC",
                },
            )
            result = data.get("result")
            if not result:
                raise RuntimeError(f"东方财富没有返回 {secucode} 的财务数据: {data.get('message')}")
            rows.extend(result["data"])
            if page >= result.get("pages", 1):
                break
            page += 1

        df = pd.DataFrame(rows)
        return pd.DataFrame(
            {
                "report_date": pd.to_datetime(df["REPORT_DATE"]),
                "is_annual": df["DATE_TYPE_CODE"] == ANNUAL_DATE_TYPE,
                "eps_ttm": pd.to_numeric(df["EPS_TTM"], errors="coerce"),
            }
        ).sort_values("report_date", ignore_index=True)
