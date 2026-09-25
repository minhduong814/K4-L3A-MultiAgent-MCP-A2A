# L3A Architecture Record

## 1. System overview

```text
inputs/<case_id>.json
        |
        v
  Coordinator ------------------------------------------+
        |                                                |
        +--> Order agent ---- get_order/items -----------+
        +--> Payment agent -- payment/refund timeline ---+--> Verifier
        +--> Shipment agent - shipment summary ----------+       |
        +--> Policy agent --- get_policy ----------------+       v
                                                        output + trace
```

The workflow is deterministic. Customer claims are routing hints, not facts: the
selected specialists retrieve authoritative evidence and `_assess_issue` verifies
the claim against order status and lifecycle events. Policy controls status, action,
responsibility type, and refund amount. The verifier checks cross-field invariants
before the coordinator returns an output.

The CLI writes each case to temporary output and trace files and commits them only
after the full case passes validation. A dropped MCP stream reconnects and resumes
at the first uncommitted case, with four bounded attempts.

## 2. Agent ownership

| Actor | Input | Responsibility | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case, claimed order, claim topics | Validate input, route relevant domain tasks, assemble result | Specialist tasks; candidate to verifier |
| Order agent | order ID | Call `get_order`; call `get_order_items` when item value or seller identity is material | Order state, affected item/seller IDs |
| Payment agent | order ID | Call `get_payment_timeline`; call `get_refund_timeline` only for refund cases | Payment and refund lifecycle facts |
| Shipment agent | order ID | Call `get_shipment_summary` only for delivery or unsupported-claim checks | Delivery timeline and accountable actor |
| Policy agent | policy version | Call `get_policy` and apply the rule for the verified issue | Status, action, party type, refund amount |
| Verifier | candidate and evidence references | Check evidence uniqueness, refund totals, and status/refund compatibility | `INVARIANTS_PASSED` or an exception |

Each specialist can call only its listed tools. `EvidenceGateway` accepts only tools
returned by MCP discovery.

## 3. A2A protocol

The observable message envelope is the public trace event. Every message is scoped
by `case_id`, identifies `actor` and optional `target`, and uses a stable decision
code rather than private reasoning text.

The handoff sequence is:

1. coordinator emits `task_assigned`;
2. specialist emits `tool_result_consumed` with the returned evidence reference;
3. policy agent emits `policy_decided`;
4. coordinator emits one `handoff` to verifier;
5. verifier emits `verification_completed`;
6. CLI emits `case_finalized` after output validation.

Tasks form a fixed acyclic route, so agents cannot hand work back recursively.
MCP transport timeout is 300 seconds, and reconnect attempts use bounded backoff.

## 4. Evidence lifecycle

The gateway validates every response against `mcp-evidence-response-v1` before the
workflow reads it. Evidence remains in a case-local dictionary and is never cached
or reused across cases. The original `evidence_ref` is copied without modification
to both `tool_result_consumed` and the final output.

Queries are claim-sensitive to reduce irrelevant evidence. For example, delivery
cases use order, shipment, and policy evidence; refund cases use order, payment,
refund, and policy evidence. Conflicting duplicate records are retained as declared
`data_conflicts`; selection uses alignment with the authoritative order timeline.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP connection/stream failure | Yes, at most four attempts for the unfinished case | Reconnect and resume last committed checkpoint | Uncommitted temporary trace is discarded |
| Tool not discovered | No | Stop run; do not fabricate evidence | No finalized case |
| Not found/invalid envelope | No | Stop run for correction or rerun | No finalized case |
| Source conflict | No tool retry | Select timeline-aligned source and declare conflict | `verification_completed`, conflict count |
| Invalid specialist result | No | Raise validation error | No finalized case |

No missing evidence is converted into guessed data or a synthetic evidence reference.

## 6. Verification invariants

Before finalize, the implementation checks:

- output schema and `case_id` match;
- every evidence reference is unique and came from the current case's MCP calls;
- affected entity IDs occur in scoped evidence;
- policy rule exists for the evidence-verified issue;
- refund lines sum exactly to `recommended_refund_brl`;
- `no_action` never carries a positive refund;
- seller responsibility uses a seller ID found in item/shipment evidence;
- actions, status, party type, and amount come from the same policy rule;
- confidence values stay within schema bounds;
- every completed case has receive, assignment, handoff, verification, and finalize events.

## 7. Reproducibility

- Python: 3.11 or newer (validated with the repository virtual environment)
- Dependencies: bounded in `pyproject.toml`
- Model: none; deterministic rules over MCP evidence
- Randomness: none in decisions; trace event IDs use secure random IDs as required
- Concurrency: one case at a time to preserve trace ordering and server stability
- Commands: `day09 validate-inputs`, `day09 run`, `day09 validate`, and
  `day09 package --output dist/submission.zip`
- Secrets: loaded from `.env`; never copied into source, outputs, trace, or package
