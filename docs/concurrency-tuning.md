# Thread Tuning & Concurrency Optimization

> **Attribution Notice**  
> Upstream Colibrì includes foundational hardware and model autotuning mechanisms created by **Vincenzo Fornaro** (see [docs/tuning.md](tuning.md) for upstream tuning facilities). This document covers specific thread concurrency tuning, physical-core isolation, and OpenMP scheduling optimizations developed in this fork for Windows/AVX2.

---

## 1. Concurrency Bottleneck on SMT/Hyper-Threading Architectures

On modern multi-threaded x86 processors, Simultaneous Multi-Threading (SMT / Hyper-Threading) exposes two logical threads per physical execution core. For standard compute workloads, this provides a modest throughput boost. However, for dense SIMD/AVX2 matrix multiplication interleaved with asynchronous Direct I/O, SMT introduces severe resource contention:

1. **AVX2 Execution Port Contention:** Two logical threads executing on the same physical core contend for the same 256-bit vector ALUs and FMA execution ports. Running compute across all logical threads increases register pressure and context switching without increasing floating-point throughput.
2. **L1/L2 Cache Thrashing:** Sibling threads share L1 instruction/data caches and the unified L2 cache. Intermediate activation tensors and weight dequantization tables are repeatedly evicted.
3. **OS Scheduling Clashes:** If OpenMP creates 8 compute threads on a 4C/8T CPU while the engine also spawns 8 loader threads for Direct I/O, 16 software threads actively contend for 4 execution cores.

---

## 2. Physical-Core Autotuning Architecture (`c/omp_tune.h`)

To eliminate thread contention, we decoupled compute threads from I/O dispatch:

```
   ┌────────────────────────────────────────────────────────┐
   │                  4C / 8T CPU Allocation                │
   └────────────────────────────────────────────────────────┘
         │                                       │
         ▼                                       ▼
  [ Compute Pool ]                        [ Loader Lanes ]
  • 4 Physical Execution Cores            • 8 Asynchronous Lanes
  • OMP_NUM_THREADS = 4                   • COLI_LOADER_LANES = 8
  • Exclusive AVX2 FMA execution          • Non-blocking Win32 Direct DMA
  • OMP_WAIT_POLICY = active              • ReOpenFile handles in TLS
```

### Key Concurrency Knobs
- **`OMP_NUM_THREADS=4`:** Sized strictly to the count of physical cores. Compute tasks achieve full AVX2 saturation with zero SMT execution port contention.
- **`COLI_LOADER_LANES=8`:** Expanded from 3 to 8 concurrent loader lanes. Because Direct DMA operations (`FILE_FLAG_NO_BUFFERING | FILE_FLAG_OVERLAPPED`) are non-blocking and wait on PCIe bus transfer rather than CPU cycles, loader threads can overlap without starving compute units.
- **`OMP_WAIT_POLICY=active` & `GOMP_SPINCOUNT=200000`:** Enforces active spin-waiting for OpenMP worker threads between GEMM operations, eliminating OS thread sleep/wake latency during token generation.

---

## 3. Future Bounded Autotuning Vision

Upstream Colibrì provides parameter tuning capabilities. A key engineering goal for this fork is evolving parameter search toward **bounded coordinate tuning**:

Instead of brute-forcing exhaustive multi-dimensional grids:
1. **Coordinate Step 1:** Measure storage throughput across variable queue depths (1 to 16 lanes) using quick Direct I/O probe reads.
2. **Coordinate Step 2:** Test compute team sizes ($N_{\text{phys}}$ vs $N_{\text{log}}$) on a single prompt layer.
3. **Coordinate Step 3:** Tune expert cache size and prefetch window within the measured memory budget.
4. **Persistence:** Cache the optimal hardware profile in a local JSON configuration for zero-overhead startup on subsequent runs.
