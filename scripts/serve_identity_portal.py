"""Identity-aware loopback portal; private gateway keys replace backend credential extraction."""

import argparse
import hashlib
import http.server
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from serve_portal import handler

from hermes_runtime.portal_identity import PortalDirectory, private_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--port", type=int, default=18159)
    parser.add_argument("--origin", default="https://172.26.67.148:8444")
    parser.add_argument("--identities", default="var/deploy/portal/identities.json")
    parser.add_argument("--signing-key", default="var/deploy/portal/keys/signing.key")
    parser.add_argument("--front-key", default="var/deploy/portal/keys/front.key")
    args = parser.parse_args()
    release = (ROOT / args.release).resolve()
    assert release.is_relative_to(ROOT / "var/deploy/portal/releases")
    manifest = json.loads((release / "manifest.json").read_text())
    assert all(
        hashlib.sha256((release / "dist" / p).read_bytes()).hexdigest() == sha
        for p, sha in manifest["files"].items()
    )
    portal = SimpleNamespace(
        directory=PortalDirectory(ROOT, args.identities, args.signing_key),
        front_key=private_key(ROOT, args.front_key),
    )
    http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), handler(release / "dist", None, args.origin, portal=portal)
    ).serve_forever()


if __name__ == "__main__":
    main()
