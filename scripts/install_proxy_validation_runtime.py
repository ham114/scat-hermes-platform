"""Bake and attest the QA relay fence in the actual container, preserving rollback."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261108-release-readiness"
NAME = "scat-memory-model-proxy-1"
FILES = [
    "scripts/run_autonomy_proxy.py",
    "src/hermes_runtime/daily_budget.py",
    "src/hermes_runtime/validation_proxy.py",
    "src/hermes_runtime/validation_campaign.py",
]


def run(args, timeout=45):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def counts():
    with sqlite3.connect(
        "file:" + str(ROOT / "var/autonomy/budget/state.sqlite") + "?mode=ro", uri=True
    ) as db:
        return db.execute(
            "SELECT count(*),coalesce(sum(reserved),0),coalesce(sum(state='reserved'),0) FROM model_calls"
        ).fetchone()


def main():
    os.umask(0o077)
    report = OUT / "proxy-campaign-runtime.json"
    assert not report.exists()
    before = counts()
    assert before == (129, 6940664, 0), "review_unaccounted_relay_calls"
    info = json.loads(run(["docker", "inspect", NAME]))[0]
    old_image = info["Image"]
    attempt = len(list((ROOT / "var/deploy/validation-campaign").glob("proxy-image-*"))) + 1
    context = ROOT / f"var/deploy/validation-campaign/proxy-image-{attempt}"
    context.mkdir(exist_ok=False)
    for file in FILES:
        target = context / file
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / file, target)
    hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in FILES}
    base_tag = "scat-proxy-readiness-base:" + old_image.split(":")[1][:12]
    run(["docker", "tag", old_image, base_tag])
    dockerfile = "FROM " + base_tag + "\n"
    for file in FILES:
        dockerfile += f"COPY --chown=1000:1000 {file} {ROOT / file}\n"
    (context / "Dockerfile").write_text(dockerfile)
    tag = (
        "scat-memory-model-proxy:readiness-"
        + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:12]
    )
    with (OUT / f"proxy-runtime-build-{attempt}.log").open("x") as log:
        subprocess.run(
            ["docker", "build", "--network=none", "--pull=false", "-t", tag, str(context)],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=600,
        )
    image = run(["docker", "image", "inspect", "--format={{.Id}}", tag]).strip()
    unit = Path("/home/h3c/.config/systemd/user/scat-memory-model-proxy.service").resolve()
    old = unit.read_text()
    assert old.count(old_image) == 1
    backup = OUT / "private/proxy-runtime-before"
    backup.mkdir(exist_ok=False)
    (backup / "unit.service").write_text(old)
    (backup / "image.txt").write_text(old_image + "\n")
    extra = "--env=SCAT_VALIDATION_CAMPAIGN_FILE=var/deploy/validation-campaign/active.json --env=SCAT_VALIDATION_PROXY_SCOPE_FILE=var/deploy/validation-campaign/proxy-scope.json "
    for path, readonly in [
        ("var/deploy/validation-campaign", True),
        ("var/autonomy/validation", False),
    ]:
        absolute = str(ROOT / path)
        extra += (
            "--mount=type=bind,src="
            + absolute
            + ",dst="
            + absolute
            + (",readonly" if readonly else "")
            + " "
        )
    new = old.replace(old_image, image).replace("--workdir=", extra + "--workdir=", 1)
    assert counts() == before
    try:
        unit.write_text(new)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "restart", "scat-memory-model-proxy.service"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(60):
            try:
                with opener.open("http://127.0.0.1:18142/health", timeout=1) as response:
                    assert json.load(response)["status"] == "ready"
                break
            except OSError:
                if attempt == 59:
                    raise
                time.sleep(0.25)
        code = (
            "import sys,json,hashlib,pathlib;sys.path.insert(0,'src');from hermes_runtime.validation_proxy import validation_reserver;from hermes_runtime.validation_campaign import ValidationCampaign; r=pathlib.Path.cwd(); d=json.loads((r/'var/deploy/validation-campaign/proxy-scope.json').read_text()); c=ValidationCampaign.for_scope(d['scope']);assert callable(validation_reserver(r));assert c.policy['id']==d['campaign_id'];print(json.dumps({p:hashlib.sha256((r/p).read_bytes()).hexdigest() for p in "
            + repr(FILES)
            + "}))"
        )
        actual = json.loads(
            run(["docker", "exec", NAME, "/opt/conda/envs/scat-agent/bin/python", "-B", "-c", code])
        )
        assert actual == hashes
        live = json.loads(run(["docker", "inspect", NAME]))[0]
        assert live["Image"] == image
        assert counts() == before, "install_must_not_call_model"
    except Exception:
        unit.write_text(old)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "restart", "scat-memory-model-proxy.service"])
        raise
    result = {
        "status": "runtime_attested",
        "supersedes": "proxy-campaign-install.json health-only assertion",
        "before_counts": before,
        "after_counts": counts(),
        "image": image,
        "previous_image": old_image,
        "source_sha256": actual,
        "new_model_calls": 0,
        "server_rebooted": False,
    }
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
