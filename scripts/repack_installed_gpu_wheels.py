"""Internal reconstruction from the frozen old image, never labelled vendor-original wheels.

Executed inside the existing image with /build mounted. Preserves METADATA, WHEEL,
licenses and package bytes; rebuilds RECORD and console entry points on install.
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from importlib import metadata
from pathlib import Path

BUILD = Path("/build")
IMAGE = "sha256:0bbae03be3d2c1340350d7eca6c9168b8735be57adfb61883fda8bad914d7bf4"


def main():
    assert sys.version_info[:2] == (3, 11)
    wheels = BUILD / "wheels"
    wheels.mkdir(exist_ok=True)
    progress = BUILD / "wheels-progress.json"
    records = json.loads(progress.read_text()) if progress.exists() else []
    prior = {r["name"].lower(): r for r in records}
    builder_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        version = dist.version
        if name.lower() in prior:
            known = prior[name.lower()]
            assert known["version"] == version
            h = hashlib.sha256()
            with (wheels / known["file"]).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            assert h.hexdigest() == known["sha256"], "frozen_wheel_changed"
            continue
        normalized = re.sub(r"[-_.]+", "_", name)
        wheel = dist.read_text("WHEEL")
        reconstructed_wheel_metadata = wheel is None
        if reconstructed_wheel_metadata:
            assert name.lower() == "setuptools", name
            assert all(Path(str(p)).suffix not in {".so", ".pyd"} for p in dist.files)
            wheel = "Wheel-Version: 1.0\nGenerator: scat-frozen-reconstruction\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        tags = [line[5:].strip() for line in wheel.splitlines() if line.startswith("Tag: ")]
        assert tags
        tag = tags[0]
        assert re.fullmatch(r"[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+", tag)
        filename = normalized + "-" + version + "-1scat-" + tag + ".whl"
        target = wheels / filename
        base = Path(dist.locate_file("")).resolve()
        info_paths = sorted(
            {
                str(p).split("/")[0]
                for p in dist.files
                if len(p.parts) == 2 and str(p).endswith(".dist-info/METADATA")
            }
        )
        assert len(info_paths) == 1, name
        info = info_paths[0]
        rows = []
        skipped = []
        entries = {
            e.name for e in dist.entry_points if e.group in ["console_scripts", "gui_scripts"]
        }
        with zipfile.ZipFile(
            target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
        ) as z:

            def add(relative, path=None, data=None, rows=rows):
                zi = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o644 if path is None else stat.S_IMODE(path.stat().st_mode) & 0o777
                zi.external_attr = (stat.S_IFREG | mode) << 16
                h = hashlib.sha256()
                size = 0
                with z.open(zi, "w", force_zip64=True) as out:
                    if data is not None:
                        out.write(data)
                        h.update(data)
                        size = len(data)
                    else:
                        with path.open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                out.write(chunk)
                                h.update(chunk)
                                size += len(chunk)
                rows.append(
                    (
                        relative,
                        "sha256=" + base64.urlsafe_b64encode(h.digest()).decode().rstrip("="),
                        size,
                    )
                )

            for member in sorted(set(dist.files), key=str):
                relative = Path(str(member))
                source = Path(dist.locate_file(member)).resolve()
                if reconstructed_wheel_metadata and relative == Path(info) / "WHEEL":
                    continue
                if (
                    relative.suffix == ".pyc"
                    or relative.parent == Path(info)
                    and relative.name
                    in [
                        "RECORD",
                        "RECORD.jws",
                        "RECORD.p7s",
                        "INSTALLER",
                        "REQUESTED",
                        "direct_url.json",
                    ]
                ):
                    skipped.append(str(member))
                    continue
                if not source.is_relative_to(base):
                    assert source.is_relative_to(Path(sys.prefix) / "bin"), (name, str(member))
                    if source.name in entries:
                        skipped.append(str(member))
                        continue
                    relative = Path(normalized + "-" + version + ".data/scripts") / source.name
                else:
                    relative = source.relative_to(base)
                assert source.is_file() and ".." not in relative.parts
                add(relative.as_posix(), path=source)
            if reconstructed_wheel_metadata:
                add(info + "/WHEEL", data=wheel.encode())
            provenance = {
                "kind": "internal_reconstruction_not_vendor_original",
                "source_image": IMAGE,
                "name": name,
                "version": version,
                "source_record_sha256": hashlib.sha256(
                    (dist.read_text("RECORD") or "").encode()
                ).hexdigest(),
                "skipped_generated_or_install_metadata": skipped,
                "wheel_metadata_reconstructed_for_pure_python_setuptools": reconstructed_wheel_metadata,
                "duplicate_source_record_entries": len(dist.files) - len(set(dist.files)),
            }
            add(
                info + "/scat-reconstruction.json",
                data=json.dumps(provenance, sort_keys=True).encode(),
            )
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerows(rows)
            writer.writerow([info + "/RECORD", "", ""])
            record_info = zipfile.ZipInfo(info + "/RECORD", date_time=(2020, 1, 1, 0, 0, 0))
            z.writestr(record_info, buffer.getvalue())
        h = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        records.append(
            {
                "name": name,
                "version": version,
                "file": filename,
                "sha256": h.hexdigest(),
                "bytes": target.stat().st_size,
                "files": len(rows),
                "source_image": IMAGE,
                "provenance": "internal_reconstruction_not_vendor_original",
                "builder_sha256": builder_sha,
            }
        )
        pending = progress.with_suffix(".pending")
        pending.write_text(json.dumps(records, indent=2) + "\n")
        pending.replace(progress)
        print(
            json.dumps({"package": name, "version": version, "bytes": target.stat().st_size}),
            flush=True,
        )
    (BUILD / "pip-hashed.txt").write_text(
        "\n".join(
            r["name"] + "==" + r["version"] + " --hash=sha256:" + r["sha256"] for r in records
        )
        + "\n"
    )
    (BUILD / "wheels.lock.json").write_text(
        json.dumps({"source_image": IMAGE, "python": sys.version, "wheels": records}, indent=2)
        + "\n"
    )


if __name__ == "__main__":
    os.umask(0o077)
    main()
