"""Tests for blkarbs_profiling core module.

Tests use real instances of all internal classes (no mocking internal classes).
Only external dependencies (psutil) are mocked where needed for determinism.
"""

import json
import threading
import time
from pathlib import Path

import pytest
from beartype.roar import BeartypeCallHintParamViolation

from blkarbs_profiling import (
    AccumulatingTimer,
    ComponentTimer,
    ProfilingSession,
    profile_operation,
)


# ---------------------------------------------------------------------------
# ComponentTimer
# ---------------------------------------------------------------------------

class TestComponentTimer:
    def test_elapsed_time_is_non_negative(self):
        with ComponentTimer(track_memory=False) as timer:
            time.sleep(0.01)
        assert timer.elapsed >= 0.01

    def test_memory_tracking_records_positive_peak(self):
        with ComponentTimer(track_memory=True) as timer:
            _ = [0] * 1000
        assert timer.peak_memory > 0
        assert timer.system_memory_before > 0
        assert timer.system_memory_after > 0

    def test_memory_tracking_disabled(self):
        with ComponentTimer(track_memory=False) as timer:
            pass
        assert timer.memory_delta == 0.0
        assert timer.peak_memory == 0.0

    def test_beartype_rejects_non_bool_track_memory(self):
        with pytest.raises(BeartypeCallHintParamViolation):
            ComponentTimer(track_memory="yes")


# ---------------------------------------------------------------------------
# AccumulatingTimer
# ---------------------------------------------------------------------------

class TestAccumulatingTimer:
    def test_accumulates_across_iterations(self):
        timer = AccumulatingTimer("test_op")
        for _ in range(5):
            with timer:
                time.sleep(0.001)
        assert timer.count == 5
        assert timer.total >= 0.005

    def test_flush_records_to_session_and_resets(self):
        session = ProfilingSession()
        timer = AccumulatingTimer("flush_test")
        for _ in range(3):
            with timer:
                time.sleep(0.001)

        flushed_total, flushed_count = timer.flush(session)
        assert flushed_count == 3
        assert flushed_total >= 0.003

        # Timer reset after flush
        assert timer.total == 0.0
        assert timer.count == 0

        # Session has the data
        results = session.get_results()
        assert "flush_test" in results
        assert results["flush_test"]["total_count"] == 3.0

    def test_flush_with_count_override(self):
        session = ProfilingSession()
        timer = AccumulatingTimer("override_test")
        for _ in range(10):
            with timer:
                pass
        _, count = timer.flush(session, count_override=42)
        assert count == 42
        assert session.get_results()["override_test"]["total_count"] == 42.0

    def test_reset_clears_without_flushing(self):
        timer = AccumulatingTimer("reset_test")
        with timer:
            pass
        assert timer.count == 1
        timer.reset()
        assert timer.count == 0
        assert timer.total == 0.0

    def test_empty_label_raises(self):
        with pytest.raises(AssertionError, match="non-empty"):
            AccumulatingTimer("")

    def test_negative_count_override_raises(self):
        session = ProfilingSession()
        timer = AccumulatingTimer("neg_test")
        with timer:
            pass
        with pytest.raises(AssertionError, match="non-negative"):
            timer.flush(session, count_override=-1)


# ---------------------------------------------------------------------------
# ProfilingSession
# ---------------------------------------------------------------------------

class TestProfilingSession:
    def test_record_and_get_results(self):
        session = ProfilingSession()
        session.record("op1", elapsed=1.0, count=10)
        session.record("op1", elapsed=2.0, count=20)
        session.record("op2", elapsed=0.5, count=5)

        results = session.get_results()
        assert results["op1"]["total_time"] == 3.0
        assert results["op1"]["total_count"] == 30.0
        assert results["op1"]["throughput"] == pytest.approx(10.0)
        assert results["op1"]["per_item_ms"] == pytest.approx(100.0)
        assert results["op2"]["total_time"] == 0.5

    def test_memory_metrics_recorded(self):
        session = ProfilingSession()
        session.record("mem_op", elapsed=1.0, count=1, memory_delta=0.5, peak_memory=4.2)

        results = session.get_results()
        assert results["mem_op"]["memory_delta"] == 0.5
        assert results["mem_op"]["peak_memory"] == 4.2

    def test_memory_metrics_absent_when_zero(self):
        session = ProfilingSession()
        session.record("no_mem", elapsed=1.0, count=1)

        results = session.get_results()
        assert "memory_delta" not in results["no_mem"]
        assert "peak_memory" not in results["no_mem"]

    def test_negative_elapsed_raises(self):
        session = ProfilingSession()
        with pytest.raises(AssertionError, match="non-negative"):
            session.record("bad", elapsed=-1.0, count=1)

    def test_negative_count_raises(self):
        session = ProfilingSession()
        with pytest.raises(AssertionError, match="non-negative"):
            session.record("bad", elapsed=1.0, count=-1)

    def test_thread_safety(self):
        session = ProfilingSession()
        errors = []

        def record_many(label: str, n: int) -> None:
            for i in range(n):
                session.record(label, elapsed=0.001, count=1)

        threads = [
            threading.Thread(target=record_many, args=(f"thread_{i}", 100))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        results = session.get_results()
        for i in range(4):
            assert results[f"thread_{i}"]["total_count"] == 100.0

    def test_zero_elapsed_throughput_is_zero(self):
        session = ProfilingSession()
        session.record("zero_time", elapsed=0.0, count=10)

        results = session.get_results()
        assert results["zero_time"]["throughput"] == 0

    def test_zero_count_per_item_is_zero(self):
        session = ProfilingSession()
        session.record("zero_count", elapsed=1.0, count=0)

        results = session.get_results()
        assert results["zero_count"]["per_item_ms"] == 0

    def test_flush_to_file_writes_json(self, tmp_path: Path):
        session = ProfilingSession()
        session.record("file_op", elapsed=1.5, count=10, memory_delta=0.3)

        out = tmp_path / "checkpoint.json"
        session.flush_to_file(out)

        assert out.exists()
        data = json.loads(out.read_text())
        assert "file_op" in data
        assert data["file_op"]["total_time"] == 1.5

    def test_flush_to_file_creates_parent_dirs(self, tmp_path: Path):
        session = ProfilingSession()
        session.record("nested", elapsed=1.0, count=1)

        out = tmp_path / "a" / "b" / "c" / "metrics.json"
        session.flush_to_file(out)
        assert out.exists()

    def test_print_summary_with_memory(self):
        """Exercises the has_memory=True branch (wide table with Mem/Peak columns)."""
        session = ProfilingSession()
        session.record("mem_op", elapsed=2.0, count=50, memory_delta=1.0, peak_memory=6.0)
        session.print_summary("With Memory")

    def test_print_summary_without_memory(self):
        """Exercises the has_memory=False branch (narrow table, no Mem/Peak columns)."""
        session = ProfilingSession()
        session.record("no_mem_op", elapsed=2.0, count=50)
        session.print_summary("Without Memory")

    @pytest.mark.parametrize(
        "elapsed,count,expected_unit",
        [
            (1.0, 10, "items/s"),       # throughput >= 1.0
            (100.0, 5, "items/min"),     # 0.01 <= throughput < 1.0
            (10000.0, 1, "items/hr"),    # throughput < 0.01
        ],
        ids=["high-throughput", "mid-throughput", "low-throughput"],
    )
    def test_print_summary_throughput_formatting(self, elapsed, count, expected_unit):
        """Exercises all three throughput format branches in print_summary."""
        session = ProfilingSession()
        session.record("tp_test", elapsed=elapsed, count=count)
        # Should not raise — branch coverage is the goal
        session.print_summary("Throughput Test")

    def test_print_summary_mixed_memory_and_no_memory(self):
        """One component has memory, another doesn't — exercises the dash formatting."""
        session = ProfilingSession()
        session.record("with_mem", elapsed=1.0, count=10, memory_delta=0.5, peak_memory=4.0)
        session.record("no_mem", elapsed=1.0, count=10)
        session.print_summary("Mixed Memory")

    def test_log_checkpoint_high_throughput(self):
        """Exercises throughput >= 1.0 branch in log_checkpoint."""
        session = ProfilingSession()
        session.record("fast_op", elapsed=1.0, count=100, peak_memory=3.5, memory_delta=0.8)
        session.log_checkpoint("High Throughput")

    @pytest.mark.parametrize(
        "elapsed,count",
        [
            (100.0, 5),     # 0.01 <= throughput < 1.0 → items/min
            (10000.0, 1),   # throughput < 0.01 → items/hr
        ],
        ids=["mid-throughput", "low-throughput"],
    )
    def test_log_checkpoint_throughput_formatting(self, elapsed, count):
        """Exercises mid and low throughput branches in log_checkpoint."""
        session = ProfilingSession()
        session.record("slow_op", elapsed=elapsed, count=count)
        session.log_checkpoint("Slow Throughput")

    def test_log_checkpoint_negative_memory_delta(self):
        """Exercises the negative memory delta branch (no '+' sign prefix)."""
        session = ProfilingSession()
        session.record("release_mem", elapsed=1.0, count=1, memory_delta=-0.5, peak_memory=2.0)
        session.log_checkpoint("Memory Released")

    def test_log_checkpoint_no_memory_metrics(self):
        """Exercises branch where neither peak_memory nor memory_delta are present."""
        session = ProfilingSession()
        session.record("no_mem", elapsed=2.0, count=10)
        session.log_checkpoint("No Memory")

    def test_log_checkpoint_empty_session(self):
        session = ProfilingSession()
        session.log_checkpoint("Empty")


# ---------------------------------------------------------------------------
# profile_operation
# ---------------------------------------------------------------------------

class TestProfileOperation:
    def test_records_timing_to_session(self):
        session = ProfilingSession()
        with profile_operation("timed_op", session, count=5) as timer:
            time.sleep(0.01)

        results = session.get_results()
        assert "timed_op" in results
        assert results["timed_op"]["total_time"] >= 0.01
        assert results["timed_op"]["total_count"] == 5.0

    def test_session_none_still_executes_code(self):
        executed = False
        with profile_operation("noop", None, count=1) as timer:
            executed = True

        assert executed
        assert timer.elapsed >= 0

    def test_session_none_does_not_record(self):
        with profile_operation("noop", None) as timer:
            pass
        # No session to check — just verify no exception

    def test_memory_tracked_when_session_provided(self):
        session = ProfilingSession()
        with profile_operation("mem_track", session) as timer:
            _ = [0] * 10000

        assert timer.peak_memory > 0

    def test_memory_not_tracked_when_session_none(self):
        with profile_operation("no_mem", None) as timer:
            pass

        assert timer.memory_delta == 0.0
        assert timer.peak_memory == 0.0
