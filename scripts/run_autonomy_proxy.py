"""Private persistent-budget model relay for memory, author and synthetic trials."""

import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def create_app():
    import httpx
    from dotenv import dotenv_values
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    from hermes_runtime.autonomy_config import private_text
    from hermes_runtime.daily_budget import DailyBudget
    from hermes_runtime.skills import checked_path
    from hermes_runtime.validation_proxy import validation_reserver

    directory = ROOT / "var/deploy/autonomy"
    policy = json.loads((directory / "proxy.json").read_text())
    campaign_path = os.environ.get("SCAT_MODEL_CAMPAIGN_FILE")
    campaign = json.loads(checked_path(ROOT, campaign_path).read_text()) if campaign_path else None
    budget = DailyBudget(
        ROOT / "var/autonomy/budget/state.sqlite",
        campaign=campaign,
        validation_reserve=validation_reserver(ROOT),
        **policy,
    )
    access = {
        role: private_text(ROOT, "var/deploy/autonomy/secrets/" + role + ".key")
        for role in ("memory", "author", "trial")
    }
    key = dotenv_values(ROOT / "var/deploy/honcho/secrets/honcho.env")["LLM_OPENAI_API_KEY"]
    app = FastAPI()

    @app.get("/health")
    def health():
        return {
            "status": "ready",
            "daily_requests": budget.daily_requests,
            "daily_conservative_units": budget.daily_units,
        }

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        role = next(
            (
                r
                for r, k in access.items()
                if secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + k)
            ),
            None,
        )
        if role is None:
            return JSONResponse({"error": {"message": "unauthorized"}}, 401)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1000000:
                return JSONResponse({"error": {"message": "bounded_request_required"}}, 413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
                raise TypeError("invalid_request")
            maximum = body.pop("max_completion_tokens", body.get("max_tokens", 4096))
            if type(maximum) is not int or maximum < 1:
                raise ValueError("invalid_output_bound")
            body["max_tokens"] = min(maximum, 8192)
            body["model"], body["thinking"] = "deepseek-flash", {"type": "disabled"}
            canonical = json.dumps(body, ensure_ascii=False).encode()
            identity = budget.reserve(role, canonical, body["max_tokens"])
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": {"message": "invalid_request_or_budget_exhausted"}}, 429)
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120, connect=10))
        try:
            upstream = await client.send(
                client.build_request(
                    "POST",
                    "https://api.deepseek.com/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer " + key},
                ),
                stream=True,
            )
        except httpx.HTTPError:
            budget.finish(identity, "unknown")
            await client.aclose()
            return JSONResponse({"error": {"message": "upstream_outcome_unknown"}}, 503)
        if upstream.status_code != 200:
            budget.finish(identity, "http_" + str(upstream.status_code))
            await upstream.aclose()
            await client.aclose()
            return JSONResponse(
                {"error": {"message": "upstream_request_failed"}}, upstream.status_code
            )
        if not body.get("stream"):
            try:
                data = json.loads(await upstream.aread())
                budget.finish(identity, "ok", data.get("usage"))
                return JSONResponse(data)
            except (ValueError, httpx.HTTPError):
                budget.finish(identity, "unknown")
                return JSONResponse({"error": {"message": "upstream_outcome_unknown"}}, 503)
            finally:
                await upstream.aclose()
                await client.aclose()

        async def chunks():
            done, usage = False, {}
            try:
                async for line in upstream.aiter_lines():
                    if line == "data: [DONE]":
                        done = True
                    elif line.startswith("data: "):
                        try:
                            usage = json.loads(line[6:]).get("usage") or usage
                        except ValueError:
                            pass
                    yield (line + "\n").encode()
            finally:
                budget.finish(identity, "ok" if done else "unknown", usage)
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("existing_Anaconda_required")
    os.umask(0o077)
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=18142, access_log=False)
