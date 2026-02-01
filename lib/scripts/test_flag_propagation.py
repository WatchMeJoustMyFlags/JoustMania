#!/usr/bin/env python3
"""
Test script to verify flag propagation timing (Part of #21).

This script tests that flag changes propagate from flags.json to the
OpenFeature client within 10 seconds (success criteria for #21).

Usage:
    python scripts/test_flag_propagation.py

Prerequisites:
    - flagd container must be running
    - flags.json must exist
"""

import json
import time
from pathlib import Path

from lib.feature_flags import get_feature_flag_client

FLAG_FILE = Path(__file__).parent.parent / "services" / "flagd" / "flags.json"
TEST_FLAG_NAME = "test_propagation_flag"


def read_flags():
    """Read current flags from flags.json."""
    with open(FLAG_FILE) as f:
        return json.load(f)


def write_flags(flags):
    """Write flags to flags.json."""
    with open(FLAG_FILE, "w") as f:
        json.dump(flags, f, indent=2)


def add_test_flag(flags, value: bool):
    """Add or update test flag."""
    flags["flags"][TEST_FLAG_NAME] = {
        "state": "ENABLED",
        "variants": {"on": True, "off": False},
        "defaultVariant": "on" if value else "off",
    }
    return flags


def remove_test_flag(flags):
    """Remove test flag."""
    flags["flags"].pop(TEST_FLAG_NAME, None)
    return flags


def test_flag_propagation():
    """Test that flag changes propagate within 10 seconds."""
    print("🧪 Testing flag propagation timing...")
    print(f"📄 Flag file: {FLAG_FILE}")

    # Initialize client
    client = get_feature_flag_client()
    print("✅ OpenFeature client initialized")

    # Backup original flags
    original_flags = read_flags()
    print(f"📦 Backed up original flags ({len(original_flags['flags'])} flags)")

    try:
        # Test 1: Add a new flag
        print("\n🔄 Test 1: Adding new flag...")
        start_time = time.time()

        modified_flags = add_test_flag(original_flags.copy(), True)
        write_flags(modified_flags)
        print(f"✏️  Added {TEST_FLAG_NAME}=True to flags.json")

        # Poll until flag appears with correct value
        max_wait = 10.0  # 10 seconds max
        poll_interval = 0.5  # Check every 500ms
        elapsed = 0.0

        while elapsed < max_wait:
            try:
                value = client.get_boolean_value(TEST_FLAG_NAME, False)
                if value is True:
                    propagation_time = time.time() - start_time
                    print(
                        f"✅ Flag propagated in {propagation_time:.2f}s "
                        f"({'PASS' if propagation_time < 10 else 'FAIL'})"
                    )
                    break
            except Exception:
                pass

            time.sleep(poll_interval)
            elapsed = time.time() - start_time

        if elapsed >= max_wait:
            print(f"❌ Flag did not propagate within {max_wait}s (FAIL)")
            return False

        # Test 2: Modify existing flag
        print("\n🔄 Test 2: Modifying existing flag...")
        start_time = time.time()

        modified_flags = add_test_flag(original_flags.copy(), False)
        write_flags(modified_flags)
        print(f"✏️  Changed {TEST_FLAG_NAME}=False in flags.json")

        elapsed = 0.0
        while elapsed < max_wait:
            try:
                value = client.get_boolean_value(TEST_FLAG_NAME, True)
                if value is False:
                    propagation_time = time.time() - start_time
                    print(
                        f"✅ Flag change propagated in {propagation_time:.2f}s "
                        f"({'PASS' if propagation_time < 10 else 'FAIL'})"
                    )
                    break
            except Exception:
                pass

            time.sleep(poll_interval)
            elapsed = time.time() - start_time

        if elapsed >= max_wait:
            print(f"❌ Flag change did not propagate within {max_wait}s (FAIL)")
            return False

        print("\n✅ All propagation tests passed!")
        return True

    finally:
        # Restore original flags
        print("\n🔄 Restoring original flags...")
        write_flags(original_flags)
        print("✅ Original flags restored")


if __name__ == "__main__":
    print("=" * 60)
    print("Flag Propagation Test (Issue #21 Success Criteria)")
    print("=" * 60)

    success = test_flag_propagation()

    print("\n" + "=" * 60)
    if success:
        print("✅ SUCCESS: Flag propagation works within 10 seconds")
    else:
        print("❌ FAILURE: Flag propagation exceeds 10 seconds")
    print("=" * 60)

    exit(0 if success else 1)
