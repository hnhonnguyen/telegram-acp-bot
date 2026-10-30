from datetime import UTC, datetime, timedelta, timezone

import pytest

from telegram_acp_bot.scheduled_tasks.recurrence import (
    CRON_EXPRESSION_MAX_CHARS,
    InvalidCronExpressionError,
    next_occurrence,
    normalize_cron_expression,
)

BASE = datetime(2026, 3, 30, 12, 0, 0, tzinfo=UTC)


def test_normalize_cron_expression_collapses_whitespace():
    assert normalize_cron_expression("  0   9  *  *  *  ") == "0 9 * * *"


def test_normalize_cron_expression_rejects_empty():
    with pytest.raises(InvalidCronExpressionError, match="must not be empty"):
        normalize_cron_expression("   ")


def test_normalize_cron_expression_rejects_too_long():
    with pytest.raises(InvalidCronExpressionError, match=f"at most {CRON_EXPRESSION_MAX_CHARS}"):
        normalize_cron_expression("0 " * (CRON_EXPRESSION_MAX_CHARS // 2 + 1))


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("*/15 * * * *", datetime(2026, 3, 30, 12, 15, tzinfo=UTC)),
        ("0 9 * * *", datetime(2026, 3, 31, 9, 0, tzinfo=UTC)),
        ("0 0 * * * 30", datetime(2026, 3, 31, 0, 0, 30, tzinfo=UTC)),
    ],
)
def test_next_occurrence_returns_utc_schedule(expression: str, expected: datetime):
    result = next_occurrence(expression, after=BASE)

    assert result == expected
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_next_occurrence_is_strictly_after_an_exact_occurrence():
    exact = datetime(2026, 3, 30, 13, 0, 0, tzinfo=UTC)

    assert next_occurrence("0 * * * *", after=exact) == datetime(2026, 3, 30, 14, 0, tzinfo=UTC)


def test_next_occurrence_supports_second_granularity():
    exact = datetime(2026, 3, 30, 12, 0, 0, tzinfo=UTC)

    assert next_occurrence("* * * * * *", after=exact) == datetime(2026, 3, 30, 12, 0, 1, tzinfo=UTC)


def test_next_occurrence_converts_aware_base_to_utc():
    local_base = datetime(2026, 3, 30, 19, 0, tzinfo=timezone(timedelta(hours=7)))

    assert next_occurrence("0 13 * * *", after=local_base) == datetime(2026, 3, 30, 13, 0, tzinfo=UTC)


def test_next_occurrence_rejects_naive_base():
    with pytest.raises(InvalidCronExpressionError, match="timezone-aware"):
        next_occurrence("0 9 * * *", after=datetime(2026, 3, 30, 12, 0))


@pytest.mark.parametrize("expression", ["not a cron", "* * *", "61 * * * *", "0 0 31 2 *"])
def test_next_occurrence_rejects_invalid_expressions(expression: str):
    with pytest.raises(InvalidCronExpressionError, match="invalid cron expression"):
        next_occurrence(expression, after=BASE)
