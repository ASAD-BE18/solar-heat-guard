"""Thin client for the FortyGuard Temperature API (tOS Enterprise).

Confirmed against the live API on 2026-08-23 using the project's own key:
POST /v1/heatmap -> {data: {activity_id}}, GET /v1/status/{activity_id} -> status
(Processing | Completed), with result.map_data / result.stats_data once Completed.

The account is capped at 30 heatmap requests/day and that quota is shared with
the rest of the team, so this client is built around not wasting it:

  - completed results are cached on disk, keyed by a hash of the request
    payload, so re-running the downstream pipeline (row geometry, percentile,
    visualization) costs nothing;
  - 429 and 5xx responses are retried with exponential backoff and honour a
    Retry-After header, rather than surfacing as a bare HTTPError after the
    request has already been spent;
  - a 429 that survives the retries raises FortyGuardQuotaError so callers can
    tell "we are rate limited" apart from "the request was malformed".
"""
import hashlib
import json
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = ROOT / "data" / "fortyguard" / "_response_cache"

RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 2.0


class FortyGuardError(RuntimeError):
    pass


class FortyGuardQuotaError(FortyGuardError):
    """Rate limited / quota exhausted (HTTP 429) after exhausting retries."""


def _require_env(name, hint):
    value = os.environ.get(name)
    if not value:
        raise FortyGuardError(f"{name} is not set -- {hint}")
    return value


class FortyGuardClient:
    def __init__(self, api_key=None, api_base=None, cache_dir=DEFAULT_CACHE_DIR, use_cache=True):
        self.api_key = api_key or _require_env(
            "FORTYGUARD_API_KEY", "copy .env.example to .env and fill it in")
        self.api_base = (api_base or _require_env(
            "FORTYGUARD_API_BASE", "set it in .env (e.g. https://api.fortyguard.com)")).rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.use_cache = use_cache and self.cache_dir is not None

    def _headers(self):
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    # --- result cache -----------------------------------------------------
    # Keyed on endpoint + payload only. The API key is deliberately NOT part
    # of the key material and never written to disk; cached payloads are
    # request parameters and public temperature data, no credentials.

    def _cache_path(self, endpoint, payload):
        digest = hashlib.sha256(
            json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True).encode()
        ).hexdigest()[:32]
        return self.cache_dir / f"{endpoint}_{digest}.json"

    def cached_result(self, endpoint, payload):
        if not self.use_cache:
            return None
        path = self._cache_path(endpoint, payload)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            # A truncated/corrupt cache entry must never take down a run --
            # fall through and spend the request instead.
            return None

    def store_result(self, endpoint, payload, result):
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(endpoint, payload).write_text(json.dumps(result, indent=2))

    # --- transport --------------------------------------------------------

    def _request(self, method, url, **kwargs):
        """Issue a request, retrying 429/5xx with exponential backoff."""
        last_response = None
        for attempt in range(MAX_ATTEMPTS):
            resp = requests.request(method, url, headers=self._headers(), timeout=30, **kwargs)
            if resp.status_code not in RETRY_STATUS:
                resp.raise_for_status()
                return resp

            last_response = resp
            if attempt == MAX_ATTEMPTS - 1:
                break
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else BACKOFF_BASE_SECONDS * (2 ** attempt)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
            print(f"  {resp.status_code} from {url} -- retrying in {delay:.0f}s "
                  f"(attempt {attempt + 2}/{MAX_ATTEMPTS})")
            time.sleep(delay)

        if last_response.status_code == 429:
            raise FortyGuardQuotaError(
                f"rate limited by FortyGuard after {MAX_ATTEMPTS} attempts -- the account is "
                f"capped at 30 heatmap requests/day; wait for the quota to reset")
        last_response.raise_for_status()
        return last_response

    def _submit(self, endpoint, payload):
        resp = self._request("POST", f"{self.api_base}/v1/{endpoint}", json=payload)
        body = resp.json()
        if body.get("error"):
            raise FortyGuardError(body.get("message", f"{endpoint} submission failed"))
        return body["data"]["activity_id"]

    # --- endpoints --------------------------------------------------------

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
        return self._submit("heatmap", self.heatmap_payload(
            polygon_geojson, date_time, granularity, analytic_type, threshold, direction))

    @staticmethod
    def heatmap_payload(polygon_geojson, date_time, granularity,
                        analytic_type=None, threshold=None, direction=None):
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
        return payload

    def check_status(self, activity_id):
        resp = self._request("GET", f"{self.api_base}/v1/status/{activity_id}")
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

    def run(self, endpoint, payload, interval_seconds=5, timeout_seconds=300):
        """Submit, poll, and return the completed result -- served from the
        on-disk cache when the identical request has already been run, so
        re-running the pipeline does not spend quota."""
        cached = self.cached_result(endpoint, payload)
        if cached is not None:
            print(f"  cache hit for {endpoint} -- no API request spent "
                  f"({self._cache_path(endpoint, payload).name})")
            return cached
        activity_id = self._submit(endpoint, payload)
        print(f"  activity_id={activity_id}, polling...")
        result = self.poll_until_complete(activity_id, interval_seconds, timeout_seconds)
        self.store_result(endpoint, payload, result)
        return result
