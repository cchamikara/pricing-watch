import pytest
from hypothesis import given
from hypothesis import strategies as st

from pricing_watch.money import Cents, ex_from_inc, inc_from_ex

amounts = st.integers(min_value=0, max_value=10**9).map(Cents)


@given(amounts)
def test_inc_ex_inc_round_trip_within_one_cent(inc: Cents) -> None:
    assert abs(inc_from_ex(ex_from_inc(inc)) - inc) <= 1


@given(amounts)
def test_ex_inc_ex_round_trip_within_one_cent(ex: Cents) -> None:
    assert abs(ex_from_inc(inc_from_ex(ex)) - ex) <= 1


@pytest.mark.parametrize(
    ("inc", "ex"),
    [(0, 0), (110, 100), (18900, 17182), (1, 1), (5, 5), (6, 5)],
)
def test_ex_from_inc_known_values(inc: int, ex: int) -> None:
    assert ex_from_inc(Cents(inc)) == ex


def test_half_cent_rounds_up() -> None:
    assert inc_from_ex(Cents(5)) == 6  # 5.5 cents


def test_negative_rejected() -> None:
    with pytest.raises(ValueError):
        ex_from_inc(Cents(-1))
