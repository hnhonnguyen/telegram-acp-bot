from __future__ import annotations

from acp_chat_bot.acp.models import AgentActivityBlock
from acp_chat_bot.channels.dingtalk.render import activity_markdown, choice_markdown, chunk_markdown
from acp_chat_bot.core.chat import ChoiceOption, ChoiceRequest


def test_chunk_markdown_keeps_short_text_whole():
    assert chunk_markdown("hello", 100) == ["hello"]
    assert chunk_markdown("hello", 0) == ["hello"]


def test_chunk_markdown_prefers_paragraph_and_line_breaks():
    assert chunk_markdown("one\n\ntwo\n\nthree", 8) == ["one", "two", "three"]
    assert chunk_markdown("one\ntwo\nthree", 8) == ["one\ntwo", "three"]


def test_chunk_markdown_splits_long_unbroken_text():
    chunks = chunk_markdown("x" * 25, 10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]


def test_activity_markdown_uses_known_and_unknown_labels():
    assert activity_markdown(AgentActivityBlock(kind="think", title="Checking", status="completed")) == (
        "**💡 Thinking**: Checking"
    )
    assert (
        activity_markdown(AgentActivityBlock(kind="custom", title="Working", status="completed", text="details"))
        == "**Custom**: Working\n\ndetails"
    )


def test_choice_markdown_lists_options_and_skip():
    request = ChoiceRequest(
        request_id="r1",
        kind="permission",
        title="Permission",
        body="Run ls",
        options=(ChoiceOption(value="once", label="Allow once"), ChoiceOption(value="deny", label="Deny")),
        skippable=True,
    )

    text = choice_markdown(request)

    assert "1. Allow once" in text
    assert "3. Skip" in text
    assert "Reply with the number of your choice." in text


def test_choice_markdown_mentions_multiple_selection():
    request = ChoiceRequest(
        request_id="r1",
        kind="elicitation",
        title="Pick",
        body="Choose",
        options=(ChoiceOption(value="a", label="A"),),
        multiple=True,
    )

    assert "numbers" in choice_markdown(request)
