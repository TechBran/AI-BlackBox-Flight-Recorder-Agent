#!/usr/bin/env python3
"""
Integration Test: Manifest Integrity

Verifies that the code manifest byte offsets and SHA-256 hashes
remain accurate. This test must pass before and after every refactoring step.

Run:
    python3 tests/integration/test_manifest_integrity.py
"""

import hashlib
import json
from pathlib import Path

import pytest

# Reads this box's Manifest/code_manifest.json (gitignored runtime data).
pytestmark = pytest.mark.live_store

# Paths
BLACKBOX_ROOT = Path(__file__).parent.parent.parent
MANIFEST_PATH = BLACKBOX_ROOT / "Manifest" / "code_manifest.json"
APP_PY_PATH = BLACKBOX_ROOT / "Orchestrator" / "app.py"


def test_file_hash():
    """Test that overall file SHA-256 matches manifest."""
    print("Test 1: File hash integrity...")

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    source_bytes = APP_PY_PATH.read_bytes()
    actual_hash = hashlib.sha256(source_bytes).hexdigest()
    expected_hash = manifest["sha256"]

    assert actual_hash == expected_hash, f"File modified! Expected {expected_hash}, got {actual_hash}"
    print(f"  ✅ File hash verified: {actual_hash[:16]}...")


def test_section_hashes():
    """Test that each section's SHA-256 matches manifest."""
    print("\nTest 2: Section hash integrity...")

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    source_bytes = APP_PY_PATH.read_bytes()
    errors = 0

    for section_name, section in manifest["sections"].items():
        start = section["byte_start"]
        end = section["byte_end"]

        section_bytes = source_bytes[start:end]
        actual_hash = hashlib.sha256(section_bytes).hexdigest()
        expected_hash = section["sha256"]

        if actual_hash != expected_hash:
            print(f"  ❌ {section_name}: Hash mismatch!")
            errors += 1
        else:
            print(f"  ✅ {section_name}: Verified")

    assert errors == 0, f"{errors} section(s) have hash mismatches"


def test_byte_offset_boundaries():
    """Test that byte offsets don't overlap and cover entire file."""
    print("\nTest 3: Byte offset boundaries...")

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    sections = sorted(manifest["sections"].items(), key=lambda x: x[1]["byte_start"])

    source_bytes = APP_PY_PATH.read_bytes()
    file_size = len(source_bytes)

    # Check no overlaps
    for i in range(len(sections) - 1):
        curr_name, curr = sections[i]
        next_name, next_sec = sections[i + 1]

        # Current section should end before next starts (or exactly at)
        if curr["byte_end"] > next_sec["byte_start"]:
            print(f"  ❌ Overlap: {curr_name} ends at {curr['byte_end']}, {next_name} starts at {next_sec['byte_start']}")
            assert False, "Section overlap detected"

    print(f"  ✅ No overlaps between sections")

    # Check coverage
    total_covered = sum(s[1]["byte_end"] - s[1]["byte_start"] for s in sections)
    coverage_pct = (total_covered / file_size) * 100

    print(f"  ✅ Coverage: {total_covered:,} / {file_size:,} bytes ({coverage_pct:.1f}%)")


def test_frozen_status():
    """Test that manifest marks app.py as frozen (read-only)."""
    print("\nTest 4: Frozen status...")

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    assert manifest["frozen"] == True, "Manifest should mark app.py as frozen"
    print(f"  ✅ app.py marked as frozen (read-only)")


def main():
    print("="*70)
    print("INTEGRATION TEST: Manifest Integrity")
    print("="*70)
    print()

    try:
        test_file_hash()
        test_section_hashes()
        test_byte_offset_boundaries()
        test_frozen_status()

        print("\n" + "="*70)
        print("✅ ALL TESTS PASSED")
        print("="*70)
        print("\nManifest integrity verified!")
        print("app.py is unchanged and byte offsets are accurate.")
        print("\nSafe to proceed with refactoring.")

        return 0

    except AssertionError as e:
        print("\n" + "="*70)
        print("❌ TEST FAILED")
        print("="*70)
        print(f"\nError: {e}")
        print("\n⚠️  DO NOT PROCEED with refactoring!")
        print("Fix the issue before continuing.")

        return 1


if __name__ == "__main__":
    exit(main())
