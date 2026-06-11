import pytest

from app.domain.states import (
    _ALLOWED,
    IllegalTransition,
    LegStatus,
    OrderStatus,
    aggregate_order_status,
    assert_transition,
)


def test_legal_transitions() -> None:
    assert_transition(OrderStatus.ACCEPTED, OrderStatus.SUBMITTED)
    assert_transition(OrderStatus.SUBMITTED, OrderStatus.RESULT_UNKNOWN)
    assert_transition(OrderStatus.RESULT_UNKNOWN, OrderStatus.SUCCEEDED)
    assert_transition(OrderStatus.SUCCEEDED, OrderStatus.PARTIALLY_REVERSED)
    assert_transition(OrderStatus.PARTIALLY_REVERSED, OrderStatus.REVERSED)


@pytest.mark.parametrize(
    "src,dst",
    [
        (OrderStatus.SUCCEEDED, OrderStatus.FAILED),
        (OrderStatus.FAILED, OrderStatus.SUCCEEDED),
        (OrderStatus.ACCEPTED, OrderStatus.SUCCEEDED),
        (OrderStatus.REVERSED, OrderStatus.PROCESSING),
    ],
)
def test_illegal_transitions(src, dst) -> None:
    with pytest.raises(IllegalTransition):
        assert_transition(src, dst)


# ── 穷举 _ALLOWED 每个 src 的全部合法 dst ──────────────────────────────────────


def _all_legal_pairs() -> list[tuple[OrderStatus, OrderStatus]]:
    pairs = []
    for src, dsts in _ALLOWED.items():
        for dst in dsts:
            pairs.append((src, dst))
    return pairs


@pytest.mark.parametrize("src,dst", _all_legal_pairs())
def test_all_allowed_transitions_pass(src: OrderStatus, dst: OrderStatus) -> None:
    assert_transition(src, dst)  # must not raise


# ── 每个终态（允许集为空）的任意转移都非法 ──────────────────────────────────────

_TERMINAL_STATES = [
    OrderStatus.FAILED,
    OrderStatus.EXPIRED,
    OrderStatus.CANCELLED,
    OrderStatus.REVERSED,
]

# 每个终态取第一个其他状态作为 dst，断言非法即可覆盖"任意转移"要求
_terminal_illegal_pairs = [
    (terminal, dst)
    for terminal in _TERMINAL_STATES
    for dst in [next(s for s in OrderStatus if s != terminal)]
]


@pytest.mark.parametrize("src,dst", _terminal_illegal_pairs)
def test_terminal_states_no_outgoing(src: OrderStatus, dst: OrderStatus) -> None:
    with pytest.raises(IllegalTransition):
        assert_transition(src, dst)


# ── aggregate_order_status ──────────────────────────────────────────────────


def test_aggregate_empty_legs_returns_processing() -> None:
    """空 legs 列表视为 SUBMITTED 后拆 leg 期间在途 → PROCESSING（spec §6 兜底）。"""
    assert aggregate_order_status([]) == OrderStatus.PROCESSING


def test_aggregate_all_success() -> None:
    assert aggregate_order_status([(LegStatus.SUCCESS, "100")]) == OrderStatus.SUCCEEDED


def test_aggregate_unknown_dominates() -> None:
    assert (
        aggregate_order_status([(LegStatus.SUCCESS, "100"), (LegStatus.UNKNOWN, "50")])
        == OrderStatus.RESULT_UNKNOWN
    )


def test_aggregate_pending_is_processing() -> None:
    assert (
        aggregate_order_status([(LegStatus.SUCCESS, "100"), (LegStatus.PENDING, "50")])
        == OrderStatus.PROCESSING
    )


def test_aggregate_failed_no_inflight() -> None:
    assert (
        aggregate_order_status([(LegStatus.FAILED, "100"), (LegStatus.SUCCESS, "50")])
        == OrderStatus.FAILED
    )


def test_aggregate_partial_reversal() -> None:
    legs = [
        (LegStatus.SUCCESS, "100"),
        (LegStatus.SUCCESS, "50"),
        (LegStatus.REVERSED, "50"),
        (LegStatus.REVERSAL, "50"),
    ]
    assert aggregate_order_status(legs) == OrderStatus.PARTIALLY_REVERSED


def test_aggregate_full_reversal() -> None:
    legs = [(LegStatus.REVERSED, "100"), (LegStatus.REVERSAL, "100")]
    assert aggregate_order_status(legs) == OrderStatus.REVERSED


def test_aggregate_reversal_exact_coverage_is_full() -> None:
    """REVERSAL 金额恰好等于 SUCCESS+REVERSED 总额 → REVERSED（等于算全额冲正）。"""
    legs = [(LegStatus.SUCCESS, "200"), (LegStatus.REVERSAL, "200")]
    assert aggregate_order_status(legs) == OrderStatus.REVERSED


def test_aggregate_unknown_dominates_over_pending() -> None:
    """UNKNOWN 优先级高于 PENDING。"""
    legs = [(LegStatus.PENDING, "50"), (LegStatus.UNKNOWN, "50")]
    assert aggregate_order_status(legs) == OrderStatus.RESULT_UNKNOWN


def test_aggregate_multi_success_legs() -> None:
    legs = [(LegStatus.SUCCESS, "100"), (LegStatus.SUCCESS, "200")]
    assert aggregate_order_status(legs) == OrderStatus.SUCCEEDED


def test_aggregate_only_failed_legs() -> None:
    legs = [(LegStatus.FAILED, "100"), (LegStatus.FAILED, "200")]
    assert aggregate_order_status(legs) == OrderStatus.FAILED
