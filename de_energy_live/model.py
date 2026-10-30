from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TimeSeries:
    """A named numeric timeseries (power in MW unless noted)."""

    name: str
    unit: str = "MW"
    timestamps: list[datetime] = field(default_factory=list)
    values: list[float | None] = field(default_factory=list)

    def latest(self, before: datetime | None = None) -> tuple[datetime, float] | None:
        """Return (timestamp, value) of the most recent non-null point."""
        for ts, v in zip(reversed(self.timestamps), reversed(self.values)):
            if v is not None and (before is None or ts <= before):
                return ts, v
        return None

    def window(self, start: datetime, end: datetime) -> "TimeSeries":
        idx = [i for i, ts in enumerate(self.timestamps) if start <= ts <= end]
        return TimeSeries(
            name=self.name,
            unit=self.unit,
            timestamps=[self.timestamps[i] for i in idx],
            values=[self.values[i] for i in idx],
        )
