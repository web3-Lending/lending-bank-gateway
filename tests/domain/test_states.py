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
    assert_transition(
        OrderStatus.ACCEPTED, OrderStatus.RESULT_UNKNOWN
    )  # 外呼超时发生在 ACCEPTED→SUBMITTED 之间
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


# ── saga 补偿场景：FAILED 优先于冲正判定 ──────────────────────────────────────


def test_aggregate_saga_failed_with_reversal_and_reversed_is_failed() -> None:
    """FAILED leg + REVERSAL leg + REVERSED leg 共存（saga 中途失败+已补偿）→ FAILED。

    spec §6：业务失败优先于冲正语义，补偿净额归零不改变"单子失败"结论。
    """
    legs = [
        (LegStatus.FAILED, "60"),
        (LegStatus.REVERSED, "60"),
        (LegStatus.REVERSAL, "60"),
    ]
    assert aggregate_order_status(legs) == OrderStatus.FAILED


def test_aggregate_saga_failed_with_reversal_only_is_failed() -> None:
    """FAILED leg + REVERSAL leg 共存（无对应 REVERSED leg）→ FAILED。"""
    legs = [
        (LegStatus.FAILED, "60"),
        (LegStatus.REVERSAL, "60"),
    ]
    assert aggregate_order_status(legs) == OrderStatus.FAILED


def test_aggregate_reversal_without_source_leg_raises() -> None:
    """纯 REVERSAL 无任何 SUCCESS/REVERSED 源 leg → 畸形数据，必须 ValueError。"""
    with pytest.raises(ValueError, match="malformed legs"):
        aggregate_order_status([(LegStatus.REVERSAL, "100")])


# ── REVERSED without REVERSAL 防御 ────────────────────────────────────────────


def test_aggregate_reversed_without_reversal_raises() -> None:
    """纯 REVERSED leg 无任何 REVERSAL leg → 畸形数据，必须 ValueError。

    REVERSED 状态表示"已被冲正"，没有配套的 REVERSAL（冲正动作）leg 是无效数据。
    """
    with pytest.raises(ValueError, match="malformed legs: REVERSED without REVERSAL"):
        aggregate_order_status([(LegStatus.REVERSED, "100")])


def test_aggregate_success_and_reversed_without_reversal_raises() -> None:
    """SUCCESS + REVERSED leg 但无 REVERSAL → 畸形数据，必须 ValueError。"""
    with pytest.raises(ValueError, match="malformed legs: REVERSED without REVERSAL"):
        aggregate_order_status([(LegStatus.SUCCESS, "50"), (LegStatus.REVERSED, "50")])
