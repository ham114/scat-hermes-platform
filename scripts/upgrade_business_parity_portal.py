"""Upgrade only the portal static release, preserving prior evidence and Nginx."""

import json
import os
import shutil
from pathlib import Path

import deploy_portal_gateway as portal

if __name__ == "__main__":
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    output = root / "var/validation/20261108-business-parity"
    original = output / "portal-result.json"
    if not original.exists():
        original = portal.OUT / "portal-result.json"
    current = json.loads(original.read_text())
    assert (portal.DEPLOY / "active-release").read_text().strip() == current["release"]
    if original != output / "portal-result.json":
        shutil.copyfile(original, output / "portal-result.json")
    shutil.copyfile(original, output / ("portal-result-" + current["release"] + ".json"))
    portal.OUT = output
    portal.upgrade()
