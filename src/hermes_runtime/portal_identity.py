"""Short-lived gateway identity assertions, bound to method and exact request target."""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from application.context import ExecutionContext

ROOT = Path(__file__).resolve().parents[2]
SUBJECT = "x-portal-subject"
ISSUED = "x-portal-issued-at"
SIGNATURE = "x-portal-signature"


def private_key(root, filename):
    original = Path(root) / filename
    path = original.resolve()
    if not path.is_relative_to(Path(root).resolve()) or original.is_symlink():
        raise ValueError("gateway_key_path")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("gateway_key_permissions")
    value = path.read_text().strip()
    if len(value) < 32:
        raise ValueError("gateway_key_missing")
    return value


class PortalDirectory:
    def __init__(self, root, manifest, key_file, *, clock=time.time):
        root = Path(root).resolve()
        original = root / manifest
        path = original.resolve()
        if not path.is_relative_to(root) or original.is_symlink():
            raise ValueError("gateway_manifest_path")
        body = json.loads(path.read_text())
        if set(body) != {"schema_version", "principals"} or body["schema_version"] != 1:
            raise ValueError("gateway_manifest_schema")
        principals = body["principals"]
        if not isinstance(principals, dict) or not 1 <= len(principals) <= 100:
            raise ValueError("gateway_principals")
        owners = set()
        for username, principal in principals.items():
            ExecutionContext(username, "gateway-check")
            if set(principal) != {"owner_id", "role"} or principal["role"] not in {"user", "admin"}:
                raise ValueError("gateway_role")
            ExecutionContext(principal["owner_id"], "gateway-check")
            if principal["owner_id"] in owners:
                raise ValueError("gateway_owner_alias_not_allowed")
            owners.add(principal["owner_id"])
        self.principals, self.key, self.clock = principals, private_key(root, key_file), clock

    def principal(self, username):
        value = self.principals.get(username)
        if value is None:
            raise ValueError("gateway_subject_not_registered")
        return {"username": username, **value}

    def sign(self, username, method, target, issued=None):
        principal = self.principal(username)
        issued = str(int(self.clock()) if issued is None else issued)
        body = json.dumps(
            [1, username, principal["owner_id"], principal["role"], issued, method.upper(), target],
            separators=(",", ":"),
        )
        signature = hmac.new(self.key.encode(), body.encode(), hashlib.sha256).hexdigest()
        return {
            SUBJECT: username,
            ISSUED: issued,
            SIGNATURE: signature,
            "X-Authenticated-User": principal["owner_id"],
        }

    def verify(self, headers, method, target):
        username, issued = headers.get(SUBJECT), headers.get(ISSUED)
        if not isinstance(issued, str) or not issued.isdecimal() or len(issued) > 12:
            raise ValueError("gateway_assertion_missing")
        if abs(self.clock() - int(issued)) > 30:
            raise ValueError("gateway_assertion_expired")
        expected = self.sign(username, method, target, issued)
        signature = headers.get(SIGNATURE, "")
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, expected[SIGNATURE]
        ):
            raise ValueError("gateway_signature_invalid")
        if headers.get("x-authenticated-user") != expected["X-Authenticated-User"]:
            raise ValueError("gateway_owner_mismatch")
        return self.principal(username)


def request_principal(request, manifest, key_file):
    if request is None:
        raise ValueError("gateway_request_required")
    for header in [SUBJECT, ISSUED, SIGNATURE, "x-authenticated-user"]:
        if len(request.headers.getlist(header)) != 1:
            raise ValueError("gateway_assertion_header_count")
    scope = request.scope
    target = scope.get("raw_path", scope["path"].encode()).decode("ascii")
    if scope.get("query_string"):
        target += "?" + scope["query_string"].decode("ascii")
    return PortalDirectory(ROOT, manifest, key_file).verify(request.headers, request.method, target)
