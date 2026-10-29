"""估值指标注册表。新增指标（PS、PB……）时实现 Metric 并在此注册。"""

from valuation_monitor.metrics.base import Metric
from valuation_monitor.metrics.multiples import PB, PE, PS, NonIfrsPE

METRICS: dict[str, Metric] = {m.key: m for m in [PE(), NonIfrsPE(), PS(), PB()]}

__all__ = ["METRICS", "Metric"]
