# Benchmarking Methodology & Memory Hygiene Protocol

> **Core Philosophy**  
> Systems-performance claims must be backed by reproducible, transparent, and rigorous empirical measurements. In streaming inference where storage and RAM interact dynamically, uncontrolled variables (such as OS page cache pollution, lingering background processes, and mismatched context lengths) can drastically distort results.

---

## 1. Distinction of Baselines

To maintain complete scientific honesty, this repository explicitly distinguishes between three separate stages of the codebase:

1. **Untouched Upstream Colibrì Baseline:**  
   The unmodified upstream codebase at commit `f028d26b` (post-`v1.11.0`).  
   *Note:* Pure upstream was not benchmarked under the exact same multi-unit Windows AVX2 flags as our custom branch; we do not fabricate or interpolate a benchmark number for it.
2. **Initial Custom Baseline:**  
   The first working version configured by the author before the Antigravity optimization series:
   - Native MSYS2 GCC compilation with `-O3 -march=native -fopenmp -flto -DCOLI_V4_EXPERIMENTAL_DUAL_EXPERT_LOADER`
   - 12.0 GB pinned memory allocation, `CTX=256`, `PIPE=1`, 1 pin slot per layer
   - Sustained 50 tokens: ~270.8s wall time, ~222.4s decode (~4.448 s/tok), 2,624 buffered fallbacks (25.3%), ~498 MB/s sustained throughput
   - Cold 5 tokens: ~133.41s wall time, ~65.57s TTFT, ~43.02s decode (~10.76 s/tok), ~71% Direct I/O
3. **Current Optimized Fork:**  
   The codebase incorporating all 5 Antigravity low-level systems optimizations:
   - Zero-fallback 4KB sector window coalescing (fallbacks: 2,624 $\to$ 0)
   - Win32 `ReOpenFile` thread-private handle isolation
   - Overlapped double-buffered MoE compute pipeline + stack buffers
   - Physical-core autotuning (`OMP_NUM_THREADS=4`, 8 loader lanes)
   - Scale tensor Direct DMA (100.000% Direct I/O payload ratio)
   - Sustained 50 tokens: 284.53s wall time, 62.48s TTFT, 218.61s decode (~4.461 s/tok), 0 fallbacks, 618.3 MB/s sustained NVMe throughput
   - Cold 5 tokens: 83.06s wall time, 62.78s TTFT, 17.46s decode (~4.365 s/tok), 0 fallbacks, 100.000% Direct DMA

---

## 2. Memory Hygiene Protocol

Before every formal trial or A/B comparison, the test harness (`benchmarks/scripts/bench_harness.py`) executes the following protocol:

```
 [1. Process Check] ──► [2. Clean Workers] ──► [3. Trim Working Set]
                                                        │
 [6. Execute Run]   ◄── [5. Log Telemetry] ◄── [4. Stabilization Pause]
```

### Mandatory Steps
1. **Process Isolation:** Verify that no orphaned `deepseek_v4.exe`, `iobench.exe`, or compiler instances remain active.
2. **Worker Teardown:** Ensure all background loader threads and worker pools from prior executions are terminated.
3. **Working Set Trimming:** The harness calls the Win32 `EmptyWorkingSet` API on its own process to release dormant heap pages.
4. **Stabilization Period:** Enforce a 1.5-second idle pause to allow Windows memory management threads to settle.
5. **Memory State Logging:** Record physical memory load, available RAM, and available commit space via `GlobalMemoryStatusEx`.
6. **Execution:** Launch the engine with explicit environment flags (`CTX`, `PIPE`, `OMP_NUM_THREADS`, `COLI_LOADER_LANES`).
7. **Telemetry Capture:** Record end-to-end wall time, process stderr telemetry, memory delta, and generated token output.

### What NOT to Do
- **Do NOT** kill unrelated Windows operating system services or background processes.
- **Do NOT** use invasive third-party RAM cleaners that destabilize driver caches.
- **Do NOT** force hard memory paging or corrupt OS page pool state.
- **Do NOT** conflate cold-cache numbers with warm-cache numbers.

---

## 3. Cold vs. Warm Benchmark Classes

| Benchmark Class | Definition & Purpose | Execution Procedure |
| :--- | :--- | :--- |
| **Cold Benchmark** | Measures responsiveness when the model weights are not in the operating system file cache. Critical for evaluating cold-start latency, TTFT, and direct storage efficiency. | Run immediately following system boot or after explicit cache purging. Evaluates first-turn interactive speed. |
| **Warm Benchmark** | Measures sustained streaming performance when dense layer components are resident and the engine reaches a steady-state caching equilibrium. | Run with multiple sequential turns or long generation sequences (50+ tokens). |

---

## 4. Benchmark Honesty Guidelines

- **Always report both TTFT and Decode Latency:** Reporting only wall time obscures whether gains occurred in prefill or autoregressive generation.
- **Never conflate 5-token and 50-token results:** A 5-token cold run measures latency (~4.36 s/tok), whereas a 50-token canonical run measures sustained throughput (~4.46 s/tok, 218.6s decode). 
- **Thermal and Bandwidth Realities:** On a 15W TDP mobile quad-core CPU, sustained AVX2 compute across 50 tokens is fundamentally constrained by dual-channel DDR4 memory bandwidth and thermal envelope. We report these figures transparently without exaggerated claims.
