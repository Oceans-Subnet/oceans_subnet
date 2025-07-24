"""
Typed synchronous client for the Oceans vote API.
"""

from __future__ import annotations

import logging
from typing import List

import backoff
import httpx

from config import settings
from .schemas import Vote

log = logging.getLogger("vote_api_client")


class VoteAPIClient:
    """
    Minimal wrapper around httpx.Client with automatic retries.
    """

    DEFAULT_TIMEOUT = 10.0

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url: str = str(base_url or settings.VOTE_API_ENDPOINT).rstrip("/")
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    # ────────────────────────────────────────────────────────
    # Public endpoints
    # ────────────────────────────────────────────────────────
    @backoff.on_exception(
        backoff.expo, httpx.HTTPError, max_tries=5, jitter=None, factor=2
    )
    def get_latest_votes(self) -> List[Vote]:
        """
        Fetch the most recent vote‑vector per voter.
        """
        response = self._client.get("/votes/latest")
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Expected JSON list from /votes/latest")

        votes = [Vote(**item) for item in data]
        log.debug("Fetched %d votes from API", len(votes))
        return votes

    # ────────────────────────────────────────────────────────
    # Context manager helpers
    # ────────────────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VoteAPIClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # noqa: D401
        self.close()
