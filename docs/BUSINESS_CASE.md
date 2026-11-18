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
| A zero-threshold CSA at the 10-day regulatory margin period | CVA $102.2k → $28.9k (−72%); peak PFE $12.4M → $7.1M (−43%), compared on the same scenarios | The CSA is worth about $73k of CVA on this one book. It also frees about 40% of the credit line for more trading with the same client |
| Margin period of risk 0 → 2 → 10 → 20 days | CVA $0.4k → $11.8k → $28.9k → $49.7k | Faster, more reliable margining is worth real money. The model prices the operational quality of the collateral process |
| Threshold sweep, $0 → $1M → $5M → $20M | CVA $28.9k → $32.1k → $77.5k → $102.0k | Above about $20M the CSA does nothing for this book. Sales and legal know where to negotiate |
| Pre-deal incremental CVA of a new 5-year forward | Standard deviation $188 on the same scenarios vs $1,961 with independent seeds; 0.27 s per run | 10× tighter for the same compute, which is about 100× fewer scenarios for the same precision. A trader gets a quote-quality number in under a second |
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
| CI: lint, coverage gate, sanitizers, manifest and alert-rule validation. CD: signed image with SBOM and vulnerability scan | Supply-chain and change-management evidence that a bank's model-risk and security reviews ask for |

## Review

**Principal business analyst: approved, with conditions.**
- The platform answers a real, recurring front-office decision (pre-deal
  credit and CVA, CSA negotiation) with numbers each stakeholder already uses.
- The value is shown in their units: dollars of CVA, % of credit line, and
  seconds to a quote.
- Conditions before any real use:
  - wrong-way risk and stochastic rates and credit;
  - real-world PFE for limits (PFE here is risk-neutral);
  - validation against an established XVA library on real trades;
  - capital (SA-CCR, BA-CVA) as a separate, regulated calculation, which this
    project does not claim.

**CTO: approved for what it is, an engineering reference.**
- The determinism and exact-merge guarantees are tested, not asserted. The
  C++ changes have independent references and pass under UBSan locally.
  ASan, TSan and Valgrind run in CI's `memory` job; they have not yet run on
  this branch.
- The open reliability issue has a tested fix.
- The cluster now runs with:
  - non-root, read-only pods;
  - default-deny network policies;
  - pod disruption budgets.
- Remaining risks:
  - Redis and Cassandra have no authentication or TLS;
  - the API has no authentication or authorization;
  - the price log can still drop rows under sustained Cassandra outage;
  - the chaos run should be repeated to confirm the idempotency fix end to end.

  These are listed in RUNBOOK.md and `docs/PERFORMANCE_REVIEW.md`, not hidden.
