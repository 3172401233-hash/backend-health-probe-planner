"""Health probe planner: state machine, correlation handling, scheduling."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import (
    SEVERITY,
    EndpointSpec,
    EndpointState,
    PassiveStats,
    ProbePlan,
    ProbeResult,
    worse,
)


@dataclass(frozen=True)
class PlannerConfig:
    """Tunables for the planner.

    Thresholds use hysteresis on purpose: ejection needs strictly more
    consecutive failures than degradation, and recovery needs a full window
    of consecutive successes, so a single flip does not move the state.
    """

    # Active-probe thresholds (consecutive results).
    degraded_failure_threshold: int = 2
    eject_failure_threshold: int = 3
    recovery_success_threshold: int = 2

    # Passive (real traffic) thresholds.
    passive_error_rate_degraded: float = 0.2
    passive_error_rate_eject: float = 0.5
    passive_min_samples: int = 20
    #: Passive data older than this (seconds) is not fresh evidence.
    max_passive_age: float = 30.0

    # Correlated-failure handling.
    #: Fraction of a correlation group (same rack or shared dependency)
    #: that must be failing before the failure is treated as infrastructural.
    correlation_threshold: float = 0.5
    #: Groups smaller than this never trigger correlation suppression, so a
    #: lone endpoint can still be ejected.
    min_correlation_group_size: int = 2
    #: When a correlated failure is detected, keep endpoints routable
    #: (degraded) instead of ejecting the whole group.
    fail_open_on_correlation: bool = True

    # Probe scheduling: exponential backoff with jitter.
    base_probe_interval: float = 1.0
    max_probe_interval: float = 60.0
    ejected_probe_interval: float = 30.0
    max_backoff_exponent: int = 5
    #: Multiplicative jitter in [1 - jitter, 1 + jitter].
    jitter: float = 0.2
    probe_budget: int = 100

    #: Minimum dwell time before a state may improve again (anti-flap).
    min_dwell_seconds: float = 5.0

    random_seed: int = 0


@dataclass(frozen=True)
class StateTransition:
    endpoint_id: str
    old: EndpointState
    new: EndpointState
    reason: str
    at: float


@dataclass
class _Runtime:
    spec: EndpointSpec
    state: EndpointState = EndpointState.HEALTHY
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_change: float = 0.0
    next_probe_at: float = 0.0


class HealthProbePlanner:
    """Plans probes and computes endpoint health states.

    Time is always supplied by the caller (``now``), which keeps the planner
    deterministic and testable. The planner tolerates clock regressions:
    internally time never moves backwards.
    """

    def __init__(self, config: Optional[PlannerConfig] = None):
        self.config = config or PlannerConfig()
        self._runtime: Dict[str, _Runtime] = {}
        self._passive: Dict[str, PassiveStats] = {}
        self._dependencies: Dict[str, bool] = {}
        self._last_now: float = 0.0
        self._rng = random.Random(self.config.random_seed)
        self.clock_regressions: int = 0
        self.transitions: List[StateTransition] = []

    # ------------------------------------------------------------------
    # Registration and evidence intake
    # ------------------------------------------------------------------

    def add_endpoint(self, spec: EndpointSpec) -> None:
        self._runtime[spec.endpoint_id] = _Runtime(spec=spec)
        for dep in spec.dependencies:
            self._dependencies.setdefault(dep, True)

    def set_dependency_health(self, name: str, healthy: bool) -> None:
        """Record the health of a shared dependency (propagation source)."""
        self._dependencies[name] = healthy

    def record_probe(self, result: ProbeResult, now: float) -> None:
        """Consume one active probe result."""
        now = self._sanitize_now(now)
        rt = self._runtime.get(result.endpoint_id)
        if rt is None:
            raise KeyError(f"unknown endpoint: {result.endpoint_id}")

        for dep in result.dependency_failures:
            self._dependencies[dep] = False

        if self._is_failure(rt.spec, result):
            rt.consecutive_failures += 1
            rt.consecutive_successes = 0
        else:
            rt.consecutive_successes += 1
            rt.consecutive_failures = 0

    def record_passive(self, stats: PassiveStats, now: float) -> None:
        """Consume passive request statistics. Stale data is discarded."""
        now = self._sanitize_now(now)
        if stats.endpoint_id not in self._runtime:
            raise KeyError(f"unknown endpoint: {stats.endpoint_id}")
        if not self._is_fresh(stats, now):
            return
        self._passive[stats.endpoint_id] = stats

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, now: float) -> Dict[str, EndpointState]:
        """Recompute all endpoint states and return them."""
        now = self._sanitize_now(now)

        candidates = {
            eid: self._candidate_state(rt, now) for eid, rt in self._runtime.items()
        }

        if self.config.fail_open_on_correlation:
            for eid in self._correlated_ejections(candidates):
                candidates[eid] = EndpointState.DEGRADED

        for eid, candidate in candidates.items():
            self._apply_transition(self._runtime[eid], candidate, now)

        return {eid: rt.state for eid, rt in self._runtime.items()}

    def state(self, endpoint_id: str) -> EndpointState:
        return self._runtime[endpoint_id].state

    def _candidate_state(self, rt: _Runtime, now: float) -> EndpointState:
        cfg = self.config
        candidate = EndpointState.HEALTHY

        # Active-probe evidence.
        if rt.consecutive_failures >= cfg.eject_failure_threshold:
            candidate = EndpointState.EJECTED
        elif rt.consecutive_failures >= cfg.degraded_failure_threshold:
            candidate = EndpointState.DEGRADED

        # Passive evidence, only when fresh and statistically meaningful.
        stats = self._passive.get(rt.spec.endpoint_id)
        if (
            stats is not None
            and self._is_fresh(stats, now)
            and stats.total >= cfg.passive_min_samples
        ):
            if stats.error_rate >= cfg.passive_error_rate_eject:
                candidate = worse(candidate, EndpointState.EJECTED)
            elif stats.error_rate >= cfg.passive_error_rate_degraded:
                candidate = worse(candidate, EndpointState.DEGRADED)

        # Dependency health propagates as degradation only, never ejection:
        # the main interface can still serve degraded responses.
        for dep in rt.spec.dependencies:
            if not self._dependencies.get(dep, True):
                candidate = worse(candidate, EndpointState.DEGRADED)

        return candidate

    def _correlated_ejections(
        self, candidates: Dict[str, EndpointState]
    ) -> List[str]:
        """Find endpoints whose ejection looks like a correlated failure."""
        cfg = self.config
        ejecting = {
            eid for eid, s in candidates.items() if s == EndpointState.EJECTED
        }
        if not ejecting:
            return []

        suppressed = set()
        for group in self._correlation_groups():
            members = [m for m in group if m in self._runtime]
            if len(members) < cfg.min_correlation_group_size:
                continue
            failing = [m for m in members if m in ejecting]
            if len(failing) / len(members) >= cfg.correlation_threshold:
                suppressed.update(failing)
        return sorted(suppressed)

    def _correlation_groups(self) -> List[Tuple[str, ...]]:
        """Groups of endpoints that share a rack or a dependency."""
        groups: Dict[Tuple[str, str], List[str]] = {}
        for eid, rt in self._runtime.items():
            groups.setdefault(("rack", rt.spec.rack), []).append(eid)
            for dep in rt.spec.dependencies:
                groups.setdefault(("dep", dep), []).append(eid)
        return [tuple(members) for members in groups.values()]

    def _apply_transition(
        self, rt: _Runtime, candidate: EndpointState, now: float
    ) -> None:
        cfg = self.config
        if candidate == rt.state:
            return

        improving = SEVERITY[candidate] < SEVERITY[rt.state]
        if improving:
            # Recovery requires a consecutive-success window and a minimum
            # dwell time; both together prevent flapping.
            if rt.consecutive_successes < cfg.recovery_success_threshold:
                return
            if now - rt.last_change < cfg.min_dwell_seconds:
                return
            reason = (
                f"recovered after {rt.consecutive_successes} "
                f"consecutive successes"
            )
        else:
            reason = (
                f"worsened after {rt.consecutive_failures} "
                f"consecutive failures"
            )

        self.transitions.append(
            StateTransition(
                endpoint_id=rt.spec.endpoint_id,
                old=rt.state,
                new=candidate,
                reason=reason,
                at=now,
            )
        )
        rt.state = candidate
        rt.last_change = now

    # ------------------------------------------------------------------
    # Probe scheduling
    # ------------------------------------------------------------------

    def plan_probes(
        self, now: float, budget: Optional[int] = None
    ) -> List[ProbePlan]:
        """Return the probes due now, capped by the probe budget.

        Planning a probe advances its next scheduled time, so calling this
        method twice in a row does not re-plan the same endpoints.
        """
        now = self._sanitize_now(now)
        if budget is None:
            budget = self.config.probe_budget
        if budget <= 0:
            return []

        due = [rt for rt in self._runtime.values() if rt.next_probe_at <= now]
        # Ejected endpoints probe first (recovery detection), then most
        # overdue, so a tight budget is spent where it matters most.
        due.sort(
            key=lambda rt: (
                0 if rt.state == EndpointState.EJECTED else 1,
                rt.next_probe_at,
                rt.spec.endpoint_id,
            )
        )

        plans: List[ProbePlan] = []
        for rt in due[:budget]:
            interval = self._jittered(self._base_interval(rt))
            rt.next_probe_at = max(rt.next_probe_at, now) + interval
            plans.append(
                ProbePlan(
                    endpoint_id=rt.spec.endpoint_id,
                    probe_type=rt.spec.probe_type,
                    scheduled_at=rt.next_probe_at,
                    interval=interval,
                    reason=self._schedule_reason(rt),
                )
            )
        return plans

    def _base_interval(self, rt: _Runtime) -> float:
        cfg = self.config
        if rt.state == EndpointState.EJECTED:
            return cfg.ejected_probe_interval
        exponent = min(rt.consecutive_failures, cfg.max_backoff_exponent)
        interval = cfg.base_probe_interval * (2 ** exponent)
        return min(interval, cfg.max_probe_interval)

    def _jittered(self, interval: float) -> float:
        j = self.config.jitter
        return interval * (1.0 + self._rng.uniform(-j, j))

    @staticmethod
    def _schedule_reason(rt: _Runtime) -> str:
        if rt.state == EndpointState.EJECTED:
            return "recovery-check"
        if rt.consecutive_failures > 0:
            return "backoff"
        return "routine"

    # ------------------------------------------------------------------
    # Time and freshness helpers
    # ------------------------------------------------------------------

    def _sanitize_now(self, now: float) -> float:
        """Clamp time so it never moves backwards (clock-regression safe)."""
        if now < self._last_now:
            self.clock_regressions += 1
            return self._last_now
        self._last_now = now
        return now

    def _is_fresh(self, stats: PassiveStats, now: float) -> bool:
        # max(0, ...) keeps evidence usable across small clock regressions
        # instead of making it look artificially old or from the future.
        age = max(0.0, now - stats.timestamp)
        return age <= self.config.max_passive_age

    @staticmethod
    def _is_failure(spec: EndpointSpec, result: ProbeResult) -> bool:
        if not result.success or result.timed_out:
            return True
        if (
            result.status_code is not None
            and result.status_code in spec.failure_status_codes
        ):
            return True
        return False
