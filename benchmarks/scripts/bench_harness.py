"""
Benchmark Harness for Colibri DeepSeek-V4 Inference Optimization.

Implements strict Section 13 memory hygiene, system memory telemetry,
and output parsing for reproducible A/B testing and benchmarking.

Protocol:
1. Ensure no lingering engine or I/O processes remain.
2. Clean up benchmark child processes/workers.
3. Release prior benchmark allocations and trim own working set.
4. Allow stabilization period.
5. Record physical and commit memory state before execution.
6. Execute engine with explicit parameter flags.
7. Record memory, wall time, TTFT, and decode throughput.
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', ctypes.c_ulong),
        ('dwMemoryLoad', ctypes.c_ulong),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]

def get_memory_status():
    """Queries Windows GlobalMemoryStatusEx for physical and commit memory."""
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    return {
        'total_phys_gb': ms.ullTotalPhys / (1024.0 ** 3),
        'avail_phys_gb': ms.ullAvailPhys / (1024.0 ** 3),
        'used_phys_gb': (ms.ullTotalPhys - ms.ullAvailPhys) / (1024.0 ** 3),
        'mem_load_pct': ms.dwMemoryLoad,
        'total_commit_gb': ms.ullTotalPageFile / (1024.0 ** 3),
        'avail_commit_gb': ms.ullAvailPageFile / (1024.0 ** 3),
    }

def trim_working_set():
    """Trims current process working set via Win32 EmptyWorkingSet."""
    try:
        ctypes.windll.psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
    except Exception as e:
        print(f"[HYGIENE WARN] EmptyWorkingSet: {e}")

def kill_lingering_engine_processes():
    """Terminates lingering deepseek or iobench engine processes."""
    try:
        cmd = "Get-Process -Name *deepseek*, *iobench* -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
        out = subprocess.check_output(['powershell', '-Command', cmd], text=True).strip()
        if out:
            pids = out.split()
            print(f"[HYGIENE] Terminating lingering processes: {pids}")
            subprocess.run(['powershell', '-Command', f"Stop-Process -Id {','.join(pids)} -Force"], check=False)
            time.sleep(1.0)
    except Exception:
        pass

def run_trial(model_path, prompt, tokens=50, memory_gb=12.0, ctx=256, pipe=1,
              omp_threads=4, loader_lanes=8, extra_env=None, output_json=None):
    print("=" * 72)
    print("COLIBRI BENCHMARK HARNESS")
    print(f"Model: {model_path}")
    print(f"Tokens: {tokens} | Memory Budget: {memory_gb} GB | CTX: {ctx} | PIPE: {pipe}")
    print(f"Threads: OMP_NUM_THREADS={omp_threads} | Loader Lanes: {loader_lanes}")
    print(f"Prompt: {prompt[:80]}...")
    print("=" * 72)

    # 1. Hygiene & stabilization
    kill_lingering_engine_processes()
    trim_working_set()
    time.sleep(1.5)

    # 2. Pre-run memory
    mem_before = get_memory_status()
    print(f"[MEM BEFORE] Avail: {mem_before['avail_phys_gb']:.2f} GB | "
          f"Used: {mem_before['used_phys_gb']:.2f} GB ({mem_before['mem_load_pct']}%) | "
          f"Commit Avail: {mem_before['avail_commit_gb']:.2f} GB")

    # 3. Environment configuration
    env = os.environ.copy()
    env["CTX"] = str(ctx)
    env["PIPE"] = str(pipe)
    env["OMP_NUM_THREADS"] = str(omp_threads)
    env["OMP_WAIT_POLICY"] = "active"
    env["GOMP_SPINCOUNT"] = "200000"
    env["OMP_DYNAMIC"] = "FALSE"
    env["V4_DRAFT"] = "0"
    env["V4_MTP"] = "0"
    env["COLI_LOADER_LANES"] = str(loader_lanes)
    if extra_env:
        env.update(extra_env)

    # Locate binary
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    engine_bin = os.path.join(root_dir, "c", "deepseek_v4.exe")
    if not os.path.exists(engine_bin):
        raise FileNotFoundError(f"Engine binary not found at {engine_bin}. Run make/gcc first.")

    cmd = [
        engine_bin,
        os.path.abspath(model_path),
        prompt,
        "--max-tokens", str(tokens),
        "--no-dspark",
    ]
    if memory_gb > 0:
        cmd.extend(["--memory-gb", str(memory_gb)])

    print(f"[LAUNCH] {' '.join(cmd)}")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    t1 = time.perf_counter()
    wall_sec = t1 - t0

    mem_after = get_memory_status()
    print(f"[MEM AFTER]  Avail: {mem_after['avail_phys_gb']:.2f} GB | "
          f"Used: {mem_after['used_phys_gb']:.2f} GB ({mem_after['mem_load_pct']}%)")
    print(f"[WALL TIME]  {wall_sec:.2f} s")

    print("\n--- ENGINE TELEMETRY (STDERR) ---")
    print(proc.stderr)
    print("\n--- ENGINE GENERATED OUTPUT ---")
    print(proc.stdout)

    result_data = {
        'status': 'SUCCESS' if proc.returncode == 0 else 'FAILURE',
        'returncode': proc.returncode,
        'wall_sec': round(wall_sec, 2),
        'tokens_requested': tokens,
        'mem_before': mem_before,
        'mem_after': mem_after,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
    }

    if output_json:
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(result_data, f, indent=2)
        print(f"[RESULT SAVED] {output_json}")

    return result_data

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Colibri DeepSeek-V4 Benchmark Harness")
    parser.add_argument("--model", type=str, default=r"D:\models\DeepSeek-V4-Flash-REAP-150B",
                        help="Path to DeepSeek-V4 model directory")
    parser.add_argument("--prompt", type=str,
                        default="Explain how a transformer neural network works, covering attention, feed-forward layers, and backpropagation in detail.",
                        help="Input prompt for generation")
    parser.add_argument("--tokens", type=int, default=50, help="Number of tokens to generate")
    parser.add_argument("--memory-gb", type=float, default=12.0, help="Pinned memory allocation in GB")
    parser.add_argument("--omp-threads", type=int, default=4, help="OpenMP compute threads (physical cores)")
    parser.add_argument("--loader-lanes", type=int, default=8, help="Concurrent Direct I/O loader lanes")
    parser.add_argument("--output-json", type=str, default=None, help="Path to save result JSON")
    args = parser.parse_args()

    run_trial(
        model_path=args.model,
        prompt=args.prompt,
        tokens=args.tokens,
        memory_gb=args.memory_gb,
        omp_threads=args.omp_threads,
        loader_lanes=args.loader_lanes,
        output_json=args.output_json,
    )
