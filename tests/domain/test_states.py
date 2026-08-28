import pytest

from app.domain.states import (
    _ALLOWED,
    WEDAP_DOOR_REJECT_HTTP_STATUSES,
    IllegalTransition,
    OrderStatus,
    assert_transition,
    is_door_reject_http_status,
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


# ── 通用交易的「零资金变动」证据白名单（v2.2 §8.2 NOT_APPLIED 的证据门）──────────


@pytest.mark.parametrize("status", sorted(WEDAP_DOOR_REJECT_HTTP_STATUSES))
def test_registered_door_reject_statuses_are_evidence(status: int) -> None:
    """在册状态 = wedap 在 HTTP 层挡住请求，未进业务引擎。"""
    assert is_door_reject_http_status(status) is True


@pytest.mark.parametrize("status", [None, 200, 202, 408, 409, 423, 425, 429, 451, 500, 502, 504])
def test_unregistered_statuses_are_not_evidence(status: int | None) -> None:
    """白名单外一律不是证据——尤其：

    - ``None`` / 2xx：HTTP 200 响应体里的业务码（含 envelope 漂移的 ``code="None"``），
      语义由 wedap 码表定义，通用侧没有在册码表可查；
    - 408/409/429：可能已部分执行、可能已存在同键交易、限流语义不保证未执行；
    - 5xx：结果未知，本仓一贯判 RESULT_UNKNOWN。
    """
    assert is_door_reject_http_status(status) is False


def test_door_reject_whitelist_excludes_ambiguous_4xx() -> None:
    """把 408/409/429 写进白名单 = 把「可能已执行」当成「确认未执行」，本用例先红。"""
    assert not ({408, 409, 429} & WEDAP_DOOR_REJECT_HTTP_STATUSES)
