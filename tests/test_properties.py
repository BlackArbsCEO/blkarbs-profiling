"""Property-based tests for blkarbs_profiling using Hypothesis.

These tests verify mathematical invariants and contract boundaries that
handwritten tests miss — float edge cases (subnormals, very large values,
precision loss near zero), and accumulation correctness for arbitrary N.
"""

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blkarbs_profiling import AccumulatingTimer, ProfilingSession

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid elapsed times: non-negative finite floats (no inf/nan)
valid_elapsed = st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False)

# Valid counts: non-negative integers
valid_count = st.integers(min_value=0, max_value=10**9)

# Positive elapsed (for throughput/per_item_ms calculations that divide)
positive_elapsed = st.floats(min_value=1e-9, max_value=1e12, allow_nan=False, allow_infinity=False)

# Positive counts (for per_item_ms calculation that divides by count)
positive_count = st.integers(min_value=1, max_value=10**9)

# Memory deltas: can be negative (memory released)
valid_memory_delta = st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False)

# Peak memory: non-negative
valid_peak_memory = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)

# Labels: non-empty strings
valid_label = st.text(min_size=1, max_size=50)

# Negative floats for contract violation testing
negative_float = st.floats(max_value=-1e-15, allow_nan=False, allow_infinity=False)

# Negative ints for contract violation testing
negative_int = st.integers(max_value=-1)

# Number of iterations for accumulating timer
iteration_count = st.integers(min_value=1, max_value=200)


# ---------------------------------------------------------------------------
# ProfilingSession: record() contract boundaries
# ---------------------------------------------------------------------------

class TestRecordContractProperties:
    @given(elapsed=valid_elapsed, count=valid_count)
    def test_any_non_negative_inputs_are_accepted(self, elapsed, count):
        """record() must accept ANY non-negative elapsed and count without raising."""
        session = ProfilingSession()
        session.record("prop_test", elapsed=elapsed, count=count)

        results = session.get_results()
        assert "prop_test" in results
        assert results["prop_test"]["total_time"] == elapsed
        assert results["prop_test"]["total_count"] == float(count)

    @given(elapsed=negative_float, count=valid_count)
    def test_negative_elapsed_always_raises(self, elapsed, count):
        """record() must reject ANY negative elapsed, no matter how small."""
        session = ProfilingSession()
        with pytest.raises(AssertionError, match="non-negative"):
            session.record("bad", elapsed=elapsed, count=count)

    @given(elapsed=valid_elapsed, count=negative_int)
    def test_negative_count_always_raises(self, elapsed, count):
        """record() must reject ANY negative count."""
        session = ProfilingSession()
        with pytest.raises(AssertionError, match="non-negative"):
            session.record("bad", elapsed=elapsed, count=count)


# ---------------------------------------------------------------------------
# ProfilingSession: get_results() math invariants
# ---------------------------------------------------------------------------

class TestResultsMathProperties:
    @given(elapsed=positive_elapsed, count=positive_count)
    def test_throughput_equals_count_over_time(self, elapsed, count):
        """throughput must equal count / elapsed for any positive inputs."""
        session = ProfilingSession()
        session.record("math", elapsed=elapsed, count=count)

        results = session.get_results()
        expected_throughput = count / elapsed
        assert results["math"]["throughput"] == pytest.approx(expected_throughput, rel=1e-9)

    @given(elapsed=positive_elapsed, count=positive_count)
    def test_per_item_ms_equals_time_over_count_times_1000(self, elapsed, count):
        """per_item_ms must equal (elapsed / count) * 1000 for any positive inputs."""
        session = ProfilingSession()
        session.record("math", elapsed=elapsed, count=count)

        results = session.get_results()
        expected_per_item = (elapsed / count) * 1000
        assert results["math"]["per_item_ms"] == pytest.approx(expected_per_item, rel=1e-9)

    @given(elapsed=valid_elapsed, count=valid_count)
    def test_throughput_is_non_negative(self, elapsed, count):
        """throughput must be >= 0 for any valid inputs."""
        session = ProfilingSession()
        session.record("tp", elapsed=elapsed, count=count)

        results = session.get_results()
        assert results["tp"]["throughput"] >= 0

    @given(elapsed=valid_elapsed, count=valid_count)
    def test_per_item_ms_is_non_negative(self, elapsed, count):
        """per_item_ms must be >= 0 for any valid inputs."""
        session = ProfilingSession()
        session.record("pi", elapsed=elapsed, count=count)

        results = session.get_results()
        assert results["pi"]["per_item_ms"] >= 0


# ---------------------------------------------------------------------------
# ProfilingSession: multi-record accumulation
# ---------------------------------------------------------------------------

class TestAccumulationProperties:
    @given(
        data=st.lists(
            st.tuples(valid_elapsed, positive_count),
            min_size=1,
            max_size=20,
        )
    )
    def test_total_time_is_sum_of_elapsed(self, data):
        """total_time must equal the sum of all recorded elapsed values."""
        session = ProfilingSession()
        for elapsed, count in data:
            session.record("acc", elapsed=elapsed, count=count)

        results = session.get_results()
        expected_total = sum(e for e, _ in data)
        assert results["acc"]["total_time"] == pytest.approx(expected_total, rel=1e-9)

    @given(
        data=st.lists(
            st.tuples(valid_elapsed, positive_count),
            min_size=1,
            max_size=20,
        )
    )
    def test_total_count_is_sum_of_counts(self, data):
        """total_count must equal the sum of all recorded counts."""
        session = ProfilingSession()
        for elapsed, count in data:
            session.record("acc", elapsed=elapsed, count=count)

        results = session.get_results()
        expected_count = float(sum(c for _, c in data))
        assert results["acc"]["total_count"] == expected_count

    @given(
        data=st.lists(
            st.tuples(valid_elapsed, valid_count, valid_peak_memory),
            min_size=2,
            max_size=10,
        )
    )
    def test_peak_memory_is_max_of_recorded_peaks(self, data):
        """peak_memory must be the max of all recorded peak values."""
        session = ProfilingSession()
        has_nonzero_peak = False
        for elapsed, count, peak in data:
            session.record("peak", elapsed=elapsed, count=count, peak_memory=peak)
            if peak != 0.0:
                has_nonzero_peak = True

        results = session.get_results()
        if has_nonzero_peak:
            nonzero_peaks = [p for _, _, p in data if p != 0.0]
            assert results["peak"]["peak_memory"] == pytest.approx(max(nonzero_peaks))


# ---------------------------------------------------------------------------
# ProfilingSession: flush_to_file JSON roundtrip
# ---------------------------------------------------------------------------

class TestFlushRoundtripProperties:
    @given(
        elapsed=positive_elapsed,
        count=positive_count,
        mem_delta=valid_memory_delta,
    )
    def test_flush_produces_valid_json_preserving_values(self, elapsed, count, mem_delta):
        """flush_to_file must produce parseable JSON with correct total_time."""
        session = ProfilingSession()
        session.record("rt", elapsed=elapsed, count=count, memory_delta=mem_delta)

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test.json"
            session.flush_to_file(out)

            data = json.loads(out.read_text())
            assert "rt" in data
            assert data["rt"]["total_time"] == pytest.approx(elapsed, rel=1e-9)
            assert data["rt"]["total_count"] == pytest.approx(float(count), rel=1e-9)


# ---------------------------------------------------------------------------
# ProfilingSession: print_summary / log_checkpoint never crash
# ---------------------------------------------------------------------------

class TestDisplayNeverCrashesProperties:
    @given(elapsed=valid_elapsed, count=valid_count, mem_delta=valid_memory_delta)
    @settings(max_examples=50)
    def test_print_summary_never_crashes(self, elapsed, count, mem_delta):
        """print_summary must not raise for any valid recorded data."""
        session = ProfilingSession()
        session.record("display", elapsed=elapsed, count=count, memory_delta=mem_delta)
        session.print_summary("Property Test")

    @given(elapsed=valid_elapsed, count=valid_count, mem_delta=valid_memory_delta)
    @settings(max_examples=50)
    def test_log_checkpoint_never_crashes(self, elapsed, count, mem_delta):
        """log_checkpoint must not raise for any valid recorded data."""
        session = ProfilingSession()
        session.record("display", elapsed=elapsed, count=count, memory_delta=mem_delta)
        session.log_checkpoint("Property Check")


# ---------------------------------------------------------------------------
# AccumulatingTimer: accumulation invariants
# ---------------------------------------------------------------------------

class TestAccumulatingTimerProperties:
    @given(n=iteration_count)
    @settings(max_examples=30)
    def test_count_equals_iterations(self, n):
        """After N context manager entries, count must equal N."""
        timer = AccumulatingTimer("prop")
        for _ in range(n):
            with timer:
                pass
        assert timer.count == n

    @given(n=iteration_count)
    @settings(max_examples=30)
    def test_total_is_non_negative(self, n):
        """total must be >= 0 regardless of iteration count (perf_counter is monotonic)."""
        timer = AccumulatingTimer("prop")
        for _ in range(n):
            with timer:
                pass
        assert timer.total >= 0.0

    @given(n=iteration_count)
    @settings(max_examples=30)
    def test_flush_resets_to_zero(self, n):
        """After flush, count and total must both be 0."""
        session = ProfilingSession()
        timer = AccumulatingTimer("prop")
        for _ in range(n):
            with timer:
                pass
        timer.flush(session)
        assert timer.count == 0
        assert timer.total == 0.0

    @given(count_override=valid_count)
    def test_flush_count_override_accepted(self, count_override):
        """flush must accept any non-negative count_override."""
        session = ProfilingSession()
        timer = AccumulatingTimer("prop")
        with timer:
            pass
        _, flushed_count = timer.flush(session, count_override=count_override)
        assert flushed_count == count_override

    @given(count_override=negative_int)
    def test_flush_negative_count_override_always_raises(self, count_override):
        """flush must reject any negative count_override."""
        session = ProfilingSession()
        timer = AccumulatingTimer("prop")
        with timer:
            pass
        with pytest.raises(AssertionError, match="non-negative"):
            timer.flush(session, count_override=count_override)
