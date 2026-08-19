#!/usr/bin/env python
"""
Run every check in one go.

    python tests/run_all.py

Each suite is a plain script with no test-runner dependency, so this works on a
fresh clone with nothing installed beyond requirements.txt. They run in
SIMULATED mode and against throwaway databases — a test must never be able to
reach a real customer or touch real bookings.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SUITES = [
    ("test_past.py",      "booking in the past (date + time)"),
    ("test_templates.py", "WhatsApp 24-hour window and templates"),
    ("test_webhook.py",   "webhook signatures, de-duplication, fast ack"),
    ("test_handler.py",   "per-customer locking, error containment, expiry"),
]

total = failed = 0
for script, description in SUITES:
    print(f"\n{'=' * 66}\n{script}  —  {description}\n{'=' * 66}")
    result = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).parent / script)],
        cwd=ROOT, capture_output=True, text=True,
    )
    # Each suite prints its own PASS/FAIL lines; show them, drop the log noise.
    for line in result.stdout.splitlines():
        if line.strip().startswith(("PASS", "FAIL", "===")) or "passed," in line:
            print(line)
    tail = [l for l in result.stdout.splitlines() if "passed," in l]
    if tail:
        n, f = tail[-1].split()[0], tail[-1].split()[2]
        total += int(n); failed += int(f)
    if result.returncode != 0 and not tail:
        print("  SUITE ERRORED:\n", result.stderr[-600:])
        failed += 1

print(f"\n{'=' * 66}")
print(f"TOTAL: {total} checks, {failed} failed")
print("=" * 66)
sys.exit(1 if failed else 0)
