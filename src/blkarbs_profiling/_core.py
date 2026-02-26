"""Core profiling utilities.

Design by Contract (P1 - MANDATORY):
- Elapsed time MUST be non-negative (crash if negative)
- Counts MUST be non-negative (crash if negative)
- No hidden defaults (all parameters explicit)
- Fail-fast on violations

All classes use beartype for runtime type enforcement.
Memory tracking via psutil is mandatory.
"""

import json
import threading
import time
from collections import defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
from beartype import beartype
from loguru import logger


class ComponentTimer:
    """Context manager for timing code blocks with memory tracking.

    Args:
        track_memory: If True, track memory usage via psutil (default: True)

    Attributes:
        elapsed: Time elapsed in seconds (MUST be >= 0)
        memory_delta: Change in process memory (GB)
        peak_memory: Peak process memory during operation (GB)
        system_memory_before: System memory usage % at start
        system_memory_after: System memory usage % at end

    Example:
        with ComponentTimer(track_memory=True) as timer:
            result = expensive_operation()
        print(f"Elapsed: {timer.elapsed:.2f}s, Memory: {timer.memory_delta:.2f}GB")

    Design by Contract:
        - elapsed >= 0 (crashes if negative - clock went backwards)
        - memory_delta can be negative (memory released)
        - peak_memory >= 0 always
    """

    @beartype
    def __init__(self, track_memory: bool = True) -> None:
        self.track_memory = track_memory
        self.elapsed: float = 0.0
        self.memory_delta: float = 0.0
        self.peak_memory: float = 0.0
        self.system_memory_before: float = 0.0
        self.system_memory_after: float = 0.0
        self._start: float = 0.0
        self._start_memory: float = 0.0

    def __enter__(self) -> "ComponentTimer":
        self._start = time.time()

        if self.track_memory:
            process = psutil.Process()
            self._start_memory = process.memory_info().rss / 1024**3  # GB
            self.system_memory_before = psutil.virtual_memory().percent

        return self

    def __exit__(self, *args: Any) -> None:
        end_time = time.time()
        self.elapsed = end_time - self._start

        assert self.elapsed >= 0, (
            f"Elapsed time cannot be negative: {self.elapsed:.6f}s. "
            f"System clock went backwards or timing bug."
        )

        if self.track_memory:
            process = psutil.Process()
            end_memory = process.memory_info().rss / 1024**3  # GB
            self.system_memory_after = psutil.virtual_memory().percent

            self.memory_delta = end_memory - self._start_memory
            self.peak_memory = end_memory

            assert self.peak_memory >= 0, (
                f"Peak memory cannot be negative: {self.peak_memory:.2f}GB"
            )


class AccumulatingTimer:
    """Lightweight accumulating timer for hot-path profiling.

    Uses only time.perf_counter() (~150ns overhead) instead of psutil (~200-600us).
    Create once before a loop, use as context manager per iteration, then flush
    to a ProfilingSession after the loop.

    Usage:
        timer = AccumulatingTimer("Signal Generation")
        for bar in bars:
            with timer:
                signal = strategy.on_price(bar)
        timer.flush(session, count_override=signal_count)

    Design by Contract:
        - label must be non-empty string
        - total >= 0 always (perf_counter is monotonic)
        - count >= 0 always
        - flush() resets state (safe for reuse across windows)
    """

    @beartype
    def __init__(self, label: str) -> None:
        assert label, "Timer label must be non-empty"
        self.label: str = label
        self._total: float = 0.0
        self._count: int = 0
        self._start: float = 0.0

    def __enter__(self) -> "AccumulatingTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self._total += time.perf_counter() - self._start
        self._count += 1

    @property
    def total(self) -> float:
        return self._total

    @property
    def count(self) -> int:
        return self._count

    @beartype
    def flush(
        self,
        session: "ProfilingSession",
        count_override: int | None = None,
    ) -> tuple[float, int]:
        """Flush accumulated timing to a ProfilingSession and reset.

        Args:
            session: Target ProfilingSession to record into
            count_override: If provided, use this count instead of invocation count.
                Needed when the semantic count differs from timer invocations
                (e.g., signal_count vs bars iterated).

        Returns:
            Tuple of (flushed_total, flushed_count).
        """
        recorded_count = count_override if count_override is not None else self._count
        assert recorded_count >= 0, f"Count must be non-negative: {recorded_count}"
        flushed_total = self._total
        flushed_count = recorded_count
        session.record(self.label, self._total, count=recorded_count)
        self._total = 0.0
        self._count = 0
        return flushed_total, flushed_count

    @beartype
    def reset(self) -> None:
        """Reset accumulated timing without flushing."""
        self._total = 0.0
        self._count = 0


class ProfilingSession:
    """Accumulates timing data across multiple components with statistics.

    Thread-safe for concurrent record() calls from multiple threads.

    Tracks:
    - Total elapsed time per component
    - Item counts for throughput calculation
    - Per-item timing
    - Memory usage via psutil

    Design by Contract:
    - All elapsed times MUST be >= 0
    - All counts MUST be >= 0

    Example:
        session = ProfilingSession()
        session.record("Data Loading", elapsed=2.5, count=10)
        session.record("Computation", elapsed=15.0, count=500, memory_delta=2.1)
        session.print_summary("Pipeline Results")
    """

    def __init__(self) -> None:
        self.timings: dict[str, list[float]] = defaultdict(list)
        self.counts: dict[str, list[int]] = defaultdict(list)
        self.memory_deltas: dict[str, list[float]] = defaultdict(list)
        self.peak_memory: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    @beartype
    def record(
        self,
        label: str,
        elapsed: float,
        count: int = 1,
        memory_delta: float = 0.0,
        peak_memory: float = 0.0,
    ) -> None:
        """Record timing for a component (thread-safe).

        Args:
            label: Component name (e.g., "Data Loading", "Backtest Execution")
            elapsed: Elapsed time in seconds (MUST be >= 0)
            count: Number of items processed (MUST be >= 0)
            memory_delta: Change in memory usage in GB (can be negative)
            peak_memory: Peak memory usage during operation in GB (MUST be >= 0)
        """
        assert elapsed >= 0, f"Elapsed time must be non-negative: {elapsed}"
        assert count >= 0, f"Count must be non-negative: {count}"

        with self._lock:
            self.timings[label].append(elapsed)
            self.counts[label].append(count)

            if memory_delta != 0.0:
                self.memory_deltas[label].append(memory_delta)
            if peak_memory != 0.0:
                self.peak_memory[label].append(peak_memory)

    @beartype
    def get_results(self) -> dict[str, dict[str, float]]:
        """Get aggregated timing results.

        Returns:
            Dictionary mapping component labels to metrics dict with keys:
            total_time, total_count, throughput, per_item_ms,
            and optionally memory_delta, peak_memory.
        """
        results: dict[str, dict[str, float]] = {}
        for label in self.timings:
            total_time = sum(self.timings[label])
            total_count = sum(self.counts[label])

            result: dict[str, float] = {
                "total_time": total_time,
                "total_count": float(total_count),
                "throughput": total_count / total_time if total_time > 0 else 0,
                "per_item_ms": (total_time / total_count * 1000) if total_count > 0 else 0,
            }

            if label in self.memory_deltas and self.memory_deltas[label]:
                result["memory_delta"] = sum(self.memory_deltas[label])
            if label in self.peak_memory and self.peak_memory[label]:
                result["peak_memory"] = max(self.peak_memory[label])

            results[label] = result

        return results

    @beartype
    def log_checkpoint(self, checkpoint_name: str) -> None:
        """Log condensed profiling snapshot via loguru.

        Useful for long-running pipelines where you want incremental visibility
        without waiting for the full summary table.

        Args:
            checkpoint_name: Name for this checkpoint (e.g., "After Feature Building")
        """
        results = self.get_results()
        if not results:
            logger.info(f"[CHECKPOINT: {checkpoint_name}] No profiling data yet")
            return

        total_time = sum(m["total_time"] for m in results.values())
        logger.info(f"[CHECKPOINT: {checkpoint_name}] Total elapsed: {total_time:.2f}s")

        for label, metrics in results.items():
            throughput = metrics["throughput"]
            if throughput >= 1.0:
                throughput_str = f"{throughput:.1f}/s"
            elif throughput >= 0.01:
                throughput_str = f"{throughput * 60:.1f}/min"
            else:
                throughput_str = f"{throughput * 3600:.1f}/hr"

            line = (
                f"  {label}: {metrics['total_time']:.2f}s, "
                f"{metrics['total_count']:.0f} items ({throughput_str})"
            )

            if "peak_memory" in metrics:
                line += f", peak={metrics['peak_memory']:.2f}GB"
            if "memory_delta" in metrics:
                delta = metrics["memory_delta"]
                sign = "+" if delta >= 0 else ""
                line += f", Δ={sign}{delta:.2f}GB"

            logger.info(line)

    @beartype
    def print_summary(self, title: str = "PROFILING RESULTS") -> None:
        """Print formatted summary table of all recorded timings.

        Args:
            title: Header title for the summary table
        """
        results = self.get_results()

        has_memory = any(
            "memory_delta" in m or "peak_memory" in m for m in results.values()
        )

        logger.info("")
        if has_memory:
            logger.info("=" * 120)
            logger.info(f"{title:^120}")
            logger.info("=" * 120)
            logger.info(
                f"{'Component':<40} {'Time':>10} {'Count':>10} "
                f"{'Throughput':>15} {'Per-Item':>10} {'Mem Δ':>10} {'Peak':>10}"
            )
            logger.info("-" * 120)
        else:
            logger.info("=" * 90)
            logger.info(f"{title:^90}")
            logger.info("=" * 90)
            logger.info(
                f"{'Component':<40} {'Time':>10} {'Count':>10} "
                f"{'Throughput':>15} {'Per-Item':>10}"
            )
            logger.info("-" * 90)

        total_time = 0.0
        for label, metrics in results.items():
            total_time += metrics["total_time"]

            throughput = metrics["throughput"]
            if throughput >= 1.0:
                throughput_str = f"{throughput:.1f} items/s"
            elif throughput >= 0.01:
                throughput_str = f"{throughput * 60:.1f} items/min"
            else:
                throughput_str = f"{throughput * 3600:.1f} items/hr"

            line = (
                f"{label:<40} "
                f"{metrics['total_time']:>9.2f}s "
                f"{metrics['total_count']:>10.0f} "
                f"{throughput_str:>15} "
                f"{metrics['per_item_ms']:>9.1f}ms"
            )

            if has_memory:
                mem_delta = metrics.get("memory_delta", 0.0)
                peak_mem = metrics.get("peak_memory", 0.0)
                mem_delta_str = f"{mem_delta:>9.2f}G" if mem_delta != 0.0 else f"{'-':>10}"
                peak_mem_str = f"{peak_mem:>9.2f}G" if peak_mem != 0.0 else f"{'-':>10}"
                line += f" {mem_delta_str:>10} {peak_mem_str:>10}"

            logger.info(line)

        width = 120 if has_memory else 90
        logger.info("=" * width)
        logger.info(f"{'TOTAL':^40} {total_time:>9.2f}s")
        logger.info("=" * width)
        logger.info("")

    @beartype
    def flush_to_file(self, path: Path) -> None:
        """Write current metrics to JSON checkpoint file.

        Thread-safe. Enables incremental checkpoints for crash resilience.

        Args:
            path: Output file path (will be created/overwritten)
        """
        assert path is not None, "Checkpoint path cannot be None"

        with self._lock:
            results = self.get_results()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(results, f, indent=2, default=str)


@beartype
@contextmanager
def profile_operation(
    label: str,
    session: ProfilingSession | None,
    count: int = 1,
) -> Generator[ComponentTimer, None, None]:
    """Context manager for profiling with automatic session recording.

    When session is None, the wrapped code still executes but timing is not recorded.
    This eliminates the need for ``if session:`` / ``else:`` branching at call sites.

    Args:
        label: Component name for profiling session
        session: ProfilingSession to record into, or None for no-op
        count: Number of items processed (default: 1)

    Yields:
        ComponentTimer instance for accessing elapsed time and memory
    """
    with ComponentTimer(track_memory=session is not None) as timer:
        yield timer
    if session is not None:
        session.record(
            label,
            timer.elapsed,
            count,
            memory_delta=timer.memory_delta,
            peak_memory=timer.peak_memory,
        )
