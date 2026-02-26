"""blkarbs-profiling: Reusable timing, throughput, and memory profiling utilities.

Provides:
- ComponentTimer: Context manager for timing code blocks with memory tracking
- AccumulatingTimer: Lightweight timer for hot-path profiling (perf_counter only)
- ProfilingSession: Thread-safe accumulator for multi-component timing data
- profile_operation: Convenience context manager combining timer + session recording

Usage:
    from blkarbs_profiling import ProfilingSession, ComponentTimer, profile_operation

    session = ProfilingSession()

    with ComponentTimer(track_memory=True) as timer:
        result = expensive_operation()
    session.record("Operation", timer.elapsed, count=100,
                   memory_delta=timer.memory_delta, peak_memory=timer.peak_memory)

    session.print_summary("Pipeline Results")
"""

from blkarbs_profiling._core import (
    AccumulatingTimer,
    ComponentTimer,
    ProfilingSession,
    profile_operation,
)

__all__ = [
    "AccumulatingTimer",
    "ComponentTimer",
    "ProfilingSession",
    "profile_operation",
]

__version__ = "0.1.0"
