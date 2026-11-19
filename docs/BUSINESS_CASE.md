# Business case: what this platform is for

An independent learning project on synthetic data. The numbers below come
from `bench/results/xva.json` (`bench/bench_xva.py`, on a laptop) and the
other files in `bench/results/`. None of them are claims about a real bank.

## The decision it supports

A corporate client asks a bank for a 5-year equity hedge. Before the trader
says yes, three questions need answers in seconds, not overnight:

| Question | Who asks | Number | Where it comes from |
| --- | --- | --- | --- |
| Does this fit inside the client's credit line? | Credit risk | Peak PFE (95%) of the netting set with the new trade | `/risk/exposure` → `peak_pfe`, `peak_pfe_csa` |
| What will the client's possible default cost us, and what do we charge for it? | XVA desk, trader | Incremental CVA (and DVA, bilateral CVA) | Two runs on the same scenarios (common random numbers) |
| Is it worth negotiating the collateral agreement (CSA)? | Collateral, legal, sales | CVA and PFE under different thresholds, MTA and margin period | `csa` block of the request |

The intraday path (`/price`, `/portfolio/price`) prices books for traders.
The batch path (`/risk/exposure`) answers the three questions above.

## What the measured numbers say

Benchmark book: 200 trades on 5 correlated indices, 50,000 scenarios, monthly
to 5 years. The counterparty is a BBB-like corporate (150 bp CDS, 40%
recovery) and the bank is at 60 bp.

| Finding | Measured | What it means for the business |
| --- | --- | --- |
| A zero-threshold CSA at a 10-business-day margin period (one synthetic 200-trade book, 50,000 scenarios) | CVA $102.1k → $40.5k (−60%); peak PFE $12.5M → $10.2M (−18%), compared with the uncollateralized run on the same scenarios | The CSA cuts CVA by about $61.6k on this one book and frees 18% of peak exposure under the credit line |
| MPoR 0 → 2 → 5 → 10 → 20 business days | CVA $0.4k → $13.7k → $25.6k → $40.5k → $57.7k | Faster, more reliable margining is worth real money. The model prices the operational quality of the collateral process |
| Threshold sweep, $0 → $1M → $5M → $20M | CVA $40.5k → $40.4k → $78.8k → $101.9k (uncollateralized: $102.1k) | Above about $20M the CSA does nothing for this book. Sales and legal know where to negotiate |
| Pre-deal incremental CVA of a new 5-year forward (30 repetitions, 10,000 scenarios) | Standard deviation $328 on shared scenarios vs $1,809 with independent seeds; 0.27 s per engine run on 10 threads | 5.5× less noisy for the same compute, about 30× fewer scenarios for equal precision. Measured for one forward on this book; the ratio shrinks as the new trade grows relative to the book |
| `POST /risk/predeal`, end to end (201 trades, 10,000 scenarios, 2 compute threads) | 1.43 s first quote; 0.70 s to re-quote once the book is cached; 4.18 s when a limit breach also prices a fallback CSA (4 passes). Benchmark trade: approve, upfront charge 29.7 bp of notional. Deployed pods use 1 thread per quote, so latency there is higher | A trader gets a decision (approve, approve only with a CSA, refer to credit) and a price for credit in seconds, from one call |
| `POST /risk/cva-sensitivities` (200 trades, 10,000 scenarios, 21 runs) | 15.2 s at 2 threads; CVA $101.0k, CS01 $634 | Per-underlying delta and vega for hedging. An end-of-day or on-demand calculation, not a quote-time one |
| Exposure run, 604M option revaluations | 2.7 s on 8 workers, identical on 1, 2, 4 and 8 workers | An end-of-day XVA run is minutes, not hours, and reproducible for audit |
| Reliability under chaos, 10 minutes, 11 faults | 99.992% of 76,481 prices served; 29 of 30 books complete | The numbers arrive on time when infrastructure fails. The one lost book now has a fix (below) |

## Engineering decisions and their business value

| Decision | Business value |
| --- | --- |
| Counter-based RNG, seed from market state only | The same request gives the same answer on any worker, which means audit, reproducibility and safe retries. What-if runs differ only in what changed |
| Exact merge of scenario blocks (sums add, PFE from the union of per-block top-k) | Scale out without changing the risk number. Model validation can sign off once |
| `Idempotency-Key` on `/portfolio/price`, plus `GET /jobs/{id}` | A client whose connection drops retries and gets the same answer, priced once. This closes the open issue from chaos run 3 (1 book in 30 lost to connection resets). It is covered by unit tests; the 10-minute chaos run has not been re-run since |
| Worker heartbeat on long tasks; a duplicate's error never overwrites a good result | Large exposure blocks (over 10 s) were being reclaimed while still running and could be failed as "poison". Batch runs that used to fail intermittently now complete |
| Intraday and batch on separate streams and pools | A big end-of-day XVA run does not slow trader quotes (5.17 s → 0.32 s measured) |
| Owner-labelled alerts, runbook, SLOs as code | Incidents reach the right team first time. Mean time to repair is an operating cost |
| CI: lint, coverage gate, sanitizers, manifest and alert-rule validation. Release after CI: local build, Trivy scan, then push with SBOM and provenance and a cosign signature by digest | Supply-chain and change-management evidence that a bank's model-risk and security reviews ask for |

## Executive review: is the impact, usage and interactivity there?

Reviewed on 2026-11-18 by walking the live site and the API as a first-time
user, not by reading the code.

| Lens | Before this review | After | Evidence |
| --- | --- | --- | --- |
| **Impact**: is the business value visible? | Measured but buried: the landing page showed only reliability numbers; the desk link was off-screen | Landing page leads with a "Try the XVA desk" button and three business tiles built from `bench/results/xva.json` | `docs/index.html`, `scripts/build_report.py` |
| **Usage**: can someone use it for the decision it exists for? | No. The desk page asked "should we say yes to this trade?" and could not answer it; the API needed two exposure jobs, polling and client-side math | `POST /risk/predeal`: one call returns approve / approve with a CSA / refer to credit, the reason, the upfront credit charge in $ and bp of notional (and as a running spread), and limit use. The book's own run is cached between quotes | `service/main.py: predeal`, `tests/test_predeal.py` |
| **Interactivity**: can a non-specialist feel it in 60 seconds? | A CSA simulator: good for intuition, but no decision at the end | A pre-deal panel with the same verdict rules as the API, four one-click scenarios (refer, needs a CSA, approve, a hedge that lowers CVA) and a copyable request for the real endpoint | `docs/desk.html#predeal`, checked in a browser at 375 px |

## Review panel: what was found and what changed

A panel of XVA quants, a distributed-systems director, a principal engineer
and an executive lens (CEO, CTO, business analyst) reviewed the platform.
The panel is a review exercise on a learning project, not an approval. This
table lists what each lens found and the status today. "Fixed" names the
evidence; "open" means not done.

| Finding | Raised by | Status |
| --- | --- | --- |
| MPoR was ambiguous (calendar or business days) | Quant | Fixed. `mpor_days` is business days on a 252-day year (10 days is about 14.5 calendar days), and the CSA results were re-run |
| Common random numbers claim was too broad; the seed included spots, vols, trades and credit | Quant | Fixed. The seed now covers only market data version, underlying names, correlation and nodes, so bumps, added trades and spread or threshold changes share scenarios |
| The "10×" and "100×" noise claims came from 6 repetitions | Quant | Fixed. 30 repetitions: 5.5× less noisy, about 30× fewer scenarios, for one 5-year forward on this book (`pre_deal` in `xva.json`) |
| Pre-deal charge units were unclear, and a hedge showed a negative charge | Quant, exec | Fixed. `cva_charge_bps` is upfront in bp of notional, `cva_charge_running_bps` is the running spread, hedges report a credit benefit, and an `approve_with_csa` charge is priced under the proposed CSA |
| No CVA sensitivities | Quant | Fixed. `POST /risk/cva-sensitivities` (delta, vega, CS01), checked against a closed form: delta within 0.1%, vega within 1% (`tests/test_sensitivities.py`) |
| Pre-deal bursts starved the pod: 300 concurrent quotes, 0 shed, worst 36.4 s, `/health/ready` timed out, `/price` 4.4 to 5.4 s | Distributed systems | Fixed. Risk quotes have their own slot checked before any thread is taken: 299 of 300 shed with 503, worst 0.2 s, `/health/ready` 3 ms, `/price` 2.7 ms. `tests/test_shedding.py` locks it in |
| Release pushed before scanning and ran on any push | Principal engineer | Fixed. Builds locally, Trivy-scans, then pushes with SBOM and provenance and signs by digest. Runs after CI succeeds on main or on `v*` tags. Actions pinned by commit SHA |
| No alerts on the new risk endpoints; routing by team name only | Distributed systems | Fixed. `PredealLatencyHigh`, `PredealErrorsHigh`, `RiskEndpointShedding`, and Alertmanager routes by severity to `<owner>-page` and `<owner>-ticket` |
| MTA tested node to node, not daily | Quant | Open, documented as a limitation in MODELS.md |
| Wrong-way risk is not modelled | Quant | Open |
| PFE is risk-neutral, not real-world, so not fit for limits | Quant | Open |
| No validation against an established XVA library (for example Open Source Risk Engine) | Quant | Open |
| No Monte Carlo standard error on CVA | Quant | Open |
| No authentication or TLS on the API, Redis or Cassandra; egress is not default-deny | Principal engineer | Open |
| Pre-deal shares pods with `/price`; no dedicated low-latency worker pool or per-book scenario cubes | Distributed systems, CTO | Open |
| The chaos run has not been repeated since the idempotency fix | Distributed systems | Open |
| Sensitivities are bump-and-revalue; no pathwise or AAD, no GPU | Quant | Open. Next step is pathwise or AAD sensitivities in the C++ engine |

Capital (SA-CCR, BA-CVA) is a separate regulated calculation that this
project does not claim to do.
