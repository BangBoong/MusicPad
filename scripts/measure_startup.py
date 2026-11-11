"""Startup measurement script for MusicPad.

Measures the elapsed time from process launch until the main application
window is visible on screen, supporting warm launches and statistical aggregation.
Also provides parsing of `python -X importtime` logs.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from typing import Any, Optional

user32 = ctypes.windll.user32 if sys.platform == "win32" else None


def compute_statistics(values: list[float]) -> dict[str, float]:
    """Calculate min, max, median, and mean for a list of measurement times."""
    if not values:
        return {"min": 0.0, "max": 0.0, "median": 0.0, "mean": 0.0}
    return {
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "median": round(statistics.median(values), 2),
        "mean": round(statistics.mean(values), 2),
    }


def parse_importtime_output(stderr: str, top_n: int = 15) -> list[dict[str, Any]]:
    """Parse python -X importtime stderr output and return top_n by cumulative time.

    Output format of python -X importtime:
    import time: self [us] | cumulative | imported package
    import time:        90 |         90 | winreg
    """
    pattern = re.compile(r"^import time:\s+(\d+)\s+\|\s+(\d+)\s+\|\s+(.+)$")
    entries: list[dict[str, Any]] = []

    for line in stderr.splitlines():
        line = line.strip()
        match = pattern.match(line)
        if match:
            self_us = int(match.group(1))
            cum_us = int(match.group(2))
            pkg = match.group(3).strip()
            entries.append(
                {
                    "package": pkg,
                    "self_us": self_us,
                    "cumulative_us": cum_us,
                    "cumulative_ms": round(cum_us / 1000.0, 2),
                    "self_ms": round(self_us / 1000.0, 2),
                }
            )

    entries.sort(key=lambda x: x["cumulative_us"], reverse=True)
    return entries[:top_n]


def find_target_window(title: str) -> Optional[int]:
    """Find a top-level visible window with the exact matching title."""
    if user32 is None:
        return None
    hwnd = user32.FindWindowW(None, title)
    if hwnd and user32.IsWindowVisible(hwnd):
        return hwnd
    return None


def wait_for_window(
    title: str, timeout_sec: float = 30.0, poll_interval_sec: float = 0.01
) -> tuple[bool, Optional[int]]:
    """Poll for a visible window with the specified title until timeout."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_sec:
        hwnd = find_target_window(title)
        if hwnd is not None:
            return True, hwnd
        time.sleep(poll_interval_sec)
    return False, None


def kill_process_tree(pid: int) -> None:
    """Terminate a process and all of its descendant child processes."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
    try:
        os.kill(pid, 9)
    except Exception:
        pass


def measure_single_run(
    cmd: list[str] | str, title: str = "MusicPad", timeout_sec: float = 30.0
) -> float:
    """Launch the application command, wait for its visible window, and return elapsed ms."""
    if isinstance(cmd, str):
        cmd_args = shlex.split(cmd, posix=False) if sys.platform == "win32" else shlex.split(cmd)
    else:
        cmd_args = list(cmd)

    # Ensure no stale window with target title exists prior to launch
    existing = find_target_window(title)
    if existing:
        raise RuntimeError(
            f"A window with title '{title}' (HWND {existing}) is already open. "
            "Please close it before measuring startup time."
        )

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    found = False
    hwnd = None
    try:
        found, hwnd = wait_for_window(title, timeout_sec=timeout_sec)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    if not found:
        raise TimeoutError(f"Window '{title}' did not appear within {timeout_sec}s.")

    return elapsed_ms


def run_measurements(
    cmd: list[str] | str,
    runs: int = 5,
    warmup: bool = True,
    title: str = "MusicPad",
    timeout_sec: float = 30.0,
    verbose: bool = True,
) -> dict[str, Any]:
    """Execute multiple startup runs, discarding warmup run if requested."""
    total_runs = runs + (1 if warmup else 0)
    times: list[float] = []

    for i in range(total_runs):
        is_warmup_run = warmup and (i == 0)
        label = "Warmup" if is_warmup_run else f"Run {len(times) + 1}/{runs}"
        if verbose:
            print(f"[{label}] Launching: {cmd} ...", end=" ", flush=True)

        elapsed_ms = measure_single_run(cmd, title=title, timeout_sec=timeout_sec)
        if verbose:
            print(f"{elapsed_ms:.1f} ms")

        if not is_warmup_run:
            times.append(elapsed_ms)

        # Brief pause between runs to let OS clean up sockets/memory
        time.sleep(0.5)

    stats = compute_statistics(times)
    stats["runs"] = runs
    stats["times_ms"] = [round(t, 1) for t in times]
    return stats


def measure_python_importtime(top_n: int = 15) -> list[dict[str, Any]]:
    """Run python -X importtime on main.py and return top_n imports by cumulative time."""
    cmd = [sys.executable, "-X", "importtime", "main.py"]
    # Run with MusicPad mocked/self-test or headless mode so it doesn't open GUI
    # Using python -X importtime -c "import main" ensures imports execute without entering mainloop
    cmd = [sys.executable, "-X", "importtime", "-c", "import main"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return parse_importtime_output(res.stderr, top_n=top_n)


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure application startup time until visible window.")
    parser.add_argument("--cmd", type=str, help="Command to execute (e.g. 'dist/MusicPad.exe' or 'python main.py')")
    parser.add_argument("-n", "--runs", type=int, default=5, help="Number of measured runs (default: 5)")
    parser.add_argument("--no-warmup", action="store_true", help="Disable the initial warmup run")
    parser.add_argument("--title", type=str, default="MusicPad", help="Window title to wait for (default: 'MusicPad')")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-run timeout in seconds (default: 30.0)")
    parser.add_argument("--importtime", action="store_true", help="Measure and display python -X importtime top imports")
    parser.add_argument("--json", action="store_true", help="Output results formatted as JSON")

    args = parser.parse_args()

    results: dict[str, Any] = {}

    if args.importtime:
        print("\n--- Python Import Time Analysis (-X importtime) ---")
        top_imports = measure_python_importtime(top_n=15)
        results["importtime"] = top_imports
        if not args.json:
            print(f"{'Package':<35} | {'Cumulative (ms)':<15} | {'Self (ms)':<10}")
            print("-" * 66)
            for item in top_imports:
                print(f"{item['package']:<35} | {item['cumulative_ms']:<15.2f} | {item['self_ms']:<10.2f}")

    if args.cmd:
        print(f"\n--- Measuring Startup Time: {args.cmd} ---")
        stats = run_measurements(
            cmd=args.cmd,
            runs=args.runs,
            warmup=not args.no_warmup,
            title=args.title,
            timeout_sec=args.timeout,
            verbose=not args.json,
        )
        results["startup"] = stats
        if not args.json:
            print("\nResults:")
            print(f"  Runs:   {stats['runs']} (warmup discarded)")
            print(f"  Median: {stats['median']:.1f} ms ({stats['median']/1000.0:.2f} s)")
            print(f"  Min:    {stats['min']:.1f} ms")
            print(f"  Max:    {stats['max']:.1f} ms")
            print(f"  Mean:   {stats['mean']:.1f} ms")
            print(f"  Times:  {stats['times_ms']}")

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
