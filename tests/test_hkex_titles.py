import pytest

from valuation_monitor.providers.hkex import RESULTS_TITLE_RE


@pytest.mark.parametrize(
    "title,expected",
    [
        ("ANNOUNCEMENT OF THE RESULTS FOR THE THREE MONTHS ENDED 31 MARCH 2026", True),
        ("ANNOUNCEMENT OF THE ANNUAL RESULTS FOR THE YEAR ENDED 31 DECEMBER 2023", True),
        ("INTERIM RESULTS ANNOUNCEMENT FOR THE SIX MONTHS ENDED 30 JUNE 2026", True),
        ("Interim Results Announcement for the Six Months Ended 30 June 2021", True),
        ("ANNUAL RESULTS ANNOUNCEMENT FOR THE YEAR ENDED 31 DECEMBER 2025", True),
        ("POLL RESULTS OF ANNUAL GENERAL MEETING HELD ON 27 MAY 2026", False),
        ("POLL RESULTS FOR THE 2026 ANNUAL GENERAL MEETING", False),
        ("POLL RESULTS OF THE EXTRAORDINARY GENERAL MEETING HELD ON 22 SEPTEMBER 2014", False),
    ],
)
def test_results_title_filter(title, expected):
    assert bool(RESULTS_TITLE_RE.search(title) and "POLL RESULTS" not in title.upper()) is expected
