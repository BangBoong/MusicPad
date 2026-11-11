"""Unit tests for the startup measurement dev script."""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

# scripts/measure_startup.py will be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_startup import (
    compute_statistics,
    kill_process_tree,
    parse_importtime_output,
    wait_for_window,
)


class TestMeasureStartup(unittest.TestCase):
    def test_compute_statistics_odd_and_even(self) -> None:
        stats_odd = compute_statistics([100.0, 200.0, 300.0])
        self.assertEqual(stats_odd["min"], 100.0)
        self.assertEqual(stats_odd["max"], 300.0)
        self.assertEqual(stats_odd["median"], 200.0)

        stats_even = compute_statistics([100.0, 200.0, 300.0, 400.0])
        self.assertEqual(stats_even["min"], 100.0)
        self.assertEqual(stats_even["max"], 400.0)
        self.assertEqual(stats_even["median"], 250.0)

    def test_parse_importtime_output(self) -> None:
        sample_stderr = """
import time: self [us] | cumulative | imported package
import time:        90 |         90 | winreg
import time:       168 |        168 |   _io
import time:       458 |        804 | _frozen_importlib_external
import time:      1810 |       1810 |   _distutils_hack
import time:      1655 |       3897 | encodings
"""
        entries = parse_importtime_output(sample_stderr, top_n=3)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["package"], "encodings")
        self.assertEqual(entries[0]["cumulative_us"], 3897)
        self.assertEqual(entries[1]["package"], "_distutils_hack")
        self.assertEqual(entries[1]["cumulative_us"], 1810)
        self.assertEqual(entries[2]["package"], "_frozen_importlib_external")
        self.assertEqual(entries[2]["cumulative_us"], 804)

    def test_kill_process_tree(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertIsNone(proc.poll())
        kill_process_tree(proc.pid)
        # Process should be terminated
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("Process was not killed")
        self.assertIsNotNone(proc.poll())

    def test_wait_for_window_timeout(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Look for a non-existent window title with short timeout
            t0 = time.perf_counter()
            found, hwnd = wait_for_window("NonExistentWindowTitle_12345", timeout_sec=0.2)
            self.assertFalse(found)
            self.assertIsNone(hwnd)
            elapsed = time.perf_counter() - t0
            self.assertGreaterEqual(elapsed, 0.15)
        finally:
            kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
