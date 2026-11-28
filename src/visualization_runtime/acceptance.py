"""Frozen acceptance inputs. Expectations stay outside the reviewer prompt."""

import json
import math
import re
import shlex
import subprocess
from contextlib import closing
from pathlib import PurePosixPath
from types import SimpleNamespace

from visualization_runtime.sandbox import DockerSandbox, RenderError
from visualization_runtime.snapshots import digest
from visualization_runtime.store import canonical


def prior_campaign_usage(root, run_id, plan, *, current_run):
    """Count the full ancestry, not just the immediately preceding attempt."""
    from visualization_runtime.paths import confined_path
    from visualization_runtime.release import readonly

    totals = [0, 0]
    seen = {current_run}
    while run_id:
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id)
            or run_id in seen
        ):
            raise ValueError("invalid_prior_acceptance_chain")
        seen.add(run_id)
        folder = confined_path(root, root / ".tmp/visualization-p6" / run_id)
        if json.loads((folder / "frozen-plan.json").read_text(encoding="utf-8")) != plan:
            raise ValueError("acceptance_frozen_plan_changed")
        with closing(readonly(confined_path(root, folder / "tasks.sqlite"))) as db:
            if (
                db.execute(
                    "SELECT 1 FROM visualization_jobs WHERE state NOT IN ('succeeded','failed','cancelled') LIMIT 1"
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM visualization_provider_calls WHERE state IN ('reserved','sending','unknown') LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("prior_acceptance_not_finished")
            usage = db.execute(
                "SELECT count(*),COALESCE(sum(charged_tokens),0) FROM visualization_provider_calls WHERE call_key!='fixture-seed' AND state!='aborted'"
            ).fetchone()
            totals = [totals[i] + usage[i] for i in range(2)]
        run_id = json.loads((folder / "report.json").read_text(encoding="utf-8")).get("prior_run")
    return tuple(totals)


class SyntheticArtifacts:
    """In-memory test repository; synthetic input never impersonates a business tool."""

    def __init__(self):
        self.rows = {}

    def add(self, context, name, payload):
        source_id = "p6-fixture-" + name
        result = SimpleNamespace(operation="p6_synthetic_fixture", model_dump=lambda **_: payload)
        self.rows[source_id] = SimpleNamespace(
            owner_user_id=context.user_id,
            thread_id=context.thread_id,
            run_id="run_p6",
            expires_at=None,
            analysis_result=result,
            payload_sha256=digest(payload),
        )
        return {
            "kind": "analysis_artifact",
            "source_id": source_id,
            "run_id": "run_p6",
            "payload_sha256": digest(payload),
        }

    async def get_for_owner(self, source_id, *, owner_user_id):
        record = self.rows.get(source_id)
        return record if record and record.owner_user_id == owner_user_id else None


def synthetic(empty=False):
    return {
        "status": "ok",
        "synthetic": True,
        "units": {"t_s": "s", "temperature": "°C", "pressure": "kPa", "vibration": "m/s²"},
        "data": {
            "points": []
            if empty
            else [
                {
                    "t_s": i,
                    "temperature": None if 25 <= i < 31 else round(20 + math.sin(i / 9), 3),
                    "pressure": round(101 + math.cos(i / 12), 3),
                    "vibration": round(abs(math.sin(i / 6)), 3),
                }
                for i in range(80)
            ],
            "categories": []
            if empty
            else [
                {"label": f"阶段{i}：模拟无人机复杂环境下的长中文标签读数", "value": i * 3}
                for i in range(1, 7)
            ],
            "errors": []
            if empty
            else [
                {"label": f"模拟组{i}", "low": i - 0.3, "mean": i, "high": i + 0.4}
                for i in range(1, 6)
            ],
        },
    }


def defect_source(kind):
    axis = (
        "{type:'value',min:19,max:22,name:'温度°C'}"
        if kind == "scale"
        else "{type:'value',name:'温度°F'}"
    )
    return (
        "export async function render({echarts,container,data}) { const p=data.datasets[0].payload.data.points; echarts.init(container).setOption({backgroundColor:'#fff',title:{text:'模拟温度'},grid:{left:65,right:25,top:75,bottom:65,containLabel:true},xAxis:{type:'category',name:'t(s)',data:p.map(x=>x.t_s)},yAxis:"
        + axis
        + ",series:[{type:'bar',data:p.map(x=>x.temperature)}]}); }"
    )


class RemoteSandbox:
    """Operational SSH transport; only source/data cross to the isolated renderer."""

    def __init__(self, image, host, root, python, *, timeout=30):
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
            raise ValueError("invalid_acceptance_host")
        path = PurePosixPath(root)
        if not path.is_absolute() or ".." in path.parts or "/.tmp/visualization-p6/" not in root:
            raise ValueError("isolated_p6_workspace_required")
        self.image, self.host, self.root, self.python, self.timeout = (
            image,
            host,
            root,
            python,
            timeout,
        )

    def command(self, args, payload=None):
        cmd = (
            "cd "
            + shlex.quote(self.root)
            + " && "
            + shlex.join([self.python, "scripts/render_visualization_once.py", *args])
        )
        try:
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.host, cmd],
                input=payload,
                capture_output=True,
                timeout=self.timeout + 60,
                check=False,
            )
            value = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            raise RenderError("acceptance_transport_unavailable") from None
        if result.returncode:
            raise RenderError(
                value.get("error", "acceptance_transport_unavailable"),
                detail=value.get("diagnostic"),
            )
        return value

    def render(self, name, directory, request):
        relative = "var/render-smoke/" + name
        result = self.command(
            [
                "--image",
                self.image,
                "--name",
                name,
                "--directory",
                relative,
                "--timeout",
                str(self.timeout),
            ],
            canonical(request).encode(),
        )
        output = directory / "output"
        output.mkdir(parents=True)
        for item in result["images"]:
            viewport = item["viewport"]
            if viewport not in {"desktop", "mobile"}:
                raise RenderError("invalid_render_viewport")
            path = self.root + "/" + relative + "/output/" + viewport + ".png"
            result_copy = subprocess.run(
                [
                    "scp",
                    "-q",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=8",
                    self.host + ":" + path,
                    str(output / (viewport + ".png")),
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result_copy.returncode:
                raise RenderError("acceptance_transport_unavailable")
        return result

    def stopped(self, name):
        DockerSandbox._name(name)
        try:
            return (
                self.command(
                    [
                        "--image",
                        self.image,
                        "--name",
                        name,
                        "--directory",
                        "var/render-smoke/" + name,
                        "--cleanup-only",
                    ]
                ).get("stopped")
                is True
            )
        except RenderError:
            return False

    def remove(self, name):
        if not self.stopped(name):
            raise RenderError("sandbox_cleanup_unverified")
