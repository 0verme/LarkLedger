"""P46 — Query contract schema tests.

These prove the deterministic contract layer before any service logic:

* valid intents are accepted and keep Decimal / timezone semantics intact;
* unknown fields — including SQL-shaped ones — are rejected (``extra=forbid``);
* there is no ``merchant`` / ``category_group`` / ``custom_filter`` escape hatch
  (such concepts do not exist in the data model and must never be guessed);
* naive datetimes, inverted ranges, invalid amount bounds, oversized pages and
  invalid grouping combinations are rejected at construction time — i.e. before
  anything reaches a query executor.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from lark_ledger.models import Direction
from lark_ledger.query_schemas import (
    MAX_QUERY_PAGE_SIZE,
    MAX_QUERY_TOP_N,
    QueryGrouping,
    QueryIntent,
    QueryMode,
    QueryOrder,
    QueryResult,
    QuerySort,
    QueryStatus,
)


def _intent(**overrides) -> QueryIntent:
    base = {"mode": QueryMode.LIST}
    base.update(overrides)
    return QueryIntent(**base)


def test_valid_list_intent_defaults() -> None:
    intent = QueryIntent()
    assert intent.mode is QueryMode.LIST
    assert intent.page == 1
    assert intent.page_size == 25
    assert intent.sort is QuerySort.OCCURRED_AT
    assert intent.order is QueryOrder.DESC


def test_valid_full_intent_accepted() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    intent = QueryIntent(
        mode=QueryMode.LIST,
        start=start,
        end=end,
        direction=Direction.EXPENSE,
        category="餐饮",
        account="招商银行",
        min_amount=Decimal("0.10"),
        max_amount=Decimal("500.00"),
        keyword="午饭",
        sort=QuerySort.AMOUNT,
        order=QueryOrder.ASC,
        page=2,
        page_size=50,
    )
    assert intent.min_amount == Decimal("0.10")
    assert intent.category == "餐饮"
    assert intent.account == "招商银行"


def test_valid_group_top_n_accepted() -> None:
    intent = QueryIntent(
        mode=QueryMode.GROUP,
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 9, 1, tzinfo=UTC),
        grouping=QueryGrouping.CATEGORY,
        top_n=5,
    )
    assert intent.top_n == 5


def test_decimal_is_exact_not_float() -> None:
    intent = _intent(min_amount=Decimal("0.10"))
    assert intent.min_amount == Decimal("0.10")
    assert isinstance(intent.min_amount, Decimal)


def test_strip_whitespace_names() -> None:
    intent = _intent(category="  餐饮  ", account="  招商银行 ")
    assert intent.category == "餐饮"
    assert intent.account == "招商银行"


# --------------------------------------------------------------------------- #
# Unknown / SQL-shaped fields are rejected
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        {"sql": "select * from entries"},
        {"where": "1=1"},
        {"expression": "amount > 100"},
        {"raw_where": "1=1"},
        {"raw_sql": "select 1"},
        {"custom_filter": {"field": "note", "op": "contains"}},
        # Concepts that do not exist in the data model must never be mapped to
        # note/category silently.
        {"merchant": "星巴克"},
        {"category_group": "交通"},
        {"query_intent_extra": True},
    ],
)
def test_unknown_and_sql_shaped_fields_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        QueryIntent(**payload)


def test_merchant_cannot_be_silently_mapped() -> None:
    # A merchant-shaped query must be a controlled schema rejection, not a
    # back-door into note search.
    with pytest.raises(ValidationError):
        QueryIntent(mode=QueryMode.LIST, merchant="星巴克")


# --------------------------------------------------------------------------- #
# Time semantics
# --------------------------------------------------------------------------- #


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(
            start=datetime(2026, 8, 1),
            end=datetime(2026, 9, 1),
        )


def test_one_sided_range_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(start=datetime(2026, 8, 1, tzinfo=UTC))


def test_inverted_range_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(
            start=datetime(2026, 9, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(timezone="Not/A_Zone")


# --------------------------------------------------------------------------- #
# Amount bounds
# --------------------------------------------------------------------------- #


def test_negative_min_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        _intent(min_amount=Decimal("-1"))


def test_non_positive_max_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        _intent(max_amount=Decimal("0"))


def test_min_gt_max_rejected() -> None:
    with pytest.raises(ValidationError):
        _intent(min_amount=Decimal("100"), max_amount=Decimal("50"))


# --------------------------------------------------------------------------- #
# Page size / Top N caps
# --------------------------------------------------------------------------- #


def test_page_size_capped() -> None:
    with pytest.raises(ValidationError):
        _intent(page_size=MAX_QUERY_PAGE_SIZE + 1)
    with pytest.raises(ValidationError):
        _intent(page_size=0)


def test_top_n_capped() -> None:
    with pytest.raises(ValidationError):
        _intent(top_n=MAX_QUERY_TOP_N + 1)


# --------------------------------------------------------------------------- #
# Grouping / mode combinations
# --------------------------------------------------------------------------- #


def test_top_n_requires_grouping() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(mode=QueryMode.LIST, top_n=5)


def test_grouping_requires_group_mode() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(mode=QueryMode.AGGREGATE, grouping=QueryGrouping.CATEGORY)


def test_group_mode_requires_grouping() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(
            mode=QueryMode.GROUP,
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_aggregate_requires_range() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(mode=QueryMode.AGGREGATE)


def test_group_requires_range() -> None:
    with pytest.raises(ValidationError):
        QueryIntent(
            mode=QueryMode.GROUP, grouping=QueryGrouping.CATEGORY
        )


def test_list_range_optional() -> None:
    assert QueryIntent(mode=QueryMode.LIST).start is None


# --------------------------------------------------------------------------- #
# Result contract
# --------------------------------------------------------------------------- #


def test_query_result_statuses_are_typed() -> None:
    assert {s.value for s in QueryStatus} == {
        "ok",
        "empty",
        "invalid",
        "needs_clarification",
        "unsupported",
    }


def test_query_result_is_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        QueryResult(status=QueryStatus.OK, mode=QueryMode.LIST, bogus_field=1)
