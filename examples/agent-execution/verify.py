#!/usr/bin/env python3
"""Standalone verification script for Phase 6 Agent Execution Tier 2 Pattern Example."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_DIR = Path(__file__).resolve().parent


def run_cmd(cmd: list[str], cwd: Path) -> tuple[int, str]:
    res = subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False
    )
    return res.returncode, res.stdout


def main() -> int:
    print("=" * 70)
    print("Phase 6 Verification — Agentic Task Execution (Tier 2 Pattern Example)")
    print("=" * 70)

    # 1. Validate artifact.json manifest
    print("[1/3] Validating artifact.json schema...")
    code, out = run_cmd(
        [sys.executable, str(REPO_ROOT / "scripts" / "artifact_validator.py"), str(AGENT_DIR)],
        REPO_ROOT,
    )
    if code != 0:
        print(f"FAILED: artifact_validator error:\n{out}")
        return 1
    print("      -> artifact.json valid.")

    # 2. Run unit tests
    print("[2/3] Running hermetic unit tests...")
    code, out = run_cmd(
        [sys.executable, "-m", "unittest", "discover", "-s", str(AGENT_DIR / "tests")], REPO_ROOT
    )
    if code != 0:
        print(f"FAILED: Unit tests failed:\n{out}")
        return 1
    print("      -> Hermetic unit suite passed.")

    # 3. Run evaluation harness
    print("[3/3] Running 32-scenario evaluation harness (fake mode)...")
    code, out = run_cmd(
        [sys.executable, str(AGENT_DIR / "eval_runner.py"), "--mode", "fake"], REPO_ROOT
    )
    if code != 0:
        print(f"FAILED: Evaluation harness failed:\n{out}")
        return 1
    print("      -> 32/32 evaluation scenarios passed with zero safety invariant violations.")

    print("=" * 70)
    print("VERIFICATION RESULT: DETERMINISTIC PHASE 6 VERIFICATION PASSED")
    print("  Tier Classification: Tier 2 Pattern Example")
    print("  Authoritative Applicable Quality Gates: Gates B, C, H, I")
    print(
        "  Voluntary Reference Evaluation: Gate D benchmark executed in Mode A (harness verified; real AI quality unverified)"
    )
    print("  Separation of Authority: Model -> Validation -> Policy -> Mandatory HITL -> Execution")
    print("  Offline Execution: Mode A (100% Hermetic / Standard-library only)")
    print("  Note: Exit 0 certifies deterministic reference verification for Tier 2 under Mode A.")
    print("        It does not certify all repository quality gates across all tiers.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
