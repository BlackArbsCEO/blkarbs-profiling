# blkarbs-profiling

Find out how long your code takes and how much memory it uses. That's it.

## Install

```bash
uv add blkarbs-profiling
```

Or with pip:

```bash
pip install blkarbs-profiling
```

## Quick start

### Time a block of code

```python
from blkarbs_profiling import ComponentTimer

with ComponentTimer() as timer:
    # your slow code here
    data = load_big_dataset()

print(f"Took {timer.elapsed:.2f} seconds")
print(f"Used {timer.memory_delta:.2f} GB of memory")
```

### Track multiple steps and get a summary table

```python
from blkarbs_profiling import ProfilingSession, ComponentTimer

session = ProfilingSession()

# Step 1: Load data
with ComponentTimer() as timer:
    data = load_data()
session.record("Load Data", timer.elapsed, count=len(data),
               memory_delta=timer.memory_delta, peak_memory=timer.peak_memory)

# Step 2: Train model
with ComponentTimer() as timer:
    model = train(data)
session.record("Train Model", timer.elapsed, count=1,
               memory_delta=timer.memory_delta, peak_memory=timer.peak_memory)

# Print a nice table
session.print_summary("My Pipeline")
```

Output:

```
========================================================================================================================
                                                    My Pipeline
========================================================================================================================
Component                                      Time      Count      Throughput   Per-Item      Mem Δ       Peak
------------------------------------------------------------------------------------------------------------------------
Load Data                                      2.50s         50    20.0 items/s    50.0ms      0.30G      4.20G
Train Model                                   15.00s          1     0.1 items/s 15000.0ms      1.20G      5.40G
========================================================================================================================
                  TOTAL                        17.50s
========================================================================================================================
```

### Use the shortcut context manager

If you don't want to manually call `session.record()` every time:

```python
from blkarbs_profiling import ProfilingSession, profile_operation

session = ProfilingSession()

with profile_operation("Load Data", session, count=50):
    data = load_data()

with profile_operation("Train Model", session, count=1):
    model = train(data)

session.print_summary("My Pipeline")
```

Pass `session=None` and the code still runs — it just doesn't record anything. No need for `if/else` at your call sites.

### Time things inside a loop (fast)

`ComponentTimer` checks memory on every call, which is slow (~0.5ms). For tight loops, use `AccumulatingTimer` — it only uses `time.perf_counter()` (~150 nanoseconds).

```python
from blkarbs_profiling import AccumulatingTimer, ProfilingSession

session = ProfilingSession()

timer = AccumulatingTimer("Process Bars")
for bar in price_bars:
    with timer:
        strategy.on_bar(bar)

# Dump the total into the session
timer.flush(session)

session.print_summary("Backtest")
```

### Check progress mid-pipeline

For long jobs, you can log a quick checkpoint without waiting for the final table:

```python
session.record("Step 1", elapsed=30.0, count=500, peak_memory=4.2, memory_delta=1.3)

session.log_checkpoint("After Step 1")
# Output:
# [CHECKPOINT: After Step 1] Total elapsed: 30.00s
#   Step 1: 30.00s, 500 items (16.7/s), peak=4.20GB, Δ=+1.30GB
```

### Save results to a file

```python
from pathlib import Path

session.flush_to_file(Path("profiling_results.json"))
```

Writes a JSON file with all the metrics. Useful for comparing runs or feeding into dashboards.

## What's in the box

| Class / Function | What it does |
|---|---|
| `ComponentTimer` | Times a block of code. Tracks memory too (via psutil). |
| `AccumulatingTimer` | Ultra-fast timer for tight loops. No memory tracking. |
| `ProfilingSession` | Collects timing from multiple steps. Thread-safe. |
| `profile_operation()` | Shortcut: wraps `ComponentTimer` + auto-records to a session. |

## Dev setup

```bash
uv sync --dev
uv run pytest
```

## License

MIT
