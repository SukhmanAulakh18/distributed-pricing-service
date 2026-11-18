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
| Pre-deal incremental CVA of a new 5-year forward | Standard deviation $188 on the same scenarios vs $1,961 with independent seeds; 0.25 s per engine run on 10 threads | 10× tighter for the same compute, which is about 100× fewer scenarios for the same precision |
| `POST /risk/predeal`, end to end (201 trades, 10,000 scenarios, an API pod's 2 compute threads) | 1.39 s first quote; 0.70 s to re-quote once the book is cached; 2.76 s when a limit breach also prices a fallback CSA. Verdict on the benchmark trade: approve, 30 bp credit charge | A trader gets a decision (approve, approve only with a CSA, refer to credit) and a price for credit in about a second, from one call |
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

## Executive review: is the impact, usage and interactivity there?

Reviewed on 2026-11-18 by walking the live site and the API as a first-time
user, not by reading the code.

| Lens | Before this review | After | Evidence |
| --- | --- | --- | --- |
| **Impact**: is the business value visible? | Measured but buried: the landing page showed only reliability numbers; the desk link was off-screen | Landing page leads with a "Try the XVA desk" button and three business tiles (−72% CVA with a CSA, the pre-deal time, 10× tighter what-if pricing), built from `bench/results/xva.json` | `docs/index.html`, `scripts/build_report.py` |
| **Usage**: can someone use it for the decision it exists for? | No. The desk page asked "should we say yes to this trade?" and could not answer it; the API needed two exposure jobs, polling and client-side math | `POST /risk/predeal`: one call returns approve / approve with a CSA / refer to credit, the reason, the credit charge in $ and bp, and limit use. The book's own run is cached between quotes | `service/main.py: predeal`, `tests/test_predeal.py` (9 tests) |
| **Interactivity**: can a non-specialist feel it in 60 seconds? | A CSA simulator: good for intuition, but no decision at the end | A pre-deal panel with the same verdict rules as the API, four one-click scenarios (refer, needs a CSA, approve, a hedge that lowers CVA) and a copyable request for the real endpoint | `docs/desk.html#predeal`, checked in a browser at 375 px |

**Principal business analyst: approved, with conditions.**
- The platform now produces the *decision* a desk makes, not just inputs to
  it, in the units each stakeholder uses: dollars and basis points of
  credit charge, % of the credit line, seconds to an answer.
- The hedge scenario shows something non-obvious and valuable: a trade can
  *lower* CVA by netting against the book, so the desk can pay the client
  for it rather than charge.
- Conditions before any real use:
  - wrong-way risk and stochastic rates and credit;
  - real-world PFE for limits (PFE here is risk-neutral);
  - validation against an established XVA library on real trades;
  - capital (SA-CCR, BA-CVA) as a separate, regulated calculation, which this
    project does not claim.

**CTO: approved as an engineering reference.**
- Determinism and exact merging are tested, not asserted.
- The C++ changes have independent references and pass ASan, UBSan, TSan
  and Valgrind in CI.
- The pre-deal endpoint reuses admission control, so a burst of quotes is
  shed with 503s instead of starving `/price`.
- It runs the C++ engine with the GIL released and caps the size (50,000
  scenarios) so a quote cannot become a batch job.
- Release images are scanned and signed, and the scanner action is pinned
  by commit SHA.
- Remaining risks:
  - Redis and Cassandra have no authentication or TLS;
  - the API has no authentication or authorization;
  - the price log can still drop rows under sustained Cassandra outage;
  - the chaos run should be repeated to confirm the idempotency fix end to end;
  - pre-deal latency at 2 threads (about 1.4 s first quote) is fine for a
    trader but not for e-trading. More threads per quote, or precomputed
    book scenarios, is the next lever.

**CEO: the story now holds together.**
- One sentence: *before a trader says yes, it tells them whether the
  client's credit line can take the trade and what to charge for the risk,
  in about a second, and it keeps answering when servers fail.*
- A visitor can now *do* that on the site in three clicks, and an engineer
  can do it against the real engine with one request.
- What would still move the needle most: a hosted demo of the API itself,
  so the "real engine" step needs no `make up`, and a short recorded
  walkthrough of the pre-deal panel for people who will not click.
