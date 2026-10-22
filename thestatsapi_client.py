"""
thestatsapi_client.py
---------------------
Cliente HTTP para TheStatsAPI (https://api.thestatsapi.com/llms.txt).

- Autenticación: Bearer token desde la variable de entorno THESTATSAPI_API_KEY
  (la key NUNCA se hardcodea).
- Rate limiting cliente: token bucket a 900 req/min (margen bajo el límite
  de 1000 req/min de la API).
- Reintentos: máximo 3, con backoff exponencial (1s, 2s, 4s) en errores de red.
- 429: respeta el header Retry-After de la API.
- Timeout por petición: 30 s.
- Caché en memoria de respuestas GET durante 5 minutos.
- Logging de cada llamada (endpoint, status, tiempo de respuesta).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger("thestatsapi")

BASE_URL = "https://api.thestatsapi.com/api"
RATE_LIMIT_PER_MINUTE = 900          # margen bajo el límite de 1000/min de la API
MAX_RETRIES = 3                      # reintentos tras un fallo
RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)   # backoff exponencial
REQUEST_TIMEOUT = 30                 # segundos
CACHE_TTL_SECONDS = 300              # caché en memoria: 5 minutos
PER_PAGE_MAX = 100                   # máximo permitido por la API


class TheStatsAPIError(Exception):
    """Error devuelto por la API o fallo de red tras agotar reintentos."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


class RateLimiter:
    """Token bucket: como máximo `per_minute` peticiones por minuto."""

    def __init__(self, per_minute: int = RATE_LIMIT_PER_MINUTE):
        self._refill_per_second = per_minute / 60.0
        self._max_tokens = float(per_minute)
        self._tokens = self._max_tokens
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._max_tokens,
                    self._tokens + (now - self._last) * self._refill_per_second,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._refill_per_second
                time.sleep(wait)


class TheStatsAPIClient:
    """Cliente sincrónico para TheStatsAPI con rate limit, retries y caché."""

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        key = api_key or os.environ.get("THESTATSAPI_API_KEY")
        if not key:
            raise RuntimeError(
                "No se encontró la API key. Define la variable de entorno "
                "THESTATSAPI_API_KEY antes de ejecutar el sistema."
            )
        self._session = session or requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {key}"})
        self._limiter = RateLimiter()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ caché
    def _cache_get(self, key: str) -> Any | None:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None and (time.monotonic() - entry[0]) < CACHE_TTL_SECONDS:
                return entry[1]
            return None

    def _cache_put(self, key: str, value: Any) -> None:
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), value)

    # ------------------------------------------------------- petición base
    def _get(self, path: str, params: dict | None = None, use_cache: bool = True) -> Any:
        query = urlencode(params or {}, doseq=True)
        url = f"{BASE_URL}{path}"
        full_url = f"{url}?{query}" if query else url

        if use_cache:
            cached = self._cache_get(full_url)
            if cached is not None:
                logger.info("CACHE HIT %s", full_url)
                return cached

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            self._limiter.acquire()
            start = time.monotonic()
            try:
                response = self._session.get(full_url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                elapsed = time.monotonic() - start
                logger.error("API %s -> ERROR DE RED (%.2fs): %s", full_url, elapsed, exc)
                last_error = exc
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue

            elapsed = time.monotonic() - start
            logger.info("API %s -> %s (%.2fs)", full_url, response.status_code, elapsed)

            if response.status_code == 200:
                data = response.json()
                if use_cache:
                    self._cache_put(full_url, data)
                return data

            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", 5))
                except (TypeError, ValueError):
                    retry_after = 5.0
                logger.warning(
                    "API 429 en %s (%s). Esperando %.1fs según Retry-After.",
                    full_url, response.text[:120], retry_after,
                )
                last_error = TheStatsAPIError("RATE_LIMITED", "Rate limit alcanzado", 429)
                time.sleep(retry_after)
                continue

            # Error definitivo de la API: envelope de error documentado
            try:
                err = response.json().get("error", {})
            except ValueError:
                err = {}
            raise TheStatsAPIError(
                err.get("code", "UNKNOWN"),
                err.get("message", response.text[:200]),
                response.status_code,
            )

        raise TheStatsAPIError(
            "NETWORK_ERROR",
            f"Fallo tras {MAX_RETRIES} reintentos: {last_error}",
            0,
        )

    # ------------------------------------------------------------ endpoints
    def get_matches(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        team_id: str | None = None,
        status: str | None = None,
        sort: str | None = None,
        per_page: int = PER_PAGE_MAX,
    ) -> list[dict]:
        """GET /football/matches — pagina automáticamente hasta meta.total_pages."""
        params: dict[str, Any] = {"per_page": per_page}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if team_id:
            params["team_id"] = team_id
        if status:
            params["status"] = status
        if sort:
            params["sort"] = sort

        items: list[dict] = []
        page, total_pages = 1, 1
        while page <= total_pages:
            params["page"] = page
            payload = self._get("/football/matches", params)
            items.extend(payload.get("data", []))
            total_pages = payload.get("meta", {}).get("total_pages", 1)
            page += 1
        return items

    def get_match(self, match_id: str) -> dict:
        """GET /football/matches/{match_id}"""
        return self._get(f"/football/matches/{match_id}").get("data", {})

    def get_match_stats(self, match_id: str) -> dict:
        """GET /football/matches/{match_id}/stats (solo partidos jugados)."""
        return self._get(f"/football/matches/{match_id}/stats").get("data", {})

    def get_match_odds(self, match_id: str) -> dict:
        """GET /football/matches/{match_id}/odds"""
        return self._get(f"/football/matches/{match_id}/odds").get("data", {})

    def get_match_lineups(self, match_id: str) -> dict:
        """GET /football/matches/{match_id}/lineups"""
        return self._get(f"/football/matches/{match_id}/lineups").get("data", {})

    def get_team_players(self, team_id: str) -> list[dict]:
        """GET /football/teams/{team_id}/players — plantilla del equipo."""
        payload = self._get(f"/football/teams/{team_id}/players")
        if isinstance(payload, list):          # respuesta sin envelope
            return payload
        return payload.get("data", [])

    def get_competitions(self, per_page: int = PER_PAGE_MAX) -> list[dict]:
        """GET /football/competitions — pagina automáticamente."""
        items: list[dict] = []
        page, total_pages = 1, 1
        while page <= total_pages:
            payload = self._get("/football/competitions", {"page": page, "per_page": per_page})
            items.extend(payload.get("data", []))
            total_pages = payload.get("meta", {}).get("total_pages", 1)
            page += 1
        return items

    def get_seasons(self, competition_id: str) -> list[dict]:
        """GET /football/competitions/{competition_id}/seasons"""
        return self._get(f"/football/competitions/{competition_id}/seasons").get("data", [])

    def get_standings(self, competition_id: str, season_id: str) -> list[dict]:
        """GET /football/competitions/{competition_id}/seasons/{season_id}/standings"""
        path = f"/football/competitions/{competition_id}/seasons/{season_id}/standings"
        return self._get(path).get("data", [])
