"""Registry of valuation metrics. To add a metric (PS, PB, ...), implement Metric and register it here."""

from valuation_monitor.metrics.base import Metric
from valuation_monitor.metrics.multiples import PB, PE, PS, NonGaapPE, NonIfrsPE

METRICS: dict[str, Metric] = {m.key: m for m in [PE(), NonIfrsPE(), NonGaapPE(), PS(), PB()]}

__all__ = ["METRICS", "Metric"]
