"""Thin client for the FortyGuard Temperature API (tOS Enterprise).

Confirmed against the live API on 2026-08-23 using the project's own key:
POST /v1/heatmap -> {data: {activity_id}}, GET /v1/status/{activity_id} -> status
(Processing | Completed), with result.map_data / result.stats_data once Completed.
"""
import os
import time

import requests


class FortyGuardError(RuntimeError):
    pass


class FortyGuardClient:
    def __init__(self, api_key=None, api_base=None):
        self.api_key = api_key or os.environ["FORTYGUARD_API_KEY"]
        self.api_base = (api_base or os.environ["FORTYGUARD_API_BASE"]).rstrip("/")

    def _headers(self):
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def submit_heatmap(self, polygon_geojson, date_time, granularity,
                        analytic_type=None, threshold=None, direction=None):
        """Submit a heatmap job. Returns the activity_id.

        polygon_geojson: GeoJSON FeatureCollection (polygon_aoi).
        date_time: dict with start_date + filter_type (1=hour, 2=hour range,
            3=day, 4=day range), plus start_time/end_time/end_date as required.
        granularity: 60 | 80 | 100 (meters).
        analytic_type: tcm (default) | time_of_measure | exceedance | persistence.
        threshold / direction: only used by exceedance/persistence.
        """
        payload = {
            "polygon_aoi": polygon_geojson,
            "date_time": date_time,
            "granularity": granularity,
        }
        if analytic_type is not None:
            payload["analytic_type"] = analytic_type
        if threshold is not None:
            payload["threshold"] = threshold
        if direction is not None:
            payload["direction"] = direction

        resp = requests.post(f"{self.api_base}/v1/heatmap", headers=self._headers(),
                              json=payload, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise FortyGuardError(body.get("message", "heatmap submission failed"))
        return body["data"]["activity_id"]

    def check_status(self, activity_id):
        resp = requests.get(f"{self.api_base}/v1/status/{activity_id}",
                             headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()["data"]

    def poll_until_complete(self, activity_id, interval_seconds=5, timeout_seconds=300):
        deadline = time.monotonic() + timeout_seconds
        while True:
            data = self.check_status(activity_id)
            status = data.get("status")
            if status == "Completed":
                return data["result"]
            if status not in ("Processing", None):
                raise FortyGuardError(f"activity {activity_id} ended with status={status}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"activity {activity_id} still Processing after {timeout_seconds}s")
            time.sleep(interval_seconds)
