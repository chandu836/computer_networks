from dataclasses import dataclass
from typing import Optional


@dataclass
class LinkMetrics:
    """Queue and performance measurements for a network link."""

    source: str
    destination: str
    queue_bytes: Optional[int] = None
    bandwidth_bps: Optional[int] = None
    estimated_delay_s: Optional[float] = None
    utilization: Optional[float] = None


@dataclass
class FailureEvent:
    """Represents a detected network link failure."""

    source: str
    destination: str
    is_failed: bool
    detection_time: Optional[float] = None


@dataclass
class RouteDecision:
    """Represents a routing decision made by the controller."""

    path: list[str]
    cost: Optional[float] = None
    reason: Optional[str] = None
