"""
High‑level wrapper used by the epoch‑validator to ingest votes,
deduplicate them, and persist snapshots.
"""

from __future__ import annotations

import logging
from typing import List

from api.client import VoteAPIClient
from api.schemas import Vote
from storage.models import VoteSnapshot
from validator.state_cache import StateCache

log = logging.getLogger("validator.vote_fetcher")


class VoteFetcher:
    """
    Stateless helper – create once, call `fetch_and_store()` each epoch.
    """

    def __init__(
        self,
        cache: StateCache,
        client: VoteAPIClient | None = None,
    ):
        self.cache = cache
        self.client = client or VoteAPIClient()

    # ────────────────────────────────────────────────────────
    # Main entry‑point
    # ────────────────────────────────────────────────────────
    def fetch_and_store(self) -> List[VoteSnapshot]:
        """
        1. Fetch latest votes from REST API
        2. Filter out votes already cached
        3. Persist only new ones
        4. Return the persisted VoteSnapshot objects
        """
        fresh_votes: List[Vote] = self.client.get_latest_votes()
        new_snapshots: List[VoteSnapshot] = []

        for v in fresh_votes:
            if self.cache.votes_changed(v.block_height, v.voter_hotkey):
                new_snapshots.append(
                    VoteSnapshot(
                        block_height=v.block_height,
                        voter_hotkey=v.voter_hotkey,
                        weights=v.weights,
                    )
                )

        if new_snapshots:
            self.cache.persist_votes(new_snapshots)

        log.info("VoteFetcher stored %d new snapshots", len(new_snapshots))
        return new_snapshots
