# Low-Level Systems Architecture

> **Attribution Notice**  
> This document describes systems optimizations implemented in this performance fork. The foundational Colibrì multi-tier streaming architecture was invented and developed by **Vincenzo Fornaro ([@JustVugg](https://github.com/JustVugg))**. This fork modifies and extends the DeepSeek-V4 execution pipeline specifically for Windows NT / AVX2 platforms.

---

## 1. Overview & Execution Model

The central premise of Colibrì is treating storage (NVMe SSD), system memory (RAM), and accelerator memory (VRAM) as a single coherent memory hierarchy. For sparse Mixture-of-Experts (MoE) architectures such as `DeepSeek-V4-Flash-0731-reap-150b` (43 layers, 132 routed experts per layer, top-8 routed per token, ~84.7 GB quantized weights), consumer systems cannot fit the full model in RAM.

Instead of paging or swapping, Colibrì streams only the actively routed expert matrices required for each token on demand.

```
   ┌────────────────────────────────────────────────────────────────────────┐
   │                          MoE Layer Forward Pass                        │
   └────────────────────────────────────────────────────────────────────────┘
                                      │
               ┌──────────────────────┴──────────────────────┐
               ▼                                             ▼
     [ Dense Components ]                         [ Dynamic Expert Routing ]
     • Attention & Norms (In-RAM)                 • Router logits computed
     • Shared Experts (In-RAM)                    • Top-k active experts selected
               │                                             │
               └──────────────────────┬──────────────────────┘
                                      ▼
             [ Double-Buffered Overlapped Execution Pipeline ]
             ┌─────────────────────────┬─────────────────────────┐
             │ Loader Lanes (Direct IO)│ Compute Team (AVX2)     │
             ├─────────────────────────┼─────────────────────────┤
             │ Fetch Expert (k+1) DMA  │ Compute Expert (k) GEMM │
             │ ReOpenFile Win32 Handle │ 4 Physical Cores        │
             │ 4KB Sector Coalescing   │ Stack-allocated buffers │
             └─────────────────────────┴─────────────────────────┘
```

---

## 2. Windows Kernel `FileObject` Busy Lock Serialization

### The Problem
On the Windows NT kernel, file handles are represented by kernel `FILE_OBJECT` structures. When multiple worker threads issue synchronous or asynchronous I/O requests concurrently against the same Win32 `HANDLE`, the NT I/O manager serializes operations by acquiring the internal `FileObject->Busy` synchronization lock.

In multi-threaded expert streaming (e.g. 8 loader worker threads reading different expert offsets simultaneously), this synchronization serialized dispatch at the kernel level, creating severe thread contention and reducing concurrent NVMe queue depth.

### The Solution: Thread-Private `ReOpenFile` Duplication
We implemented thread-local direct handle isolation. Each background loader thread creates its own duplicate handle using the Win32 `ReOpenFile` API:

```c
HANDLE v4_get_thread_handle(HANDLE base_handle) {
    static __thread HANDLE tls_handle = INVALID_HANDLE_VALUE;
    if (tls_handle == INVALID_HANDLE_VALUE) {
        tls_handle = ReOpenFile(base_handle, 
                                GENERIC_READ, 
                                FILE_SHARE_READ, 
                                FILE_FLAG_NO_BUFFERING | FILE_FLAG_OVERLAPPED);
    }
    return tls_handle;
}
```

By opening dedicated kernel file objects per loader lane with `FILE_FLAG_NO_BUFFERING | FILE_FLAG_OVERLAPPED`, concurrent read requests dispatch directly to the NVMe driver without encountering `FileObject->Busy` locks.

---

## 3. Zero-Fallback Aligned Direct Window & Shard Coalescing

### The Problem: Shard Straddling and Sector Alignment
Direct I/O on Windows requires that:
1. File offsets must be integer multiples of the physical storage sector size (typically 4096 bytes).
2. Read lengths must be integer multiples of 4096 bytes.
3. Destination memory buffers must be aligned to sector boundaries (`_aligned_malloc` or `posix_memalign`).

In the DeepSeek-V4 150B checkpoint, ~25.3% of experts (1,436 out of 5,676 expert records) cross SafeTensors shard boundaries or start at non-sector-aligned offsets. In upstream Colibrì, any misalignment or shard boundary caused the engine to fall back to buffered POSIX `pread` (`pread_fallback`), generating 2,624 buffered fallbacks per 50 tokens, polluting the Windows standby cache, and incurring high memcpy overhead.

### The Solution: Outward Window Rounding and In-Place Coalescing
We reworked the direct window reader to guarantee 100% Direct DMA:
- **Outward Sector Alignment:** For any tensor slice spanning offset `[off, off + len)`, the reader computes the enclosing sector bounds:
  $$\text{base} = \text{off} \ \& \ \sim(4095)$$
  $$\text{aligned\_len} = ((\text{off} + \text{len} + 4095) \ \& \ \sim(4095)) - \text{base}$$
- **Multi-Shard Coalescing:** For tensors straddling two shards, the reader calculates the byte count remaining in the first shard, issues an aligned Direct DMA read for shard $A$, issues an aligned Direct DMA read for shard $B$, and stitches the payload directly into destination memory.
- **In-Place Shift Guards:** For sub-sector offsets, the payload is shifted to the target pointer via `memmove` within the aligned sector buffer, completely bypassing the OS page cache.

**Result:** Zero buffered fallbacks (2,624 $\to$ 0) across all benchmark runs.

---

## 4. Double-Buffered Overlapped MoE Pipeline

### The Problem
Under stock layer execution, the engine executed in rigid sequential stages:
1. Identify all active routed experts for the current token.
2. Bulk-load all required expert matrices from NVMe.
3. Synchronize via an execution barrier.
4. Execute OpenMP AVX2 GEMMs for all experts.

During stage 2, CPU execution units sat idle waiting for disk DMA. During stage 4, the NVMe SSD sat idle waiting for matrix-vector multiplication.

### The Solution: Pipelined Overlap with Early Memory Release
Under `COLI_V4_EXPERIMENTAL_DUAL_EXPERT_LOADER`, we restructured the expert execution loop into a streaming double-buffered pipeline:
- As soon as expert $k$'s weights finish loading into memory, its OpenMP AVX2 GEMM forward pass begins immediately.
- Concurrently, loader worker threads stream expert $k+1$'s weights over NVMe Direct DMA.
- Once expert $k$'s forward pass completes, its memory lease is released immediately back to the pool, reducing peak working set.

---

## 5. Hot Path Allocation Elimination

In high-frequency token generation, repeated heap allocations (`malloc`/`free`) in the inner MoE layer loop introduce lock contention in the C runtime allocator and degrade cache locality.

We replaced dynamic per-layer allocations with fixed-size stack buffers:
- Active expert indices, routing weights, and job descriptors are held in stack arrays sized for standard topologies (`gate_static[16]`, `jobs_static[16]`).
- For standard DeepSeek-V4 topologies (top-6 to top-8 active routed experts), the hot path performs zero heap allocations.
- Dynamic heap fallback is retained as a safety guard if an atypical topology exceeds stack capacity.

---

## 6. Physical-Core OpenMP Autotuning (`omp_tune.h`)

On an Intel Core i5 (4 physical cores, 8 logical threads via Hyper-Threading):
- OpenMP defaults to 8 compute threads (`omp_get_max_threads()`).
- Hyper-Threading sibling pairs compete for the same physical 256-bit AVX2 execution ports and share L1/L2 caches.
- Meanwhile, 8 background loader threads handle asynchronous Direct I/O completions.

With 8 OpenMP threads + 8 loader threads, 16 software threads actively contended for 4 physical cores, resulting in scheduling quanta exhaustion and thread migration penalties.

We implemented hardware-aware topology detection in `c/omp_tune.h`:
- Caps OpenMP compute teams strictly to **4 physical execution cores** (`OMP_NUM_THREADS=4`).
- Reserves remaining logical threads for non-blocking Direct I/O loader lanes.
- Enforces active spin-wait (`OMP_WAIT_POLICY=active`, `GOMP_SPINCOUNT=200000`) during token generation to eliminate thread wakeup latency.

---

## 7. Scale Tensor Direct DMA

While primary FP4 weight matrices were routed through the Direct I/O pipeline, block dequantization scales were previously read via standard POSIX `pread`.

We extended 4KB sector-aligned Direct DMA to block scales:
- Scaled reads are staged through a 4KB-aligned scratch buffer with thread-private handles.
- Completely eliminated buffered read calls from the tensor loading path.
- Lifted the Direct DMA payload ratio to **100.000%** (`40,562,589,696 / 40,562,589,696` bytes).
