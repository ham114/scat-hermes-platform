"""Owner-scoped bounded display expansion, cached by immutable source receipt.

Only the first creation reads business data. Refresh reopens persisted receipts.
The LLM's analysis receipts stay separate from display-only pages.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
import weakref
from contextlib import contextmanager
from copy import deepcopy

MAX_POINTS = 2000
PAGE_SIZE = 100
_locks = weakref.WeakValueDictionary()


@contextmanager
def _connection(store):
    db = sqlite3.connect(store.path, timeout=5)
    db.execute(
        "CREATE TABLE IF NOT EXISTS trajectory_views (scope TEXT, run TEXT, seed TEXT, state TEXT, payload TEXT, expires REAL, lease REAL, PRIMARY KEY(scope,run,seed))"
    )
    try:
        with db:
            yield db
    finally:
        db.close()


def _claim(store, scope, run_id, seed):
    now = time.time()
    with _connection(store) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM trajectory_views WHERE expires<=?", (now,))
        row = db.execute(
            "SELECT state,payload,lease FROM trajectory_views WHERE scope=? AND run=? AND seed=?",
            (scope, run_id, seed["result_id"]),
        ).fetchone()
        if row and row[0] == "ready":
            return json.loads(row[1])
        if row and row[2] > now:
            raise ValueError("trajectory_view_busy")
        db.execute(
            "INSERT OR REPLACE INTO trajectory_views VALUES (?,?,?,?,?,?,?)",
            (scope, run_id, seed["result_id"], "building", "{}", seed["expires_at"], now + 120),
        )
    return None


def _save(store, scope, run_id, seed, result):
    with _connection(store) as db:
        db.execute(
            "UPDATE trajectory_views SET state=?,payload=?,lease=0 WHERE scope=? AND run=? AND seed=?",
            ("ready", json.dumps(result), scope, run_id, seed["result_id"]),
        )


async def trajectory_view(store, context, run_id, result_id, app):
    # Verification and expiry happen before looking up a cached view.
    seed = store.get(context.scope_key, run_id, result_id)
    if seed["operation"] != "get_scat_track_points":
        raise ValueError("not_a_trajectory_receipt")
    raw = seed["result"]
    if raw.get("display_only"):
        raise ValueError("original_analysis_source_required")
    if raw.get("representation") == "page_summary" or not isinstance(
        raw.get("data", {}).get("points"), list
    ):
        raise ValueError("trajectory_points_required")
    query = raw["query"]
    total = raw["data"]["pagination"]["total_points"]
    if type(total) is not int or total < 1 or total > 10_000_000:
        raise ValueError("invalid_trajectory_total")
    key = (str(store.path), context.scope_key, run_id, result_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _claim(store, context.scope_key, run_id, seed)
        if cached is not None:
            # Recheck each source; a cached id never bypasses scope/hash/expiry.
            for ref in cached["items"]:
                store.get(context.scope_key, run_id, ref["result_id"])
            return cached
        requested_step = max(1, math.ceil((total - 1) / (MAX_POINTS - 1)))
        segmented = requested_step > 20
        step = min(20, requested_step)
        # The business schema caps every_nth at 20. Larger sources use 20
        # bounded segments rather than violating that contract or making 2000 RPCs.
        segment_starts = [i * (total - 99 * 20) // 19 for i in range(20)] if segmented else []
        refs, selected = [], set()
        failure = None
        time_origin = None
        try:
            async with asyncio.timeout(24):
                start = 0
                for page_index in range(21):
                    if segmented:
                        start = segment_starts[page_index]
                    args = {
                        k: query[k]
                        for k in ["batch", "external_id", "start_time", "end_time"]
                        if k in query
                    }
                    args.update(
                        start=start,
                        every_nth=step,
                        limit=99 if segmented else PAGE_SIZE,
                        fields=["position"],
                    )
                    page = await app.invoke("get_scat_track_points", args, context=context)
                    data = page.get("data") or {}
                    pagination, points = data.get("pagination", {}), data.get("points")
                    if (
                        page.get("status") != "ok"
                        or page.get("representation") == "page_summary"
                        or not isinstance(points, list)
                        or pagination.get("total_points") != total
                        or pagination.get("start") != start
                    ):
                        raise ValueError("trajectory_page_unavailable_or_changed")
                    if len(points) > PAGE_SIZE or len(selected) + len(points) > MAX_POINTS:
                        raise ValueError("trajectory_view_budget")
                    if not points or any(
                        not isinstance(p.get("position"), dict)
                        or any(
                            type(p["position"].get(k)) not in (int, float)
                            or not math.isfinite(p["position"][k])
                            or abs(p["position"][k]) > bound
                            for k, bound in [("lon_deg", 180), ("lat_deg", 90)]
                        )
                        for p in points
                    ):
                        raise ValueError("invalid_trajectory_coordinates")
                    if start == 0:
                        from agents.analysis_results import build_scat_track_analysis

                        first_analysis = build_scat_track_analysis(page)
                        time_origin = first_analysis.start_time if first_analysis else None
                    display_page = deepcopy(page)
                    display_page["display_only"] = True
                    if time_origin:
                        display_page["display_time_origin"] = time_origin
                    ref = store.put(
                        context.scope_key,
                        run_id,
                        f"view-{result_id}-{start}-{step}",
                        "get_scat_track_points",
                        display_page,
                    )
                    refs.append(ref)
                    selected.update(start + i * step for i in range(len(points)))
                    if segmented:
                        if page_index == 19:
                            break
                        continue
                    if not pagination.get("has_more"):
                        break
                    next_start = pagination.get("next_start")
                    if type(next_start) is not int or next_start <= start:
                        raise ValueError("trajectory_pagination_stalled")
                    start = next_start
                else:
                    raise ValueError("trajectory_page_budget")
                if total - 1 not in selected:
                    args.update(start=total - 1, every_nth=1, limit=1)
                    last = await app.invoke("get_scat_track_points", args, context=context)
                    if (
                        last.get("status") != "ok"
                        or len(last.get("data", {}).get("points", [])) != 1
                        or last["data"]["pagination"]["total_points"] != total
                    ):
                        raise ValueError("trajectory_tail_unavailable")
                    last = deepcopy(last)
                    last["display_only"] = True
                    if time_origin:
                        last["display_time_origin"] = time_origin
                    refs.append(
                        store.put(
                            context.scope_key,
                            run_id,
                            f"view-{result_id}-tail",
                            "get_scat_track_points",
                            last,
                        )
                    )
                    selected.add(total - 1)
        except (ValueError, TimeoutError, OSError, sqlite3.Error) as exc:
            failure = type(exc).__name__
        result = {
            "items": refs,
            "coverage": {
                "total": total,
                "loaded": len(selected),
                "every_nth": step,
                "sampling_mode": "segmented" if segmented else "uniform",
                "complete": len(selected) == total and not failure,
                "full_extent": 0 in selected and total - 1 in selected and not failure,
                "partial": bool(failure),
                "analysis_scope": "original_tool_receipts",
                "display_scope": "bounded_trajectory_view",
            },
        }
        if failure:
            result["message"] = "部分页面未能读取，仅展示已保存范围；未补造缺失点。"
        _save(store, context.scope_key, run_id, seed, result)
        return result
