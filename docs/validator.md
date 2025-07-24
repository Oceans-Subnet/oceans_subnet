# Oceans Subnet 66 • Validator Guide (v0)

**Last updated:** July 2025  
**Applies to:** Oceans Subnet 66 (Bittensor Network, version 0)

---

## 1. Purpose of This Guide
This document describes how to operate a **validator** on Oceans Subnet 66.

---

## 2. High‑Level Validator Role
Validators on Subnet 66 are the **referees** that keep mining incentives fair and aligned with community intent. In v0 they perform three core duties:

1. **Vote Ingestion**  
   * **Fetch the weight vector** that α‑Stake holders set via off‑chain web voting.  
   * Make the vector publicly auditable (e.g., include it in your block metadata or an IPFS snapshot).

2. **Liquidity Measurement**  
   * **Observe on‑chain liquidity** that each registered miner supplies to eligible Bittensor subnet pools.  
   * Convert each miner’s positions to a USD valuation using the reference price oracle defined in the protocol.

3. **Reward Calculation & Attribution**  
   * Combine the **community weight vector** with **measured liquidity per miner** to compute each miner’s reward for the epoch.  
   * Publish the per‑miner scores so other validators can verify and the chain can distribute emissions.

---

## 3. Key Concepts & Terminology
| Term | Meaning on Subnet 66 |
|------|---------------------|
| **Validator Neuron** | Your identity inside Bittensor that signs and submits scoring data. |
| **Weight Vector** | List of weights per subnet pool derived from α‑Stake voting. |
| **Measured Liquidity** | Miner’s USD‑valued pool balances observed by validators. |
| **Score Matrix** | Table of `miner → reward_share` values produced each epoch. |
| **Consensus** | Majority agreement among validators on both the weight vector and score matrix. |

---

## 4. Validator Lifecycle

| Stage | What You Do | What the Network Expects |
|-------|-------------|--------------------------|
| **Register** | Submit a validator registration transaction with your coldkey. | Records your validator neuron and collateral stake. |
| **Retrieve Votes** | Pull the latest weight vector from the Oceans v0 API or mirror site. | Make the exact vector available for audit. |
| **Sample Liquidity** | Query on‑chain contracts to enumerate each miner’s pool balances. | Use the reference oracle for USD conversions. |
| **Compute Scores** | For every miner: `score = Σ(liquidity_pool_i × weight_pool_i)` | Follow the common math spec; round to required precision. |
| **Publish Scores** | Sign and broadcast the score matrix buffer before the epoch deadline. | Other validators cross‑verify; disagreement triggers slashes if malicious. |
| **Self‑Audit** | Compare your vector & scores with peers; alert if divergence > tolerance. | Maintain ≥ threshold agreement to stay in the quorum set. |

---

## 5. Consensus & Dispute Resolution
* **Soft‑Finality Window:** Validators have a grace period to cross‑check published vectors and scores.  
* **Discrepancy Handling:**  
  * If a minority set diverges, their submissions are ignored and may be slashed.  
  * If majority disagreement exists, the epoch enters *contested* status; a governance vote or manual review is triggered.  
* **Transparency:** All intermediate artifacts (raw pool snapshots, oracle prices, weight file hash) must be retained for post‑epoch audits.

---

## 6. Best‑Practice Playbook
1. **Automate Vote Fetching**  
   * Poll the Oceans API more often than the maximum expected vote‑change frequency (suggested: every 5 minutes).

2. **Use Redundant Data Sources**  
   * Maintain at least two independent RPC endpoints for on‑chain queries.  
   * Mirror the weight vector file locally and pin to IPFS.

3. **Time‑Bound Computations**  
   * Complete liquidity sampling and score calculation well before the publication deadline to allow for manual overrides if needed.

4. **Run Health Checks**  
   * Validate schema of fetched data, check for zero or negative weights, and ensure oracle prices are fresh.

5. **Collaborate, Don’t Compete**  
   * Join the validator channel in Discord; share anomalies and edge‑case findings promptly.

---

## 7. Risk & Responsibility Matrix

| Category | Potential Issue | Mitigation |
|----------|-----------------|------------|
| **Data Integrity** | Weight vector tampering | Verify SHA‑256 hash against community‑posted reference; require quorum signature set. |
| **Oracle Failure** | Stale or manipulated prices | Cross‑compare two oracles; fail‑safe to previous epoch prices if discrepancy > X %. |
| **Latency** | Missing publication window | Monitor system clock; run NTP; set alert thresholds. |
| **Slashing** | Publishing incorrect scores | Implement internal double‑entry check; simulate before broadcast. |
| **Security** | Key compromise | Store coldkey offline; use separate hotkey for signing; rotate tokens periodically. |

---

## 8. Operational Checklist (No Code)

1. **Infrastructure Ready** → Two independent cloud/VPS nodes with automated fail‑over.  
2. **Validator Neuron Registered** → Confirm on Subnet 66 explorer.  
3. **Weight Vector Mirror** → Local copy updated and hashed.  
4. **Oracle Feeds Live** → Last price update < 60 s.  
5. **Liquidity Indexer Sync** → No gaps in historical on‑chain data.  
6. **Score Simulator Pass** → Internal dry‑run equals peer reference sample.  
7. **Alerting & Logs** → Centralized dashboard with retention ≥ 30 days.  
8. **Backup & Keys** → Offline copies and revocation plan.

---

## 9. Frequently Asked Questions

| Question | Answer |
|----------|--------|
| *Do validators need to hold α‑Stake?* | Not required, but it aligns incentives and grants voting power. |
| *How are validator rewards calculated?* | A fixed share of subnet emissions is split proportionally by each validator’s stake and uptime, provided their scores match consensus. |
| *What happens if my node goes offline?* | You forfeit that epoch’s validator reward and, if prolonged, may be pruned from the active set. |
| *Is slashing permanent?* | Slashed stake is burned. Severe or repeated offenses can also lead to blacklist. |
| *Will v0.2 change my workflow?* | Yes—weight vectors will be on‑chain, removing the off‑chain fetch step and simplifying consensus logic. |

---

## 10. Roadmap Impact on Validators
| Version | Change | Required Validator Action |
|---------|--------|---------------------------|
| **v0.1 Dashboard (Q4 2025)** | Real‑time telemetry for weight vectors and score agreement. | Integrate WebSocket feed for faster self‑audit. |
| **v0.2 On‑Chain Voting (Q1 2026)** | Weight vector fetched directly from smart contract. | Update codebase to read on‑chain mapping; retire off‑chain mirror. |
| **v0.3 Burn‑Boost Bounties (Q2 2026)** | Temporary weight multipliers for boosted pools. | Add “boost decay” term to score formula; publish extended score schema. |

---

## 11. Support & Community
* **Discord:** `https://discord.gg/bittensor`  
* **Twitter:** `@OceansSN66`  
* **Website:** `https://oceans66.com`

For validator‑specific incidents, tag the **#validators** channel on Discord or submit a GitHub issue referencing your neuron ID.

---

### Acknowledgements
Thanks to the Bittensor core team and every operator striving for transparent, community‑driven liquidity. Together we keep the **oceans** deep, clear, and fair.

*Secure validating!*  
