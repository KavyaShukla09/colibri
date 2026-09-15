# Hardware-Adaptive Systems Engineering Roadmap

> **Long-Term Architectural Vision**  
> While the initial empirical results in this fork were measured and validated on a specific laptop (Lenovo ThinkPad L380 Yoga, Intel Core i5 4C/8T, 16 GB RAM, WD SN720 NVMe), the long-term goal of this project is **not** to create a hardcoded setup for one machine. The goal is to develop a **hardware-adaptive runtime** capable of self-calibrating to arbitrary constrained CPU/NVMe hardware environments.

---

## 1. The Challenge of Heterogeneous Consumer Hardware

Consumer PCs exhibit immense heterogeneity across CPU, RAM, and storage subsystems:
- **CPUs:** 2-core budget laptops, 4-core mobile platforms, 8-to-16-core desktops, and hybrid architectures with Performance and Efficient cores (P-cores / E-cores). Instruction sets vary from AVX2/FMA3 to AVX-512 VNNI and AMX.
- **Memory Subsystems:** Single-channel DDR4 (~19 GB/s), dual-channel DDR4/DDR5 (38–80 GB/s), and unified LPDDR5X (up to 120+ GB/s).
- **Storage Subsystems:** SATA SSDs (500 MB/s), PCIe 3.0 NVMe (1.5–3.0 GB/s), PCIe 4.0/5.0 NVMe (5.0–12.0 GB/s), and varying sector sizes (512e vs 4Kn).

A static configuration that excels on a 4-core PCIe 3.0 laptop will either oversubscribe a 2-core machine or underutilize an 8-core PCIe 4.0 workstation.

---

## 2. Dynamic Hardware Probing Architecture

Our planned hardware-adaptation subsystem is structured into four automated probing stages:

```
   ┌────────────────────────────────────────────────────────┐
   │             Hardware Auto-Probing Framework            │
   └────────────────────────────────────────────────────────┘
                               │
       ┌───────────────┬───────┴───────┬───────────────┐
       ▼               ▼               ▼               ▼
  [ CPU Topology ] [ RAM Budget ]  [ Storage Probe ] [ Parameter Map ]
  • Physical Cores • Available RAM • DMA Bandwidth   • OMP Threads
  • SMT Multiplier • OS Reserve    • Sector Size     • Loader Lanes
  • AVX2/AVX-512   • Cache Quota   • Safe Queue Depth• Pipelined Slots
```

### Stage 1: CPU Topology & Vector ISA Discovery
- Identify physical execution cores vs. logical threads to prevent SMT contention.
- On hybrid x86 CPUs (Intel Alder Lake / Raptor Lake / Arrow Lake), distinguish P-cores from E-cores and bind compute threads to high-throughput vector ports.
- Detect vector extensions (AVX2, AVX-512F, AVX-512 VNNI) to select optimal micro-kernels dynamically.

### Stage 2: Dynamic Memory Headroom Planning
- Query system physical memory load and commit headroom (`GlobalMemoryStatusEx`).
- Automatically partition available RAM:
  $$\text{RAM}_{\text{allocated}} = \text{RAM}_{\text{total}} - \text{RAM}_{\text{OS\_reserve}} - \text{Buffer}_{\text{loader}}$$
- Allocate resident dense layers and pin slots proportionally to maximize hit rates without inducing OS paging.

### Stage 3: Storage DMA Calibration
- Issue micro-benchmark Direct I/O reads against the model file during engine initialization.
- Measure actual unbuffered DMA throughput and block latency across queue depths (1, 2, 4, 8, 16).
- Detect 4096-byte vs 512-byte sector alignment requirements automatically.

### Stage 4: Parameter Synthesis
The runtime maps measured hardware metrics to execution parameters:
- $\text{Compute Threads} = N_{\text{physical\_cores}}$
- $\text{Loader Lanes} = \min(\text{Optimal Queue Depth}, 2 \times N_{\text{physical\_cores}})$
- $\text{Streaming Pipeline Depth} = \text{Double-buffered (2)}$ for balanced storage, or $\text{Triple-buffered (3)}$ for slower SATA drives.

---

## 3. Status & Implementation Notice

> [!IMPORTANT]
> **Implementation Scope**  
> In the current codebase, hardware awareness is implemented for **physical-core OpenMP autotuning (`c/omp_tune.h`)** and **Win32 sector-aligned Direct DMA**. The comprehensive dynamic auto-probing framework outlined above represents an active research and development roadmap for future releases.
