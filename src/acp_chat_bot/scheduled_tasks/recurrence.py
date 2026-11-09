"""Cron recurrence helpers for deferred Telegram schedules."""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import CroniterError, croniter

CRON_EXPRESSION_MAX_CHARS = 120
EMPTY_CRON_EXPRESSION_ERROR = "cron expression must not be empty"
CRON_EXPRESSION_TOO_LONG_ERROR = f"cron expression must be at most {CRON_EXPRESSION_MAX_CHARS} characters"
NAIVE_CRON_BASE_ERROR = "cron base time must be timezone-aware"


class InvalidCronExpressionError(ValueError):
    """Raised when a cron expression cannot produce the next occurrence."""


def normalize_cron_expression(raw: str) -> str:
    """Return a whitespace-normalized cron expression suitable for persistence."""

    normalized = " ".join(raw.split())
    if not normalized:
        raise InvalidCronExpressionError(EMPTY_CRON_EXPRESSION_ERROR)
    if len(normalized) > CRON_EXPRESSION_MAX_CHARS:
        raise InvalidCronExpressionError(CRON_EXPRESSION_TOO_LONG_ERROR)
    return normalized


def next_occurrence(expression: str, *, after: datetime) -> datetime:
    """Return the first cron occurrence strictly after *after*, in UTC.

    The six-field form keeps seconds in the last field, matching croniter's default.
    """

    if after.tzinfo is None:
        raise InvalidCronExpressionError(NAIVE_CRON_BASE_ERROR)
    normalized = normalize_cron_expression(expression)
    base = after.astimezone(UTC).replace(microsecond=0)
    try:
        result = croniter(normalized, base).get_next(datetime)
    except (CroniterError, ValueError) as exc:
        raise InvalidCronExpressionError(f"invalid cron expression: {normalized!r}") from exc  # noqa: TRY003
    return result.astimezone(UTC)
