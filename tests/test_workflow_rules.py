from decimal import Decimal

import pytest

from student_agent.workflow import _money, _verify_output


def test_money_is_normalized_to_brl_cents() -> None:
    assert _money("18") == Decimal("18.00")
    assert _money(35.0) == Decimal("35.00")


def test_money_rejects_negative_refund() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        _money("-0.01")


def test_verifier_rejects_refund_total_mismatch() -> None:
    output = {
        "assessment": {"case_status": "action_required"},
        "evidence_refs": ["ev_example"],
        "financial_resolution": {
            "recommended_refund_brl": 20,
            "refund_lines": [{"amount_brl": 10}],
        },
    }
    with pytest.raises(ValueError, match="refund lines"):
        _verify_output(output)


def test_verifier_rejects_refund_for_no_action() -> None:
    output = {
        "assessment": {"case_status": "no_action"},
        "evidence_refs": ["ev_example"],
        "financial_resolution": {
            "recommended_refund_brl": 10,
            "refund_lines": [{"amount_brl": 10}],
        },
    }
    with pytest.raises(ValueError, match="no_action"):
        _verify_output(output)
