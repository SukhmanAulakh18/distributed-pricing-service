# Guarded remediation runner

`python -m tools.remediate` takes [triage](../tools/triage.py) findings
(`--findings snapshot.json`, or `--demo redis|poison|worker|backlog|reset`),
looks each one up in a fixed allowlist, performs the one mapped action, then
proves it by replaying the matching [siege](../tests/siege) test. Every
decision lands in a hash-chained ledger.

It is a rules table with a gate and a witness. It is **not** an autonomous
agent and uses no LLM.

## What it deliberately does not do

- No LLM decisions: finding kind to action is a static table in the code.
- No cluster mutations: no kubectl, no network. The one subprocess is
  `python -m pytest -q <test>` on the local siege harness.
- No free-form commands: the pytest target comes from the allowlist, never
  from a finding or a CLI argument.
- The action bodies are inert (diagnostics text, advice, a retry note). The
  value is the gate, the replay evidence and the audit trail, not the action.

## Guardrails

1. **Allowlist.** Kinds not listed (Cassandra, HPA, anything new) are
   `refused` and logged.
2. **Approval gate.** Mutating actions default to dry-run: outcome
   `proposed`, nothing run. `--approve --by <name>` executes; the approver
   must differ from the invoking operator (`--who`).
3. **Evidence by read-back.** An action is `verified` only if its siege test
   passes (120 s timeout). Otherwise `unverified`; the action's own return
   value is never trusted.
4. **Ledger.** `var/remediation-ledger.jsonl` (gitignored) is append-only;
   each entry carries `prev_hash` and `hash` = sha256 of the canonical entry.
   `--verify-ledger` names the first broken line. A truncated tail needs an
   externally stored last hash to detect.

Exit code: 0 all verified or proposed, 1 any refused or unverified, 2 ledger
tampered.

## Bank controls

- Segregation of duties: the operator who runs it cannot approve their own
  mutating action.
- Audit trail: who, what finding, owner, runbook section, approver, evidence
  and outcome are recorded tamper-evidently.
- Least privilege and change control: a closed action list; adding an action
  is a code review.

## Example ledger entry

```json
{"action": "resubmit_with_same_idempotency_key", "approved_by": "alice",
 "evidence": {"passed": true, "seconds": 2.67,
              "test": "tests/siege/test_client_reset.py::test_client_reset_then_retry_with_same_key"},
 "finding": {"title": "HTTP 5xx on pricing endpoints", "severity": "warning", "owner": "Core engineering (pricing service)", "runbook": "§2", "evidence": "4 since pod start"},
 "mutates": true, "outcome": "verified", "owner": "Core engineering (pricing service)",
 "runbook_section": "§2", "ts": "2026-11-19T03:08:43+00:00", "who": "op",
 "prev_hash": "ebccb0e5...", "hash": "21684069..."}
```
