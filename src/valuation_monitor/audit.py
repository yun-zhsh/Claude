"""Write out how each valuation metric is calculated, for the download files and the chart tooltips, so users can check it step by step.

A "record" (detail) holds everything that became available when one filing was published; it is produced by
filings._ttm_row:
  periods          the periods covered by the TTM, oldest to newest, e.g. ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]
  eps / non_ifrs_eps / revenue   the value of each period (Component); None as a whole when a period is missing
  profit, eps_latest, shares     the latest period's net profit attributable to owners, its basic EPS, and the share count
  equity                         equity attributable to owners at the end of the latest period
All formulas are produced here, so the CSV downloads and the web tooltips show exactly the same text.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    period: str
    value: float
    derived: bool = False  # obtained by subtracting cumulative figures (e.g. Q4 = full year - first three quarters)
    raw: float | None = None  # the original figure in the filing before the split adjustment (None if not adjusted)


def num(value: float | None) -> str:
    """Thousands separators, at most 4 decimals with trailing zeros removed; negatives in parentheses so they fit inside a sum."""
    if value is None:
        return "-"
    text = f"{abs(value):,.4f}".rstrip("0").rstrip(".")
    return f"({'-' + text})" if value < 0 else text


def sum_terms(components: list[Component]) -> str:
    return " + ".join(f"{c.period} {num(c.value)}{'*' if c.derived else ''}" for c in components)


def sum_formula(components: list[Component] | None, total: float | None) -> str:
    if not components or total is None:
        return ""
    return f"{sum_terms(components)} = {num(total)}"


SHARES_UNIT = "millions of shares; millions of ADS for ADRs"


def shares_formula(rec: dict) -> str:
    """Where the share count comes from: the number disclosed in the filing first; implied (profit / basic EPS) only if none is disclosed."""
    if rec.get("shares") is None:
        return ""
    implied = rec.get("shares_implied")
    check = f"; cross-check: net profit {num(rec['profit'])} / basic EPS {num(rec['eps_latest'])} implies {num(implied)}" if implied else ""
    parts = rec.get("shares_parts")
    if rec.get("shares_source") == "derived" and parts:
        earlier = " + ".join(f"{x['period']} {num(x['shares'])} x {x['days']} days" for x in parts["parts"])
        return (
            f"The filing only discloses the full-year weighted average shares {num(parts['full_year'])}, so {rec['period_label']} is derived: "
            f"({num(parts['full_year'])} x {parts['year_days']} days - ({earlier})) / {parts['days']} days = {num(rec['shares'])} "
            f"({SHARES_UNIT}){check}"
        )
    if rec.get("shares_source") == "reported":
        basis = "; the filing discloses the full-year figure" if rec.get("shares_basis") else ""
        label = rec.get("shares_label", "basic EPS weighted average shares")
        return f"{rec['period_label']} {label} disclosed in the filing: {num(rec['shares'])} ({SHARES_UNIT}{basis}){check}"
    if implied is None:
        return ""
    return (
        f"The filing does not disclose the share count; implied from net profit {num(rec['profit'])} / basic EPS {num(rec['eps_latest'])} "
        f"= {num(rec['shares'])} ({rec['period_label']}, {SHARES_UNIT})"
    )


def ratio_formula(numerator_name: str, numerator: float | None, denominator_name: str, denominator: float | None, result: float | None) -> str:
    if numerator is None or denominator is None or result is None:
        return ""
    return f"{numerator_name} {num(numerator)} / {denominator_name} {num(denominator)} = {num(result)}"


def split_notes(components: list[Component] | None) -> list[str]:
    return [f"{c.period}: the filing says {num(c.raw)}, adjusted to {num(c.value)} for the stock split" for c in components or [] if c.raw is not None]


def eps_lines(rec: dict, series: str, name: str) -> list[str]:
    comps = rec.get(series)
    lines = [f"{name} = {sum_formula(comps, rec.get(series + '_ttm'))}"]
    return lines + split_notes(comps)


def ps_lines(rec: dict) -> list[str]:
    return [
        f"Revenue per share (TTM) = {ratio_formula('TTM revenue', rec.get('revenue_ttm'), 'shares', rec.get('shares'), rec.get('revenue_per_share_ttm'))}",
        f"TTM revenue = {sum_formula(rec.get('revenue'), rec.get('revenue_ttm'))} (millions)",
        f"Shares = {shares_formula(rec)}",
    ]


def pb_lines(rec: dict) -> list[str]:
    return [
        f"Book value per share = {ratio_formula('equity at period end', rec.get('equity'), 'shares', rec.get('shares'), rec.get('book_value_per_share'))}",
        f"Equity attributable to owners at the end of {rec['period_label']} (millions) = {num(rec.get('equity'))}",
        f"Shares = {shares_formula(rec)}",
    ]


EXPLAINERS = {
    "eps_ttm": lambda rec: eps_lines(rec, "eps", "TTM EPS (sum of the basic EPS of each period)"),
    "non_ifrs_eps_ttm": lambda rec: eps_lines(rec, "non_ifrs_eps", "TTM Non-IFRS/Non-GAAP EPS (sum of each period)"),
    "revenue_per_share_ttm": ps_lines,
    "book_value_per_share": pb_lines,
}


def explain(column: str, rec: dict) -> list[str]:
    """How a per-share item (TTM EPS, revenue per share, book value per share, ...) is calculated, line by line."""
    lines = [line for line in EXPLAINERS[column](rec) if line]
    if any(c.derived for key in ("eps", "non_ifrs_eps", "revenue") for c in rec.get(key) or []):
        lines.append("* obtained by subtracting cumulative figures (e.g. Q4 = full year - first three quarters)")
    if rec.get("note"):
        lines.append(rec["note"])
    if rec.get("provisional"):
        lines.append("This period uses preliminary data (earnings call presentation or express report, before the external review/audit); it is replaced automatically when the official report is out")
    return lines
