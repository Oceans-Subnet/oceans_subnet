# ============================================================================ #
# oceans/base/validator.py                                                     #
# ============================================================================ #
"""
Validator foundations.

* BaseValidatorNeuron  – original step-driven loop
* EpochValidatorNeuron – same features but runs forward() once per epoch
"""
from __future__ import annotations

import copy
import asyncio
import threading
import argparse
from traceback import print_exception
from typing import List, Union, Optional, Tuple

import numpy as np
import bittensor as bt

from base.neuron import BaseNeuron
from base.utils.weight_utils import (
    process_weights_for_netuid,
    convert_weights_and_uids_for_emit,
)

# ╭─────────────────────────── BASE CLASS ─────────────────────────────╮


class BaseValidatorNeuron(BaseNeuron):
    neuron_type: str = "ValidatorNeuron"

    # -------------------------- CLI hooks ----------------------------- #
    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)

    # -------------------------- construction -------------------------- #
    def __init__(self, config=None):
        super().__init__(config=config)

        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

        # Use float64 for scores to avoid any suspicion of loss of precision.
        self.scores = np.zeros(self.metagraph.n, dtype=np.float64)

        self.dendrite = bt.dendrite(wallet=self.wallet)
        bt.logging.info(f"Dendrite: {self.dendrite}")

        self.sync()

        if not self.config.neuron.axon_off:
            self.serve_axon()
        else:
            bt.logging.warning("axon off, not serving ip to chain.")

        # Event loop & run thread flags
        self.loop = asyncio.get_event_loop()
        self.should_exit = False
        self.is_running = False
        self.thread: Union[threading.Thread, None] = None

    # ------------------------ axon serving ---------------------------- #
    def serve_axon(self):
        try:
            self.axon = bt.axon(wallet=self.wallet, config=self.config)
            self.subtensor.serve_axon(netuid=self.config.netuid, axon=self.axon)
            bt.logging.info(
                f"Running validator {self.axon} on "
                f"{self.config.subtensor.chain_endpoint}  netuid={self.config.netuid}"
            )
        except Exception as e:
            bt.logging.error(f"Failed to serve axon: {e}")

    # ------------------------ forward helper -------------------------- #
    async def concurrent_forward(self):
        # The default implementation runs a single forward.
        await self.forward()

    # ---------------------------- run loop ---------------------------- #
    def run(self):
        self.sync()
        bt.logging.info(f"Validator starting at block: {self.block}")

        try:
            while not self.should_exit:
                bt.logging.info(f"step({self.step}) block({self.block})")
                self.loop.run_until_complete(self.concurrent_forward())
                self.sync()
                self.step += 1
        except KeyboardInterrupt:
            if hasattr(self, "axon"):
                self.axon.stop()
            bt.logging.success("Validator killed by keyboard interrupt.")
        except Exception as err:
            bt.logging.error(f"Error during validation: {err}")
            bt.logging.debug(print_exception(type(err), err, err.__traceback__))

    # ---------------- background-thread helpers ----------------------- #
    def run_in_background_thread(self):
        if not self.is_running:
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True

    def stop_run_thread(self):
        if self.is_running:
            self.should_exit = True
            self.thread.join(5)
            self.is_running = False

    def __enter__(self):
        self.run_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_run_thread()

    # ---------------------- weight-setting logic ---------------------- #
    def set_weights(self):
        """
        Emits weights to chain according to most recent `self.scores`.
        """
        if np.isnan(self.scores).any():
            bt.logging.warning("Scores contain NaN values.")

        norm = np.linalg.norm(self.scores, ord=1, axis=0, keepdims=True) or 1.0
        raw_weights = self.scores / norm

        (
            processed_uids,
            processed_weights,
        ) = process_weights_for_netuid(
            uids=self.metagraph.uids,
            weights=raw_weights,
            netuid=self.config.netuid,
            subtensor=self.subtensor,
            metagraph=self.metagraph,
        )

        uint_uids, uint_weights = convert_weights_and_uids_for_emit(
            uids=processed_uids, weights=processed_weights
        )

        ok, msg = self.subtensor.set_weights(
            wallet=self.wallet,
            netuid=self.config.netuid,
            uids=uint_uids,
            weights=uint_weights,
            wait_for_finalization=False,
            wait_for_inclusion=True,
            version_key=self.metagraph.hparams.weights_version,
        )
        if ok:
            bt.logging.warning("set_weights on chain successfully!")
        else:
            bt.logging.error("set_weights failed", msg)

    # ------------------------ metagraph sync -------------------------- #
    def resync_metagraph(self):
        """
        Re-syncs the metagraph and keeps `self.scores` aligned with the
        current miner set.
        """
        prev = copy.deepcopy(self.metagraph)
        self.metagraph.sync(subtensor=self.subtensor)

        if prev.axons == self.metagraph.axons:
            return  # nothing changed

        # Zero scores for replaced hotkeys
        for uid, hk in enumerate(self.hotkeys):
            if hk != self.metagraph.hotkeys[uid]:
                self.scores[uid] = 0.0

        # Resize scores array if the subnet grew
        if len(self.hotkeys) < len(self.metagraph.hotkeys):
            new_scores = np.zeros(self.metagraph.n, dtype=self.scores.dtype)
            new_scores[: len(self.scores)] = self.scores
            self.scores = new_scores

        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

    # ---------------------- score update (EMA) ------------------------ #
    def _top_k_summary(
        self, arr: np.ndarray, k: int = 5
    ) -> List[Tuple[int, float]]:
        """Return top-k (uid, value) from `arr` for compact logging."""
        if arr.size == 0:
            return []
        k = max(1, min(k, arr.size))
        idx = np.argpartition(arr, -k)[-k:]
        idx = idx[np.argsort(arr[idx])[::-1]]
        return [(int(i), float(arr[i])) for i in idx if arr[i] > 0.0]

    def update_scores(self, rewards: np.ndarray, uids: List[int]):
        """
        EMA update of `self.scores` using per-miner `rewards`.
        """
        # ---- SAFEGUARD: initialise if ever None -----------------------
        if not isinstance(self.scores, np.ndarray):
            self.scores = np.zeros(self.metagraph.n, dtype=np.float64)
        # ---------------------------------------------------------------

        # Ensure float64 internally for clarity/precision.
        rewards = np.nan_to_num(np.asarray(rewards, dtype=np.float64), nan=0.0)
        uids = np.asarray(uids, dtype=int)

        if rewards.size == 0 or uids.size == 0:
            bt.logging.warning("update_scores called with empty inputs.")
            return
        if rewards.size != uids.size:
            raise ValueError(
                f"Shape mismatch: rewards {rewards.shape} vs uids {uids.shape}"
            )

        # Scatter rewards into full-size vector (in metagraph UID space)
        scattered = np.zeros_like(self.scores, dtype=np.float64)
        scattered[uids] = rewards

        # Log brief pre-update summary (BEFORE EMA)
        pre_nnz = int(np.count_nonzero(self.scores))
        pre_sum = float(self.scores.sum())
        pre_max = float(self.scores.max() if self.scores.size else 0.0)

        alpha = 0.1
        # EMA update
        self.scores = alpha * scattered + (1.0 - alpha) * self.scores

        # POST-update summary (this is what you want to see)
        post_nnz = int(np.count_nonzero(self.scores))
        post_sum = float(self.scores.sum())
        post_max = float(self.scores.max() if self.scores.size else 0.0)
        top5 = self._top_k_summary(self.scores, k=5)

        bt.logging.warning(
            "Scores updated (EMA). "
            f"alpha={alpha:.3f} | "
            f"post: nnz={post_nnz}, sum={post_sum:.6f}, max={post_max:.6f} | "
            f"top5={top5}"
        )

        # Keep very detailed arrays at DEBUG level only (optional)
        bt.logging.debug(f"rewards(scattered) nnz={int(np.count_nonzero(scattered))}, "
                         f"sum={float(scattered.sum()):.6f}")
        bt.logging.debug(f"full scores vector: {self.scores}")

    # ---------------------- miner ordering helper --------------------- #
    def get_miner_uids(self, *, exclude: Optional[List[int]] = None) -> np.ndarray:
        """
        Returns a shuffled array of miner UIDs, excluding any specified.
        """
        if exclude is None:
            exclude = []
        uids = np.arange(len(self.metagraph.hotkeys), dtype=np.int32)

        if exclude:
            mask = np.isin(uids, np.asarray(exclude, dtype=np.int32), invert=True)
            uids = uids[mask]

        rng = np.random.default_rng(seed=int(self.epoch_index))
        rng.shuffle(uids)
        return uids

    # -------------------------- state I/O ----------------------------- #
    def save_state(self):
        bt.logging.info("Saving validator state.")
        np.savez(
            f"{self.config.neuron.full_path}/state.npz",
            step=self.step,
            scores=self.scores,
            hotkeys=self.hotkeys,
        )

    def load_state(self):
        bt.logging.info("Loading validator state.")
        state = np.load(f"{self.config.neuron.full_path}/state.npz")
        self.step = int(state["step"])
        self.scores = state["scores"]
        self.hotkeys = state["hotkeys"]
