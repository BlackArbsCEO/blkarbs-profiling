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
print(f"Peak RSS: {timer.peak_memory:.2f} GB")
print(f"End RSS: {timer.end_memory:.2f} GB")
```

`peak_memory` is the sampled peak process RSS during the block. `end_memory` is the
process RSS at the end of the block.

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

### Find out which functions are slow (cProfile call graph)

`ComponentTimer` tells you *how long* a block takes. `profile_callgraph` tells you *why* it's slow — which sub-functions inside that block consume the time.

```python
from blkarbs_profiling import profile_callgraph

with profile_callgraph(top_n=15, sort_by="cumulative"):
    run_backtest()
```

Output:

```
[cProfile] Top 15 functions by 'cumulative':
         84923 function calls in 2.341 seconds

   Ordered by: cumulative time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000    2.341    2.341 backtest.py:42(run_backtest)
     5000    1.102    0.000    1.891    0.000 strategy.py:18(on_price)
     5000    0.789    0.000    0.789    0.000 indicators.py:55(calc_ema)
     ...
```

Now you know `calc_ema` is the bottleneck — something `ComponentTimer` alone cannot reveal.

#### Save a .prof file for visualization

```python
from pathlib import Path
from blkarbs_profiling import profile_callgraph

with profile_callgraph(top_n=20, sort_by="time", output_path=Path("backtest.prof")):
    run_backtest()
```

Then open it with snakeviz:

```bash
pip install snakeviz
snakeviz backtest.prof
```

Or generate a call graph image with gprof2dot:

```bash
pip install gprof2dot
gprof2dot -f pstats backtest.prof | dot -Tpng -o callgraph.png
```

#### Sort keys

The `sort_by` parameter accepts any valid `pstats.SortKey` value:

| Key | What it sorts by |
|---|---|
| `"cumulative"` | Total time spent in function + its callees (default) |
| `"time"` | Time spent in the function itself (excludes callees) |
| `"calls"` | Number of times the function was called |
| `"name"` | Function name alphabetically |
| `"filename"` | Source file name |

Use `"cumulative"` to find the slow code path from the top down. Use `"time"` to find the single hottest function.

### Combining tools: the full picture

Each tool answers a different question. Use them together to go from "this is slow" to "here's exactly why."

```python
from pathlib import Path
from blkarbs_profiling import (
    ProfilingSession,
    AccumulatingTimer,
    profile_callgraph,
    profile_operation,
)

session = ProfilingSession()

# 1. profile_callgraph: wrap the entire pipeline to get the call graph
with profile_callgraph(top_n=30, sort_by="cumulative", output_path=Path("pipeline.prof")):

    # 2. profile_operation: time coarse-grained steps with memory tracking
    with profile_operation("Data Loading", session, count=1):
        data = load_data()

    # 3. AccumulatingTimer: time the hot loop with minimal overhead
    signal_timer = AccumulatingTimer("Signal Generation")
    for bar in data.itertuples():
        with signal_timer:
            strategy.on_price(bar)
    signal_timer.flush(session)

    with profile_operation("Risk Calculation", session, count=len(data)):
        risk = calculate_risk(strategy.positions)

# 4. ProfilingSession: get the summary table
session.print_summary("Full Pipeline")
# Then: snakeviz pipeline.prof  — to drill into whichever step is slowest
```

What each layer tells you:

| Tool | Question it answers |
|---|---|
| `ProfilingSession` table | "Which *step* is slowest? How much memory did each use?" |
| `AccumulatingTimer` | "How much total time does this hot loop burn, at near-zero overhead?" |
| `profile_callgraph` | "Which *sub-function* inside the slow step is the actual bottleneck?" |
| `.prof` file + snakeviz | "Show me the full call graph visually so I can trace the hot path." |

Typical workflow:

1. Run with `ProfilingSession` — identify that "Signal Generation" takes 80% of time.
2. Wrap the pipeline with `profile_callgraph` — discover that `calc_ema()` inside `on_price()` is the bottleneck.
3. Optimize `calc_ema()`, re-run, verify the time dropped in both the session table and the call graph.

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
| `profile_callgraph()` | cProfile wrapper — reveals which sub-functions are slow. Dumps `.prof` files. |

## Dev setup

```bash
uv sync --dev
uv run pytest
```

## License

MIT
