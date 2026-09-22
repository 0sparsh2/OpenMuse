# Incident runbooks — safety/runbooks.md

Deterministic response procedures for policy-violation incidents. Each
runbook names the detection signal, severity, containment steps, evidence
handling, and recovery. Automation hooks reference the implementing code.

General principles:

- Deterministic policy is the final authority; no incident step may weaken it.
- Preserve the event log first — it is append-only and hash-chained.
- Quarantine before investigating: `safety.support.quarantine_run`.
- Never paste raw secrets or raw user data into tickets, chats, or bundles;
  use the privacy-preserving diagnostics builder.

---

## RB-1 — Prompt-injection attempt detected

**Signal:** `INJECTION_BLOCKED` / `INJECTION_REVIEW_REQUIRED` policy decisions,
or a red-team fixture failure in CI.

**Severity:** P1 if an effect occurred; P2 if blocked at policy (the common case).

**Contain:**

1. The policy engine already denied the call — verify no grant was issued
   for the same argument hash (`ApprovalService.find_valid_grant`).
2. Quarantine the run: `quarantine_run(run_id=..., approvals=..., reason="RB-1")`.
   This denies all pending approvals for the run.
3. Mark the tainted artifact in the taint tracker; do NOT clear its taint.

**Evidence:**

4. Export the run's event log (JSONL) — it contains the injection finding,
   matched rules, and the policy decision with reason code.
5. Build a diagnostic bundle via `DiagnosticsBuilder` (redacted by construction).

**Eradicate / recover:**

6. Identify the source (web/email/PDF/child output) from the taint provenance.
7. If the source is a recurring feed (connector, hook), disable the hook or
   connector until the source is remediated.
8. Add the payload to `safety/fixtures/redteam_corpus.yaml` as a new fixture
   so the regression gate pins the behavior.

**Post-incident:** review whether the classifier needs a new rule; classifier
changes are deterministic regex additions with a paired fixture.

---

## RB-2 — Credential / secret leak (persistence or egress blocked)

**Signal:** `SECRET_PERSIST_BLOCKED` / `SECRET_EGRESS_BLOCKED` from
`safety.secret_scan`, or `SECRET_LEAK_BLOCKED` from a connector.

**Severity:** P1 — treat as a leak until proven otherwise.

**Contain:**

1. The write/send was blocked — confirm the blocked labels in the scanner log.
2. Quarantine the run (RB-1 step 2).
3. Rotate the affected credential at the provider: the leaked value is known
   only by label; retrieve the actual value from the vault (never from logs)
   to identify which credential to rotate.

**Evidence:**

4. Preserve the event log; the scanner stores labels only, never values.
5. Check whether an earlier, unblocked write of the same run persisted the
   value: search the tenant's stores for the canary/label via the vault's
   `scan()` (hashes only).

**Eradicate / recover:**

6. Rotate the credential; revoke affected grants/sessions.
7. If the value reached an external sink before the block (egress race),
   notify per the data-handling policy and record the disclosure.

**Post-incident:** add a canary regression fixture; review why the value
entered the context (prompt, tool output, or user paste) and tighten the
ingest-side redaction.

---

## RB-3 — Approval tampering / argument mutation

**Signal:** `find_valid_grant` misses on an executed call, `APPROVAL_HASH_MISMATCH`
from the API layer, or repeated `TAINT_CLEARANCE_REQUIRED` escalations.

**Severity:** P1.

**Contain:**

1. The mutated call was denied — verify the executed argument hash differs
   from every issued grant for the run.
2. Quarantine the run.
3. Suspend the session's approval auto-flows (if any) pending review.

**Evidence:**

4. Event log shows the approved hash vs the executed hash — diff the
   canonicalized arguments to identify the mutated field.
5. Check whether the mutation originated from tainted content (RB-1) or
   from the model drifting off the approved plan.

**Recover:**

6. The user may re-issue a fresh approval for the corrected arguments;
   never "repair" a grant in place — grants are immutable and single-use.

---

## RB-4 — Support access misuse

**Signal:** `SupportAccessService` audit shows denied checks, expired-grant
use, or access outside the grant's purpose.

**Severity:** P2; P1 if raw user data was accessed (should be impossible —
no support scope exposes raw content).

**Contain:**

1. Revoke the grant: `SupportAccessService.revoke(grant_id)`.
2. Quarantine any runs the support session touched.

**Evidence:**

3. The audit trail (`support.audit`) is complete: grant issuance, every
   check with allow/deny, expiry, and revocation.
4. Rebuild the diagnostic bundles that were issued and secret-scan them.

**Recover:**

5. Re-issue only with a narrower scope and shorter TTL after review.

---

## RB-5 — Cross-origin redirect / destination escape

**Signal:** `CROSS_ORIGIN_REDIRECT` / `DESTINATION_MISMATCH` decisions.

**Severity:** P2 (blocked by default); P1 if the agent acted on the new
origin before re-evaluation.

**Contain:**

1. The navigation was denied — verify the browser session origin did not change.
2. Quarantine the run if any action executed against the new origin.

**Recover:**

3. The user may explicitly authorize the new origin; the destination check
   then passes with the updated intent and the flow continues.
4. Pin the redirect chain as a fixture (`redirect` vector) in the corpus.
