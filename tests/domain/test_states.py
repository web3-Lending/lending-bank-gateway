import pytest

from app.domain.states import (
    _ALLOWED,
    IllegalTransition,
    OrderStatus,
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
        # 注：ACCEPTED→SUCCEEDED 同步优先 V2 后已合法（≤5s 同步终态），移出非法集
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
