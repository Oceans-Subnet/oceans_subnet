"""
Validator‑side helper that pulls liquidity data from chain, converts it
into storage‑layer snapshots and persists only *new* records.

All amounts are denominated in **TAO**.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import bittensor as bt
from bittensor import AsyncSubtensor            # type: ignore
from bittensor.utils.balance import Balance
from bittensor.utils.balance import fixed_to_float
from sqlalchemy.orm import Session

from config import settings
from storage.models import LiquiditySnapshot
from validator.state_cache import StateCache
from utils.liquidity_utils import (
    LiquiditySubnet,
    fetch_subnet_liquidity_positions,
)
from utils.subnet_utils import get_metagraph

log = logging.getLogger("validator.liquidity_fetcher")


class LiquidityFetcher:
    """
    Fetches on‑chain liquidity, stores snapshots, and updates
    `cache.liquidity` in the form:

        cache.liquidity = { liquidity_subnet_id: { uid_on_66: tao, … }, … }

    **Important change**: we no longer sum `position.liquidity` (the Uniswap "L").
    We compute token amounts at the current subnet price via
    `LiquidityPosition.to_token_amounts(price)` and count **only TAO**.
    """

    # --- Optional policy knobs (you can tune these) -------------------- #
    # Minimum relative width of a position band to be counted.
    # Example: 0.003 => require band at least 0.3% wide around current price.
    MIN_RELATIVE_WIDTH: float = float(getattr(settings, "MIN_RELATIVE_WIDTH", 0.0))

    # If True, only count positions where current price is strictly in-range.
    COUNT_ONLY_IN_RANGE: bool = bool(getattr(settings, "COUNT_ONLY_IN_RANGE", True))

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        cache: StateCache,
        *,
        primary_netuid: int,   # ← validator’s own subnet (66)
        fetch_fn: Optional[
            Callable[..., Awaitable[List[LiquiditySubnet]]]
        ] = None,
    ) -> None:
        self.cache = cache
        self.primary_netuid = int(primary_netuid)
        self._fetch_fn = fetch_fn or self._default_fetch

        # Cache: coldkey → UID (on subnet 66)
        self._primary_uid_map: Dict[str, int] = {}
        self._primary_loaded: bool = False

    # ------------------------------------------------------------------ #
    # PUBLIC – async entry‑point                                         #
    # ------------------------------------------------------------------ #
    async def fetch_and_store(
        self,
        *,
        netuid: Optional[int] = None,
        block: Optional[int] = None,
    ) -> List[LiquiditySnapshot]:
        if netuid == 0:
            bt.logging.warning("[LiquidityFetcher] Ignoring request for subnet 0")
            return []

        bt.logging.info(f"[LiquidityFetcher] Fetching liquidity (netuid={netuid})…")

        # 1️⃣  Download liquidity ----------------------------------------
        if asyncio.iscoroutinefunction(self._fetch_fn):
            liquidity_subnets = await self._fetch_fn(netuid=netuid, block=block)
        else:
            liquidity_subnets = await asyncio.to_thread(
                self._fetch_fn, netuid=netuid, block=block
            )

        bt.logging.info(
            f"[LiquidityFetcher] Retrieved {len(liquidity_subnets)} "
            f"LiquiditySubnet objects"
        )

        # 2️⃣  Ensure primary‑subnet UID map is loaded once --------------
        if not self._primary_loaded:
            await self._load_primary_uid_map(block=block)

        # 2.5️⃣ Fetch current prices once per subnet ---------------------
        subnet_ids = [ls.netuid for ls in liquidity_subnets]
        bt.logging.warning(
            f"Fetching current prices, block={block}"
        )
        price_map = await self._fetch_current_prices(subnet_ids, block=block)
        bt.logging.warning(f"[LiquidityFetcher] Prices: {price_map}")
        if not price_map:
            bt.logging.warning(
                "[LiquidityFetcher] No current prices available; all contributions will be 0."
            )

        # 3️⃣  Aggregate TAO per coldkey / subnet ------------------------
        aggregated: Dict[Tuple[str, int, int], float] = {}

        for ls in liquidity_subnets:
            P = price_map.get(ls.netuid, 0.0)
            if not (P and P > 0.0 and math.isfinite(P)):
                bt.logging.warning(
                    f"[LiquidityFetcher] No valid price for subnet {ls.netuid}; skipping."
                )
                continue

            P_bal = Balance.from_tao(P)

            bt.logging.warning(
                f"[LiquidityFetcher] Subnet {ls.netuid} → "
                f"{ls.unique_coldkeys} coldkeys, {ls.total_positions} positions (P={P:.10f})"
            )

            for coldkey, positions in ls.coldkey_positions.items():
                tao_sum = 0.0

                for pos in positions:
                    # Defensive guards against invalid/degenerate ranges
                    try:
                        p_low = float(pos.price_low)
                        p_high = float(pos.price_high)
                    except Exception:
                        continue

                    if (
                        not math.isfinite(p_low)
                        or not math.isfinite(p_high)
                        or p_low <= 0.0
                        or p_high <= 0.0
                        or p_high <= p_low
                    ):
                        # reject zero/negative/degenerate ranges
                        bt.logging.warning(
                            f"[LiquidityFetcher] Drop degenerate range for {coldkey[:6]}… "
                            f"(low={p_low}, high={p_high})"
                        )
                        continue

                    # Optional: only reward *active* liquidity
                    if self.COUNT_ONLY_IN_RANGE and not (p_low < P < p_high):
                        continue

                    # Optional: enforce minimal band width (as fraction of current price)
                    if self.MIN_RELATIVE_WIDTH > 0.0:
                        rel_width = (p_high - p_low) / P
                        if rel_width < self.MIN_RELATIVE_WIDTH:
                            bt.logging.warning(
                                f"[LiquidityFetcher] Drop narrow band for {coldkey[:6]}… "
                                f"rel_width={rel_width:.6f} < {self.MIN_RELATIVE_WIDTH:.6f}"
                            )
                            continue

                    # Compute token amounts at current price
                    try:
                        alpha_amt, tao_amt = pos.to_token_amounts(P_bal)
                    except Exception as e:
                        bt.logging.warning(
                            f"[LiquidityFetcher] to_token_amounts() failed for {coldkey[:6]}…: {e}"
                        )
                        continue

                    # Sum **only the TAO leg** (true TAO provided)
                    tao_sum += self._bal_to_tao(tao_amt)

                if tao_sum > 0.0:
                    aggregated[(coldkey, ls.netuid, block or 0)] = tao_sum

        # 4️⃣  Persist new LiquiditySnapshot rows ------------------------
        bt.logging.warning(
            f"[LiquidityFetcher] Aggregated {aggregated} (coldkey, subnet) pairs"
        )
        new_rows: List[LiquiditySnapshot] = []
        with self.cache._session() as db:  # pylint: disable=protected-access
            for (ck, subnet, blk), tao_val in aggregated.items():
                if tao_val <= 0.0:
                    continue
                if not self._exists(db, ck, subnet, blk):
                    bt.logging.warning(
                        f"[LiquidityFetcher] New snapshot: {ck[:6]}… "
                        f"subnet {subnet} blk {blk} → {tao_val:.9f} TAO"
                    )
                    new_rows.append(
                        LiquiditySnapshot(
                            wallet_hotkey=ck,
                            subnet_id=subnet,
                            tao_value=tao_val,
                            block_height=blk,
                        )
                    )
        bt.logging.warning(
            f"new_rows prepared: {new_rows}"
        )

        if new_rows:
            self.cache.persist_liquidity(new_rows)
            bt.logging.warning(f"[LiquidityFetcher] Persisted {len(new_rows)} snapshots")

        # 5️⃣  Build liquidity map for RewardCalculator ------------------
        liq_map: Dict[int, Dict[int, float]] = defaultdict(dict)

        for (ck, subnet, _), tao_val in aggregated.items():
            if tao_val <= 0.0:
                continue
            uid = self._primary_uid_map.get(ck)
            if uid is None:
                bt.logging.debug(
                    f"[LiquidityFetcher] Coldkey {ck[:6]}… not on subnet "
                    f"{self.primary_netuid} – skipped"
                )
                continue
            liq_map[subnet][uid] = tao_val
            bt.logging.warning(
                f"[LiquidityFetcher] Map entry: subnet {subnet} uid {uid} "
                f"→ {tao_val:.9f} TAO"
            )

        self.cache.liquidity = liq_map
        bt.logging.warning(
            f"[LiquidityFetcher] liquidity map updated "
            f"(liquidity map {liq_map}"
            f"{sum(len(v) for v in liq_map.values())} UIDs)"
        )
        return new_rows

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _exists(db: Session, wallet: str, subnet: int, block_height: int) -> bool:
        return (
            db.query(LiquiditySnapshot)
            .filter_by(
                wallet_hotkey=wallet,
                subnet_id=subnet,
                block_height=block_height,
            )
            .first()
            is not None
        )

    @staticmethod
    def _bal_to_tao(b: Balance) -> float:
        """
        Best‑effort conversion of a Balance to TAO as float.
        Works whether Balance carries tao/rao or raises on float().
        """
        try:
            return float(b)  # preferred: Balance.__float__ should yield TAO
        except Exception:
            try:
                return float(getattr(b, "tao"))
            except Exception:
                try:
                    rao = float(getattr(b, "rao"))
                    return rao / 1e9
                except Exception:
                    return 0.0

    async def _default_fetch(
        self,
        *,
        netuid: Optional[int],
        block: Optional[int],
    ) -> List[LiquiditySubnet]:
        async with AsyncSubtensor(network=settings.BITTENSOR_NETWORK) as subtensor:
            return await fetch_subnet_liquidity_positions(
                subtensor,
                netuid=netuid,
                block=block,
                max_concurrency=settings.MAX_CONCURRENCY,
                logprogress=False,
            )

    async def _fetch_current_prices(
        self, subnets: List[int], *, block: Optional[int]
    ) -> Dict[int, float]:
        """
        Read current price per subnet from `Swap.AlphaSqrtPrice` and square it:
            P = (AlphaSqrtPrice)**2

        Returns: {netuid: price_float}
        """
        prices: Dict[int, float] = {}
        if not subnets:
            return prices

        async with AsyncSubtensor(network=settings.BITTENSOR_NETWORK) as st:
            try:
                block_hash = await st.determine_block_hash(
                    block=block, block_hash=None, reuse_block=(block is None)
                )
            except Exception:
                block_hash = None

            async def _one(uid: int):
                try:
                    sp = await st.substrate.query(
                        module="Swap",
                        storage_function="AlphaSqrtPrice",
                        params=[uid],
                        block_hash=block_hash,
                    )
                    sqrt_p = fixed_to_float(sp)
                    P = float(sqrt_p) * float(sqrt_p)
                    if math.isfinite(P) and P > 0.0:
                        return uid, P
                except Exception as e:
                    bt.logging.debug(
                        f"[LiquidityFetcher] AlphaSqrtPrice query failed for {uid}: {e}"
                    )
                return uid, 0.0

            results = await asyncio.gather(*(_one(u) for u in set(subnets)))
            for uid, P in results:
                if P > 0.0:
                    prices[uid] = P

        return prices

    # ---------- UID map (coldkey → UID on subnet 66) ------------------- #
    async def _load_primary_uid_map(self, *, block: Optional[int]) -> None:
        bt.logging.warning(
            f"[LiquidityFetcher] Loading UID map for subnet {self.primary_netuid}"
        )
        async with AsyncSubtensor(network=settings.BITTENSOR_NETWORK) as subtensor:
            mg = await get_metagraph(
                self.primary_netuid, st=subtensor, lite=True, block=block
            )
            self._primary_uid_map = {
                str(ck): int(uid) for uid, ck in zip(mg.uids, mg.coldkeys)
            }
        self._primary_loaded = True
        bt.logging.warning(
            f"[LiquidityFetcher] UID map loaded ({len(self._primary_uid_map)} entries)"
        )
