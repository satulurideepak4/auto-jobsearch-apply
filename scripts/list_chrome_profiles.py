#!/usr/bin/env python3
"""List all Chrome profiles so you can pick the one to use for automation."""
import json
import sys
from pathlib import Path

CHROME_DATA = Path.home() / "Library/Application Support/Google/Chrome"

state_file = CHROME_DATA / "Local State"
if not state_file.exists():
    print("Chrome user data not found at", CHROME_DATA)
    sys.exit(1)

state = json.loads(state_file.read_text())
profiles = state.get("profile", {}).get("info_cache", {})

print(f"\nChrome profiles found in: {CHROME_DATA}\n")
print(f"{'DIR NAME':<20} {'DISPLAY NAME':<30} {'EMAIL'}")
print("-" * 70)
for dir_name, info in profiles.items():
    display = info.get("name", "")
    email = info.get("user_name", "")
    print(f"{dir_name:<20} {display:<30} {email}")

print(f"\nSet CHROME_PROFILE=<DIR NAME> in your .env to pick one.")
print("Example:  CHROME_PROFILE=Profile 1")
