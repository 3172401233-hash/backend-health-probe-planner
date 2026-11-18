"""Data models for the health probe planner."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, Tuple


class ProbeType(enum.Enum):
    HTTP = "http"
    TCP = "tcp"
    GRPC = "grpc"


class EndpointState(enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    EJECTED = "ejected"


#: Ordering used to compare severity; higher is worse.
SEVERITY = {
    EndpointState.HEALTHY: 0,
    EndpointState.DEGRADED: 1,
    EndpointState.EJECTED: 2,
}


def worse(first: EndpointState, second: EndpointState) -> EndpointState:
    """Return the more severe of two states."""
    return first if SEVERITY[first] >= SEVERITY[second] else second


@dataclass(frozen=True)
class EndpointSpec:
    """Static description of a backend endpoint."""

    endpoint_id: str
    probe_type: ProbeType = ProbeType.HTTP
    rack: str = "default"
    dependencies: Tuple[str, ...] = ()
    #: Status codes (HTTP or gRPC) that count as failures even when the
    #: transport-level probe succeeded.
    failure_status_codes: frozenset = frozenset()


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one active probe."""

    endpoint_id: str
    success: bool
    timed_out: bool = False
    status_code: Optional[int] = None
    #: Names of dependencies whose sub-check failed while the main interface
    #: still answered. A dependency-only failure degrades but never ejects.
    dependency_failures: Tuple[str, ...] = ()
    timestamp: Optional[float] = None


@dataclass(frozen=True)
class PassiveStats:
    """Aggregated real-request statistics for an endpoint."""

    endpoint_id: str
    total: int
    errors: int
    timestamp: float

    @property
    def error_rate(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.errors / self.total


@dataclass(frozen=True)
class ProbePlan:
    """A scheduled active probe for one endpoint."""

    endpoint_id: str
    probe_type: ProbeType
    scheduled_at: float
    interval: float
    reason: str
