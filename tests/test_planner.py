"""Unit tests for the health probe planner."""

import unittest

from health_probe_planner import (
    EndpointSpec,
    EndpointState,
    HealthProbePlanner,
    PassiveStats,
    PlannerConfig,
    ProbeResult,
    ProbeType,
)


def make_planner(**overrides):
    return HealthProbePlanner(PlannerConfig(**overrides))


def add_endpoint(planner, endpoint_id, rack="r1", deps=(), **kwargs):
    planner.add_endpoint(
        EndpointSpec(endpoint_id=endpoint_id, rack=rack, dependencies=deps, **kwargs)
    )


def fail(planner, endpoint_id, now, times=1, **kwargs):
    for _ in range(times):
        planner.record_probe(
            ProbeResult(endpoint_id=endpoint_id, success=False, **kwargs), now
        )


def succeed(planner, endpoint_id, now, times=1, **kwargs):
    for _ in range(times):
        planner.record_probe(
            ProbeResult(endpoint_id=endpoint_id, success=True, **kwargs), now
        )


class ActiveProbeStateTest(unittest.TestCase):
    def test_sporadic_timeout_does_not_eject(self):
        planner = make_planner()
        add_endpoint(planner, "a")

        fail(planner, "a", now=0, timed_out=True)
        self.assertEqual(planner.evaluate(0)["a"], EndpointState.HEALTHY)

        fail(planner, "a", now=1, timed_out=True)
        self.assertEqual(planner.evaluate(1)["a"], EndpointState.DEGRADED)

        # One success is not enough to recover (window = 2).
        succeed(planner, "a", now=2)
        self.assertEqual(planner.evaluate(2)["a"], EndpointState.DEGRADED)

        # Second success plus dwell time recovers to healthy.
        succeed(planner, "a", now=6)
        self.assertEqual(planner.evaluate(6)["a"], EndpointState.HEALTHY)

    def test_consecutive_failures_eject(self):
        planner = make_planner()
        add_endpoint(planner, "a")

        fail(planner, "a", now=0, times=3)
        self.assertEqual(planner.evaluate(0)["a"], EndpointState.EJECTED)

    def test_failure_status_codes_count_as_failures(self):
        planner = make_planner()
        add_endpoint(
            planner,
            "http-ep",
            probe_type=ProbeType.HTTP,
            failure_status_codes=frozenset({500, 503}),
        )
        add_endpoint(
            planner,
            "grpc-ep",
            rack="r2",
            probe_type=ProbeType.GRPC,
            failure_status_codes=frozenset({14}),  # UNAVAILABLE
        )

        # Transport OK but status code is a configured failure code.
        for now in range(3):
            planner.record_probe(
                ProbeResult(endpoint_id="http-ep", success=True, status_code=500),
                now,
            )
            planner.record_probe(
                ProbeResult(endpoint_id="grpc-ep", success=True, status_code=14),
                now,
            )
        states = planner.evaluate(3)
        self.assertEqual(states["http-ep"], EndpointState.EJECTED)
        self.assertEqual(states["grpc-ep"], EndpointState.EJECTED)

        # A non-configured code is not a failure.
        planner2 = make_planner()
        add_endpoint(
            planner2, "b", failure_status_codes=frozenset({500})
        )
        planner2.record_probe(
            ProbeResult(endpoint_id="b", success=True, status_code=404), 0
        )
        self.assertEqual(planner2.evaluate(0)["b"], EndpointState.HEALTHY)

    def test_state_does_not_flap_under_alternating_results(self):
        planner = make_planner()
        add_endpoint(planner, "a")

        fail(planner, "a", now=0, times=2)
        self.assertEqual(planner.evaluate(0)["a"], EndpointState.DEGRADED)

        # Alternating success/failure never reaches either consecutive
        # threshold, so the state must stay put.
        for now in range(1, 20):
            if now % 2:
                succeed(planner, "a", now)
            else:
                fail(planner, "a", now)
            self.assertEqual(planner.evaluate(now)["a"], EndpointState.DEGRADED)

        worsening = [
            t for t in planner.transitions if t.new == EndpointState.EJECTED
        ]
        self.assertEqual(worsening, [])

    def test_recovery_then_failure_again(self):
        planner = make_planner()
        add_endpoint(planner, "a")

        fail(planner, "a", now=0, times=3)
        self.assertEqual(planner.evaluate(0)["a"], EndpointState.EJECTED)

        succeed(planner, "a", now=10, times=2)
        self.assertEqual(planner.evaluate(10)["a"], EndpointState.HEALTHY)

        # Counters were reset by the successes, so a full new streak of
        # failures is required to eject again.
        fail(planner, "a", now=20, times=2)
        self.assertEqual(planner.evaluate(20)["a"], EndpointState.DEGRADED)
        fail(planner, "a", now=21)
        self.assertEqual(planner.evaluate(21)["a"], EndpointState.EJECTED)


class DependencyTest(unittest.TestCase):
    def test_unhealthy_dependency_degrades_but_never_ejects(self):
        planner = make_planner()
        add_endpoint(planner, "a", deps=("db",))

        planner.set_dependency_health("db", False)
        self.assertEqual(planner.evaluate(0)["a"], EndpointState.DEGRADED)

        # Main interface keeps answering; still only degraded.
        succeed(planner, "a", now=1, times=5)
        self.assertEqual(planner.evaluate(1)["a"], EndpointState.DEGRADED)

        # Dependency recovers; success window + dwell brings endpoint back.
        planner.set_dependency_health("db", True)
        succeed(planner, "a", now=10, times=2)
        self.assertEqual(planner.evaluate(10)["a"], EndpointState.HEALTHY)

    def test_probe_result_propagates_dependency_failure(self):
        planner = make_planner()
        add_endpoint(planner, "a", deps=("cache",))
        add_endpoint(planner, "b", rack="r2", deps=("cache",))

        # Main check OK, dependency sub-check failed.
        planner.record_probe(
            ProbeResult(
                endpoint_id="a", success=True, dependency_failures=("cache",)
            ),
            0,
        )
        states = planner.evaluate(0)
        self.assertEqual(states["a"], EndpointState.DEGRADED)
        # The failure propagates to every endpoint sharing the dependency.
        self.assertEqual(states["b"], EndpointState.DEGRADED)


class PassiveStatsTest(unittest.TestCase):
    def test_fresh_passive_errors_escalate_state(self):
        planner = make_planner()
        add_endpoint(planner, "a")

        planner.record_passive(
            PassiveStats(endpoint_id="a", total=100, errors=30, timestamp=0), 0
        )
        self.assertEqual(planner.evaluate(1)["a"], EndpointState.DEGRADED)

        planner.record_passive(
            PassiveStats(endpoint_id="a", total=100, errors=90, timestamp=2), 2
        )
        self.assertEqual(planner.evaluate(2)["a"], EndpointState.EJECTED)

    def test_stale_passive_data_is_not_fresh_evidence(self):
        planner = make_planner(max_passive_age=30.0)
        add_endpoint(planner, "a")

        # Arrives already stale: discarded at intake.
        planner.record_passive(
            PassiveStats(endpoint_id="a", total=100, errors=100, timestamp=0), 100
        )
        self.assertEqual(planner.evaluate(100)["a"], EndpointState.HEALTHY)

        # Arrives fresh but ages out before evaluation.
        planner.record_passive(
            PassiveStats(endpoint_id="a", total=100, errors=100, timestamp=100),
            100,
        )
        self.assertEqual(planner.evaluate(200)["a"], EndpointState.HEALTHY)

    def test_passive_below_min_samples_is_ignored(self):
        planner = make_planner(passive_min_samples=20)
        add_endpoint(planner, "a")
        planner.record_passive(
            PassiveStats(endpoint_id="a", total=3, errors=3, timestamp=0), 0
        )
        self.assertEqual(planner.evaluate(0)["a"], EndpointState.HEALTHY)


class CorrelatedFailureTest(unittest.TestCase):
    def test_rack_wide_failure_fails_open(self):
        planner = make_planner()
        for eid in ("a", "b", "c"):
            add_endpoint(planner, eid, rack="rack-1")
        add_endpoint(planner, "solo", rack="rack-2")

        for eid in ("a", "b", "c", "solo"):
            fail(planner, eid, now=0, times=3)

        states = planner.evaluate(0)
        # Correlated rack failure: kept routable as degraded, not ejected.
        for eid in ("a", "b", "c"):
            self.assertEqual(states[eid], EndpointState.DEGRADED, eid)
        # The lone endpoint is a genuine single-instance failure.
        self.assertEqual(states["solo"], EndpointState.EJECTED)

    def test_partial_group_failure_still_ejects(self):
        planner = make_planner()
        for eid in ("a", "b", "c"):
            add_endpoint(planner, eid, rack="rack-1")

        fail(planner, "a", now=0, times=3)
        states = planner.evaluate(0)
        self.assertEqual(states["a"], EndpointState.EJECTED)
        self.assertEqual(states["b"], EndpointState.HEALTHY)
        self.assertEqual(states["c"], EndpointState.HEALTHY)

    def test_shared_dependency_failure_is_correlated(self):
        planner = make_planner()
        # Different racks, same dependency: still correlated.
        add_endpoint(planner, "a", rack="r1", deps=("db",))
        add_endpoint(planner, "b", rack="r2", deps=("db",))

        fail(planner, "a", now=0, times=3)
        fail(planner, "b", now=0, times=3)

        states = planner.evaluate(0)
        self.assertEqual(states["a"], EndpointState.DEGRADED)
        self.assertEqual(states["b"], EndpointState.DEGRADED)

    def test_fail_closed_mode_ejects_despite_correlation(self):
        planner = make_planner(fail_open_on_correlation=False)
        for eid in ("a", "b"):
            add_endpoint(planner, eid, rack="rack-1")

        fail(planner, "a", now=0, times=3)
        fail(planner, "b", now=0, times=3)

        states = planner.evaluate(0)
        self.assertEqual(states["a"], EndpointState.EJECTED)
        self.assertEqual(states["b"], EndpointState.EJECTED)


class ProbeSchedulingTest(unittest.TestCase):
    def test_probe_budget_limits_plan(self):
        planner = make_planner()
        for i in range(5):
            add_endpoint(planner, f"ep-{i}", rack=f"rack-{i}")

        plans = planner.plan_probes(0, budget=3)
        self.assertEqual(len(plans), 3)

        # Already-planned endpoints are not re-planned immediately.
        plans = planner.plan_probes(0, budget=3)
        self.assertEqual(len(plans), 2)

    def test_zero_budget_plans_nothing(self):
        planner = make_planner()
        add_endpoint(planner, "a")
        self.assertEqual(planner.plan_probes(0, budget=0), [])

    def test_backoff_and_jitter_bounds(self):
        planner = make_planner(
            base_probe_interval=1.0, jitter=0.2, random_seed=42
        )
        add_endpoint(planner, "a")
        fail(planner, "a", now=0, times=3)
        planner.evaluate(0)

        plans = planner.plan_probes(0, budget=1)
        self.assertEqual(len(plans), 1)
        # Ejected endpoints use the (longer) ejected probe interval.
        self.assertTrue(30.0 * 0.8 <= plans[0].interval <= 30.0 * 1.2)
        self.assertEqual(plans[0].reason, "recovery-check")

        # A degraded endpoint backs off exponentially: base * 2**failures.
        planner2 = make_planner(
            base_probe_interval=1.0, jitter=0.2, random_seed=42
        )
        add_endpoint(planner2, "b")
        fail(planner2, "b", now=0, times=2)
        planner2.evaluate(0)
        plans = planner2.plan_probes(0, budget=1)
        self.assertTrue(4.0 * 0.8 <= plans[0].interval <= 4.0 * 1.2)
        self.assertEqual(plans[0].reason, "backoff")

    def test_ejected_endpoints_are_scheduled_first(self):
        planner = make_planner()
        add_endpoint(planner, "ejected-ep", rack="r1")
        add_endpoint(planner, "healthy-ep", rack="r2")
        fail(planner, "ejected-ep", now=0, times=3)
        planner.evaluate(0)

        plans = planner.plan_probes(0, budget=1)
        self.assertEqual([p.endpoint_id for p in plans], ["ejected-ep"])


class ClockRegressionTest(unittest.TestCase):
    def test_clock_regression_does_not_corrupt_state(self):
        planner = make_planner()
        add_endpoint(planner, "a")

        planner.record_passive(
            PassiveStats(endpoint_id="a", total=100, errors=30, timestamp=100),
            100,
        )
        # Clock jumps backwards: passive data must not suddenly look stale.
        states = planner.evaluate(50)
        self.assertEqual(states["a"], EndpointState.DEGRADED)
        self.assertEqual(planner.clock_regressions, 1)

    def test_clock_regression_keeps_schedule_monotonic(self):
        planner = make_planner()
        add_endpoint(planner, "a")

        plans = planner.plan_probes(100, budget=1)
        scheduled_at = plans[0].scheduled_at

        # Time moves backwards: nothing may be scheduled in the past.
        plans = planner.plan_probes(10, budget=1)
        for plan in plans:
            self.assertGreaterEqual(plan.scheduled_at, scheduled_at)

    def test_unknown_endpoint_raises(self):
        planner = make_planner()
        with self.assertRaises(KeyError):
            planner.record_probe(ProbeResult(endpoint_id="nope", success=True), 0)
        with self.assertRaises(KeyError):
            planner.record_passive(
                PassiveStats(endpoint_id="nope", total=1, errors=1, timestamp=0), 0
            )


if __name__ == "__main__":
    unittest.main()
