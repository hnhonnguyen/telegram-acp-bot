"""Flatten an ACP form elicitation into the choices the transport can offer as buttons.

A form elicitation is the agent's structured version of "which one do you want": a JSON schema whose
fields each enumerate the answers the agent will accept. Only fields that really are a choice between
known options can become buttons, so anything free-form is reported as unsupported instead of guessed.
"""

from __future__ import annotations

from collections.abc import Mapping

from acp.schema import (
    ElicitationFormSessionMode,
    ElicitationMultiSelectPropertySchema,
    ElicitationSchema,
    ElicitationStringPropertySchema,
    EnumOption,
    StringMultiSelectItems,
    TitledMultiSelectItems,
)

from telegram_acp_bot.acp.models import ElicitationOption, ElicitationQuestion


def elicitation_form(mode: object) -> ElicitationFormSessionMode | None:
    """The form the bot knows how to render, or None for a mode it cannot answer with buttons."""
    if isinstance(mode, ElicitationFormSessionMode):
        return mode
    return None


def elicitation_questions(schema: ElicitationSchema) -> tuple[ElicitationQuestion, ...] | None:
    """Every field as a choice of declared options, or None when some field needs typed input."""
    questions: list[ElicitationQuestion] = []
    for field, prop in (schema.properties or {}).items():
        question = _question(field=field, prop=prop)
        if question is None:
            return None
        questions.append(question)
    return tuple(questions) if questions else None


def elicitation_content(
    questions: tuple[ElicitationQuestion, ...],
    answers: Mapping[str, tuple[str, ...]],
) -> dict[str, str | list[str]]:
    """The `content` the agent asked for: one value per single choice, a list per multi choice."""
    content: dict[str, str | list[str]] = {}
    for question in questions:
        values = answers[question.field]
        content[question.field] = list(values) if question.multiple else values[0]
    return content


def _question(*, field: str, prop: object) -> ElicitationQuestion | None:
    if isinstance(prop, ElicitationStringPropertySchema):
        options = _options(prop.one_of, prop.enum)
        multiple = False
    elif isinstance(prop, ElicitationMultiSelectPropertySchema):
        options = _item_options(prop.items)
        multiple = True
    else:
        return None
    if not options:
        return None
    return ElicitationQuestion(
        field=field,
        prompt=prop.description or prop.title or field,
        options=options,
        multiple=multiple,
    )


def _options(one_of: list[EnumOption] | None, enum: list[str] | None) -> tuple[ElicitationOption, ...]:
    if one_of:
        return tuple(ElicitationOption(value=option.const, label=option.title or option.const) for option in one_of)
    return tuple(ElicitationOption(value=value, label=value) for value in (enum or ()))


def _item_options(items: TitledMultiSelectItems | StringMultiSelectItems | object) -> tuple[ElicitationOption, ...]:
    """Options of a multi-select field, which the schema carries under `items` instead of the field."""
    if isinstance(items, TitledMultiSelectItems):
        return _options(items.any_of, None)
    if isinstance(items, StringMultiSelectItems):
        return _options(None, items.enum)
    return ()
