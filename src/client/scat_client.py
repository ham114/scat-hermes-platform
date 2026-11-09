"""Typed synchronous client for the deterministic SCAT REST API."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Literal, Self, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from scat.schemas.api import (
    BatchOverview,
    BatchPage,
    FlightOverview,
    FlightPage,
    FplOverview,
    PredictionSnapshot,
    PredictionSnapshotPage,
    TrajectoryPage,
)

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class ScatClientError(Exception):
    """Raised when the SCAT API cannot return a valid typed response."""


def _format_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"

    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return f"HTTP {response.status_code}"

    message = str(error.get("message") or f"HTTP {response.status_code}")
    request_id = error.get("request_id")
    return f"{message} (request_id={request_id})" if request_id else message


class ScatClient:
    """Client used by fixed pages and scripts that do not require an LLM."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        timeout: float = 30.0,
        auth_secret: str | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        secret = auth_secret if auth_secret is not None else os.getenv("AUTH_SECRET")
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(
        self,
        path: str,
        response_model: type[ResponseModel],
        *,
        params: dict[str, Any] | None = None,
    ) -> ResponseModel:
        try:
            clean_params = (
                {key: value for key, value in params.items() if value is not None}
                if params is not None
                else None
            )
            response = self._client.get(path, params=clean_params)
            response.raise_for_status()
            return response_model.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            raise ScatClientError(_format_error(exc.response)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ScatClientError(str(exc)) from exc

    @staticmethod
    def _flight_path(batch: str, external_id: str) -> str:
        return f"/api/v1/scat/flights/{quote(batch, safe='')}/{quote(external_id, safe='')}"

    def list_batches(self, *, limit: int = 50, offset: int = 0) -> BatchPage:
        return self._get(
            "/api/v1/scat/batches",
            BatchPage,
            params={"limit": limit, "offset": offset},
        )

    def get_batch_overview(
        self,
        batch: str,
        *,
        distribution_limit: int = 10,
        time_granularity: Literal["day", "hour"] = "day",
    ) -> BatchOverview:
        path = f"/api/v1/scat/batches/{quote(batch, safe='')}/overview"
        return self._get(
            path,
            BatchOverview,
            params={
                "distribution_limit": distribution_limit,
                "time_granularity": time_granularity,
            },
        )

    def search_flights(
        self,
        *,
        batch: str | None = None,
        callsign: str | None = None,
        aircraft_type: str | None = None,
        adep: str | None = None,
        ades: str | None = None,
        candidate_key: str | None = None,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        sort_by: Literal["iobt", "callsign", "external_id"] = "iobt",
        sort_order: Literal["asc", "desc"] = "asc",
        limit: int = 20,
        offset: int = 0,
    ) -> FlightPage:
        start_time_value = (
            start_time.isoformat() if isinstance(start_time, datetime) else start_time
        )
        end_time_value = end_time.isoformat() if isinstance(end_time, datetime) else end_time
        params = {
            "batch": batch,
            "callsign": callsign,
            "aircraft_type": aircraft_type,
            "adep": adep,
            "ades": ades,
            "candidate_key": candidate_key,
            "start_time": start_time_value,
            "end_time": end_time_value,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
            "offset": offset,
        }
        return self._get(
            "/api/v1/scat/flights",
            FlightPage,
            params={key: value for key, value in params.items() if value is not None},
        )

    def get_flight(self, batch: str, external_id: str) -> FlightOverview:
        return self._get(self._flight_path(batch, external_id), FlightOverview)

    def get_trajectory(
        self,
        batch: str,
        external_id: str,
        *,
        start: int = 0,
        limit: int = 100,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        every_nth: int = 1,
        fields: tuple[str, ...] = ("position", "motion", "mode_s", "quality"),
    ) -> TrajectoryPage:
        start_time_value = (
            start_time.isoformat() if isinstance(start_time, datetime) else start_time
        )
        end_time_value = end_time.isoformat() if isinstance(end_time, datetime) else end_time
        return self._get(
            f"{self._flight_path(batch, external_id)}/trajectory",
            TrajectoryPage,
            params={
                "start": start,
                "limit": limit,
                "start_time": start_time_value,
                "end_time": end_time_value,
                "every_nth": every_nth,
                "fields": ",".join(fields),
            },
        )

    def get_fpl(self, batch: str, external_id: str) -> FplOverview:
        return self._get(f"{self._flight_path(batch, external_id)}/fpl", FplOverview)

    def list_predictions(
        self,
        batch: str,
        external_id: str,
        *,
        start: int = 0,
        limit: int = 20,
    ) -> PredictionSnapshotPage:
        return self._get(
            f"{self._flight_path(batch, external_id)}/predictions",
            PredictionSnapshotPage,
            params={"start": start, "limit": limit},
        )

    def get_prediction_snapshot(
        self,
        batch: str,
        external_id: str,
        snapshot_time: datetime | str,
    ) -> PredictionSnapshot:
        value = snapshot_time.isoformat() if isinstance(snapshot_time, datetime) else snapshot_time
        path = (
            f"{self._flight_path(batch, external_id)}/prediction-snapshots/{quote(value, safe='')}"
        )
        return self._get(path, PredictionSnapshot)
