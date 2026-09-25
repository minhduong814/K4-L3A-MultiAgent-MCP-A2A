from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one case using scoped MCP evidence and deterministic specialists."""
    case_id = _required_string(case, "case_id")
    request = case.get("customer_request")
    if not isinstance(request, dict):
        raise ValueError(f"{case_id}: customer_request must be an object")
    order_id = _required_string(request, "claimed_order_id")
    policy_version = _required_string(case, "policy_version")
    claims = request.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError(f"{case_id}: claims must be a non-empty array")
    claimed_issue = _required_string(claims[0], "topic")
    if claimed_issue not in PRIMARY_ISSUES:
        claimed_issue = "unsupported_claim"

    evidence: dict[str, dict[str, Any]] = {}

    async def delegate(actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=f"QUERY_{tool_name.upper()}",
        )
        result = await gateway.call(tool_name, case_id=case_id, **arguments)
        evidence[tool_name] = result
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[result["evidence_ref"]],
        )
        return result

    order_ev = await delegate("order-agent", "get_order", order_id=order_id)
    order = _object_data(order_ev, "get_order")

    # Only query domains capable of proving or disproving the submitted claim. This
    # keeps the final evidence set precise while still covering every material fact.
    need_items = claimed_issue in {"unavailable_order_paid", "valid_split_payment"}
    need_payment = claimed_issue not in {"late_delivery_seller", "late_delivery_logistics"}
    need_shipment = claimed_issue in {
        "late_delivery_seller",
        "late_delivery_logistics",
        "unsupported_claim",
    }
    need_refund = claimed_issue in {"refund_pending", "refund_failed"}

    if need_items:
        await delegate("order-agent", "get_order_items", order_id=order_id)
    if need_payment:
        await delegate("payment-agent", "get_payment_timeline", order_id=order_id)
    if need_shipment:
        await delegate("shipment-agent", "get_shipment_summary", order_id=order_id)
    if need_refund:
        await delegate("payment-agent", "get_refund_timeline", order_id=order_id)
    policy_ev = await delegate("policy-agent", "get_policy", policy_version=policy_version)

    issue = _assess_issue(claimed_issue, order, evidence)
    policy = _object_data(policy_ev, "get_policy")
    rules = policy.get("rules")
    if not isinstance(rules, dict) or not isinstance(rules.get(issue), dict):
        raise ValueError(f"{case_id}: policy has no rule for {issue}")
    rule = rules[issue]
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=str(rule.get("recommended_action", "POLICY_RULE_APPLIED")),
        evidence_refs=[policy_ev["evidence_ref"]],
    )

    entities = _affected_entities(order_id, order, evidence)
    responsible_parties = _responsible_parties(rule, issue, entities)
    refund = _money(rule.get("refund_brl", 0))
    action = str(rule.get("recommended_action", "document_no_action"))
    case_status = str(rule.get("case_status", "needs_investigation"))
    refs = [value["evidence_ref"] for value in evidence.values()]
    claim_assessments = _claim_assessments(claims, issue, refs)
    conflicts = _data_conflicts(order, evidence)

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": issue,
                "amount_brl": float(refund),
                "entity_id": order_id,
            }
        )

    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": 0.99,
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": str(policy.get("currency", "BRL")),
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }
    _verify_output(output)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="SPECIALIST_RESULTS_READY",
        evidence_refs=refs,
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="INVARIANTS_PASSED",
        evidence_refs=refs,
        attributes={"evidence_count": len(refs), "conflict_count": len(conflicts)},
    )
    return output


PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}


def _required_string(value: Any, key: str) -> str:
    if not isinstance(value, dict) or not isinstance(value.get(key), str) or not value[key]:
        raise ValueError(f"missing non-empty string: {key}")
    return value[key]


def _object_data(evidence: dict[str, Any], tool_name: str) -> dict[str, Any]:
    data = evidence.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{tool_name} returned non-object data")
    return data


def _list_data(evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not evidence or not isinstance(evidence.get("data"), list):
        return []
    return [row for row in evidence["data"] if isinstance(row, dict)]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _near_purchase(event: dict[str, Any], order: dict[str, Any], days: int = 3) -> bool:
    event_at = _parse_time(event.get("event_at"))
    purchased_at = _parse_time(order.get("order_purchase_timestamp"))
    if event_at is None or purchased_at is None:
        return False
    return abs((event_at - purchased_at).total_seconds()) <= days * 86_400


def _payment_data(evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    value = evidence.get("get_payment_timeline", {}).get("data", {})
    return value if isinstance(value, dict) else {}


def _assess_issue(
    claimed_issue: str, order: dict[str, Any], evidence: dict[str, dict[str, Any]]
) -> str:
    """Verify the routed claim against authoritative domain signatures."""
    status = order.get("order_status")
    payment = _payment_data(evidence)
    current_payment_events = [
        event
        for event in payment.get("events", [])
        if isinstance(event, dict) and _near_purchase(event, order)
    ]
    captured = [event for event in current_payment_events if event.get("event_type") == "captured"]

    if claimed_issue == "canceled_order_paid" and status == "canceled" and captured:
        return claimed_issue
    if claimed_issue == "unavailable_order_paid" and status == "unavailable" and captured:
        return claimed_issue
    if claimed_issue == "payment_mismatch" and any(
        event.get("event_type") == "reconciliation_mismatch" for event in current_payment_events
    ):
        return claimed_issue
    if claimed_issue in {"refund_pending", "refund_failed"}:
        refund = evidence.get("get_refund_timeline", {}).get("data", {})
        events = refund.get("events", []) if isinstance(refund, dict) else []
        if any(
            isinstance(event, dict) and event.get("status") == claimed_issue[7:]
            for event in events
        ):
            return claimed_issue
    if claimed_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        shipment = evidence.get("get_shipment_summary", {}).get("data", {})
        actor = claimed_issue.removeprefix("late_delivery_")
        events = shipment.get("events", []) if isinstance(shipment, dict) else []
        if any(isinstance(event, dict) and event.get("actor") == actor for event in events):
            return claimed_issue
        delivered = _parse_time(order.get("order_delivered_customer_date"))
        estimated = _parse_time(order.get("order_estimated_delivery_date"))
        if delivered and estimated and delivered > estimated:
            return claimed_issue
    if claimed_issue in {"valid_split_payment", "duplicate_charge"} and len(captured) >= 2:
        return claimed_issue
    if claimed_issue == "unsupported_claim":
        return claimed_issue
    return "unsupported_claim"


def _affected_entities(
    order_id: str, order: dict[str, Any], evidence: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    items = _list_data(evidence.get("get_order_items"))
    shipment = evidence.get("get_shipment_summary", {}).get("data", {})
    limits = shipment.get("shipping_limits", []) if isinstance(shipment, dict) else []
    entity_rows = [*items, *(row for row in limits if isinstance(row, dict))]
    return {
        "order_ids": [order_id],
        "item_ids": _unique_strings(row.get("order_item_id") for row in entity_rows),
        "seller_ids": _unique_strings(row.get("seller_id") for row in entity_rows),
        # The public evidence payload exposes payment sequence numbers and shipment
        # timestamps, but no authoritative IDs for either entity type.
        "payment_references": [],
        "shipment_ids": [],
    }


def _unique_strings(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _responsible_parties(
    rule: dict[str, Any], issue: str, entities: dict[str, list[str]]
) -> list[dict[str, Any]]:
    raw = rule.get("responsible_parties", [])
    parties = [dict(item) for item in raw if isinstance(item, dict)]
    if issue in {"late_delivery_seller", "unavailable_order_paid"} and entities["seller_ids"]:
        return [{"party_type": "seller", "party_id": entities["seller_ids"][0]}]
    normalized = []
    for party in parties:
        party_type = party.get("party_type")
        if isinstance(party_type, str):
            normalized.append({"party_type": party_type, "party_id": party.get("party_id")})
    return normalized or [{"party_type": "unknown", "party_id": None}]


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid monetary value: {value!r}") from exc
    if amount < 0:
        raise ValueError("refund amount cannot be negative")
    return amount.quantize(Decimal("0.01"))


def _claim_assessments(
    claims: list[Any], issue: str, evidence_refs: list[str]
) -> list[dict[str, Any]]:
    results = []
    for raw in claims[:5]:
        if not isinstance(raw, dict) or not isinstance(raw.get("claim_id"), str):
            continue
        topic = raw.get("topic")
        if topic == "requested_full_refund":
            if issue in {"canceled_order_paid", "unavailable_order_paid", "refund_failed"}:
                verdict = "supported"
            elif issue == "refund_pending":
                verdict = "insufficient_evidence"
            elif issue in {
                "late_delivery_seller",
                "late_delivery_logistics",
                "duplicate_charge",
                "payment_mismatch",
            }:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif issue == "unsupported_claim":
            verdict = "unsupported"
        else:
            verdict = "supported" if topic == issue else "unsupported"
        results.append(
            {
                "claim_id": raw["claim_id"],
                "verdict": verdict,
                "confidence": 0.98,
                "evidence_refs": evidence_refs,
            }
        )
    return results


def _data_conflicts(
    order: dict[str, Any], evidence: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    items = _list_data(evidence.get("get_order_items"))
    _append_duplicate_conflicts(
        conflicts, items, "order_item_id", "shipping_limit_date", "order_items"
    )
    payments = _payment_data(evidence).get("payments", [])
    if isinstance(payments, list):
        _append_duplicate_conflicts(
            conflicts,
            [row for row in payments if isinstance(row, dict)],
            "payment_sequential",
            "payment_value",
            "payment_timeline",
        )
    shipment = evidence.get("get_shipment_summary", {}).get("data", {})
    if isinstance(shipment, dict):
        limits = shipment.get("shipping_limits", [])
        if isinstance(limits, list):
            _append_duplicate_conflicts(
                conflicts,
                [row for row in limits if isinstance(row, dict)],
                "order_item_id",
                "shipping_limit_at",
                "shipment_summary",
            )
        delivered = _parse_time(order.get("order_delivered_customer_date"))
        estimated = _parse_time(order.get("order_estimated_delivery_date"))
        events = shipment.get("events", [])
        if delivered and estimated and delivered <= estimated and isinstance(events, list) and any(
            isinstance(event, dict) and event.get("event_type") == "delivered_late"
            for event in events
        ):
            conflicts.append(
                {
                    "field": "shipment.delivery_status",
                    "sources": ["order.delivery_timestamps", "shipment.events"],
                    "selected_source": "order.delivery_timestamps",
                    "resolution_code": "ORDER_TIMELINE_ALIGNMENT",
                }
            )
    return conflicts[:5]


def _append_duplicate_conflicts(
    conflicts: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    key: str,
    field: str,
    source: str,
) -> None:
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        identity = row.get(key)
        if isinstance(identity, str):
            grouped.setdefault(identity, []).append(row.get(field))
    for identity, values in grouped.items():
        distinct = list(dict.fromkeys(value for value in values if value is not None))
        if len(distinct) > 1:
            conflicts.append(
                {
                    "field": f"{source}.{identity}.{field}"[:100],
                    "sources": [f"{source}.row_{index + 1}" for index in range(len(distinct))][
                        :5
                    ],
                    "selected_source": f"{source}.timeline_aligned_row",
                    "resolution_code": "ORDER_TIMELINE_ALIGNMENT",
                }
            )


def _verify_output(output: dict[str, Any]) -> None:
    financial = output["financial_resolution"]
    line_total = sum(Decimal(str(line["amount_brl"])) for line in financial["refund_lines"])
    recommended = Decimal(str(financial["recommended_refund_brl"]))
    if line_total != recommended:
        raise ValueError("refund lines do not equal the recommended refund")
    if output["assessment"]["case_status"] == "no_action" and recommended != 0:
        raise ValueError("no_action cannot recommend a positive refund")
    if len(output["evidence_refs"]) != len(set(output["evidence_refs"])):
        raise ValueError("duplicate evidence references")
