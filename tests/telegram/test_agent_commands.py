from __future__ import annotations

# ruff: noqa: F403, F405, I001

import base64

from telegram import InlineKeyboardButton, Message
from telegram_acp_bot.telegram import bridge as bridge_module
from telegram_acp_bot.telegram.constants import (
    AGENT_COMMAND_KEYBOARD_COLUMNS,
    AGENT_COMMAND_KEYBOARD_INFO_CALLBACK_DATA,
    AGENT_COMMAND_KEYBOARD_INFO_LABEL,
    AGENT_COMMAND_KEYBOARD_MAX_ROWS,
)
from tests.telegram.support import *

GOAL_COMMAND = AgentCommand(name="goal", description="Set or manage a persistent goal", hint="<objective> | status")
RESEARCH_COMMAND = AgentCommand(name="deep-research", description="Research a topic")


def group_prompt(command: str) -> str:
    """A group command as the agent sees it: the mention reminder first, then the command itself."""
    return f"{bridge_module.GROUP_MENTION_REMINDER}\n\n{command}"


def _fake_message(*, text: str | None = None, caption: str | None = None) -> Message:
    return cast(Message, SimpleNamespace(text=text, caption=caption))


class TapQuery:
    """Callback query whose tapped message can be any double, including nothing at all."""

    def __init__(self, data: str, *, message: object) -> None:
        self.data = data
        self.message = message
        self.answers: list[str] = []

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text or "")


def make_callback_update(
    data: str,
    *,
    chat_id: object = TEST_CHAT_ID,
    chat_type: str = "private",
    tapped_text: str = "2 agent commands. Tap one to run it.",
) -> tuple[Update, TapQuery, DummyMessage]:
    """A tap on the bot's own keyboard: no inbound message, only the tapped one."""
    tapped = DummyMessage(tapped_text, message_id=7)
    query = TapQuery(data, message=tapped)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1, username=None),
        effective_chat=None if chat_id is None else SimpleNamespace(id=chat_id, type=chat_type),
        callback_query=query,
        message=None,
        effective_message=tapped,
    )
    return cast(Update, update), query, tapped


def make_commands_bridge(
    commands: tuple[AgentCommand, ...] | None,
    *,
    group_require_mention: bool = False,
    group_media_queue: bool = False,
) -> tuple[TelegramBridge, AgentCommandsService]:
    service = AgentCommandsService(commands)
    config = make_config(
        token="TOKEN",
        allowed_user_ids=[],
        workspace=".",
        group_require_mention=group_require_mention,
        group_media_queue=group_media_queue,
    )
    return TelegramBridge(config=config, agent_service=cast(AgentService, service)), service


def _keyboard_rows(message: DummyMessage) -> tuple[tuple[InlineKeyboardButton, ...], ...]:
    reply_markup = message.reply_kwargs[-1]["reply_markup"]
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    return reply_markup.inline_keyboard


async def test_cmds_shows_one_button_per_advertised_command():
    bridge, _ = make_commands_bridge((GOAL_COMMAND, RESEARCH_COMMAND))
    update = make_update(with_message=True)

    await bridge.cmds(update, make_context())

    assert update.message is not None
    assert "Tap one to run it" in update.message.replies[0]
    buttons = [button for row in _keyboard_rows(update.message) for button in row]
    assert [button.text for button in buttons] == ["/goal", "/deep-research", AGENT_COMMAND_KEYBOARD_INFO_LABEL]
    assert buttons[0].callback_data == "cmd|goal"
    assert buttons[1].callback_data == "cmd|deep-research"
    assert buttons[-1].callback_data == AGENT_COMMAND_KEYBOARD_INFO_CALLBACK_DATA


async def test_cmds_keyboard_stops_at_the_configured_number_of_rows():
    commands = tuple(
        AgentCommand(name=f"cmd{index}", description=f"Command {index}")
        for index in range(AGENT_COMMAND_KEYBOARD_MAX_ROWS * AGENT_COMMAND_KEYBOARD_COLUMNS + 5)
    )
    bridge, _ = make_commands_bridge(commands)
    update = make_update(with_message=True)

    await bridge.cmds(update, make_context())

    assert update.message is not None
    rows = _keyboard_rows(update.message)
    assert len(rows) == AGENT_COMMAND_KEYBOARD_MAX_ROWS + 1
    assert all(len(row) == AGENT_COMMAND_KEYBOARD_COLUMNS for row in rows[:AGENT_COMMAND_KEYBOARD_MAX_ROWS])


async def test_cmds_keyboard_skips_a_name_that_cannot_be_a_callback():
    bridge, _ = make_commands_bridge((AgentCommand(name="weird.name", description="Cannot be tapped"),))
    update = make_update(with_message=True)

    await bridge.cmds(update, make_context())

    assert update.message is not None
    buttons = [button for row in _keyboard_rows(update.message) for button in row]
    assert [button.text for button in buttons] == [AGENT_COMMAND_KEYBOARD_INFO_LABEL]


async def test_tapping_a_command_button_forwards_the_command_to_the_prompt():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))

    update, query, tapped = make_callback_update("cmd|goal")
    await bridge.on_agent_command_callback(update, make_context())

    assert query.answers[-1] == "Running /goal."
    assert [text for text, _ in service.prompts] == ["/goal"]
    assert tapped.replies == ["ok"]


async def test_tapping_a_hyphenated_command_button_forwards_it():
    bridge, service = make_commands_bridge((RESEARCH_COMMAND,))

    update, _, _ = make_callback_update("cmd|deep-research")
    await bridge.on_agent_command_callback(update, make_context())

    assert [text for text, _ in service.prompts] == ["/deep-research"]


async def test_tapping_the_info_button_shows_descriptions_and_hints():
    bridge, _ = make_commands_bridge((GOAL_COMMAND, AgentCommand(name="simplify", description="a" * 101)))

    update, query, tapped = make_callback_update(AGENT_COMMAND_KEYBOARD_INFO_CALLBACK_DATA)
    await bridge.on_agent_command_callback(update, make_context())

    assert query.answers[-1] == "Showing descriptions."
    listing = tapped.replies[0]
    assert "/goal: Set or manage a persistent goal —" in listing
    assert "<objective> | status" in listing
    assert "/simplify: " + "a" * 99 + "…" in listing


async def test_tapping_the_info_button_after_the_session_stops_answers_without_a_reply():
    bridge, _ = make_commands_bridge(())

    update, query, tapped = make_callback_update(AGENT_COMMAND_KEYBOARD_INFO_CALLBACK_DATA)
    await bridge.on_agent_command_callback(update, make_context())

    assert query.answers[-1] == "No commands advertised any more."
    assert tapped.replies == []


async def test_tapping_a_command_the_agent_stopped_advertising_is_not_forwarded():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))

    update, query, tapped = make_callback_update("cmd|quest")
    await bridge.on_agent_command_callback(update, make_context())

    assert "no longer advertises" in query.answers[-1]
    assert tapped.replies == []
    assert service.prompts == []


async def test_a_re_advertised_command_list_replaces_the_keyboard_and_the_gate():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    service.commands = (RESEARCH_COMMAND,)

    update = make_update(with_message=True)
    await bridge.cmds(update, make_context())

    assert update.message is not None
    buttons = [button for row in _keyboard_rows(update.message) for button in row]
    assert [button.text for button in buttons] == ["/deep-research", AGENT_COMMAND_KEYBOARD_INFO_LABEL]

    typed = make_update(text="/goal status")
    await bridge.on_agent_command(typed, make_context())
    assert typed.message is not None
    assert "/goal is not a command this agent advertises. See /cmds." in typed.message.replies[0]

    tap_update, query, _ = make_callback_update("cmd|goal")
    await bridge.on_agent_command_callback(tap_update, make_context())
    assert "no longer advertises" in query.answers[-1]
    assert service.prompts == []


async def test_tapping_a_button_from_another_keyboard_is_an_invalid_action():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))

    update, query, _ = make_callback_update("resume|1")
    await bridge.on_agent_command_callback(update, make_context())

    assert query.answers[-1] == "Invalid action."
    assert service.prompts == []


async def test_tapping_without_a_chat_answers_missing_chat():
    bridge, _ = make_commands_bridge((GOAL_COMMAND,))

    update, query, _ = make_callback_update("cmd|goal", chat_id=None)
    query.message = None
    await bridge.on_agent_command_callback(update, make_context())

    assert query.answers[-1] == "Missing chat."


async def test_tapping_without_a_callback_query_does_nothing():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    update = make_update(with_message=False)
    update.callback_query = None

    await bridge.on_agent_command_callback(update, make_context())

    assert service.prompts == []


async def test_tapping_is_denied_for_a_sender_outside_the_allowlist():
    service = AgentCommandsService((GOAL_COMMAND,))
    bridge = TelegramBridge(
        config=make_config(token="TOKEN", allowed_user_ids=[999], workspace="."),
        agent_service=cast(AgentService, service),
    )

    update, query, _ = make_callback_update("cmd|goal")
    await bridge.on_agent_command_callback(update, make_context())

    assert query.answers[-1] == "Access denied."
    assert service.prompts == []


async def test_group_button_tap_runs_the_command_without_an_at_mention():
    bridge, service = make_commands_bridge((GOAL_COMMAND,), group_require_mention=True)

    update, _, _ = make_callback_update("cmd|goal", chat_type="group")
    await bridge.on_agent_command_callback(update, make_context())

    assert [text for text, _ in service.prompts] == [group_prompt("/goal")]


async def test_cmds_says_no_commands_are_advertised_before_the_first_prompt():
    bridge, _ = make_commands_bridge(())
    update = make_update(with_message=True)

    await bridge.cmds(update, make_context())

    assert update.message is not None
    assert "has not advertised any slash commands yet" in update.message.replies[0]


async def test_cmds_without_a_message_to_answer_to_sends_nothing():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))

    await bridge.cmds(make_update(with_message=False), make_context())

    assert service.prompts == []


async def test_cmds_says_the_agent_does_not_advertise_commands_without_the_getter():
    bridge = make_bridge()
    update = make_update(with_message=True)

    await bridge.cmds(update, make_context())

    assert update.message is not None
    assert "has not advertised any slash commands yet" in update.message.replies[0]


async def test_advertised_agent_command_is_forwarded_to_the_prompt_with_its_leading_slash():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    update = make_update(text="/goal status")

    await bridge.on_agent_command(update, make_context())

    assert [text for text, _ in service.prompts] == ["/goal status"]


async def test_hyphenated_advertised_agent_command_is_forwarded():
    bridge, service = make_commands_bridge((AgentCommand(name="mcp-config", description="Manage MCP servers"),))
    update = make_update(text="/mcp-config list")

    await bridge.on_agent_command(update, make_context())

    assert [text for text, _ in service.prompts] == ["/mcp-config list"]


async def test_command_mentioning_this_bot_is_forwarded_without_the_mention():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    update = make_update(text="/goal@echo_bot status")

    await bridge.on_agent_command(update, make_context())

    assert [text for text, _ in service.prompts] == ["/goal status"]


async def test_command_mentioning_another_bot_keeps_its_own_address():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    update = make_update(text="/goal@other_bot status")

    await bridge.on_agent_command(update, make_context())

    assert [text for text, _ in service.prompts] == ["/goal@other_bot status"]


async def test_unadvertised_slash_command_is_rejected_with_a_hint_to_cmds():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    update = make_update(text="/qwe poke")

    await bridge.on_agent_command(update, make_context())

    assert update.message is not None
    assert "/qwe is not a command this agent advertises. See /cmds." in update.message.replies[0]
    assert service.prompts == []


async def test_agent_command_in_a_photo_caption_is_forwarded_with_the_media():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    context = make_context()
    context.bot.files["photo-1"] = b"bytes"
    update = make_update(caption="/goal review", photo=[SimpleNamespace(file_id="photo-1")])

    await bridge.on_agent_command(update, context)

    assert [text for text, _ in service.prompts] == ["/goal review"]
    expected = base64.b64encode(b"bytes").decode("ascii")
    assert [[image.data_base64 for image in images] for _, images in service.prompts] == [[expected]]


async def test_own_mention_is_stripped_from_a_bare_command_and_from_a_caption():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    context = make_context()
    context.bot.files["photo-1"] = b"bytes"

    await bridge.on_agent_command(make_update(text="/goal@echo_bot"), context)
    await bridge.on_agent_command(
        make_update(caption="/goal@echo_bot status", photo=[SimpleNamespace(file_id="photo-1")]),
        context,
    )

    assert [text for text, _ in service.prompts] == ["/goal", "/goal status"]


async def test_agent_command_gate_ignores_text_without_a_leading_command_token():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))

    await bridge.on_agent_command(make_update(text="goal status"), make_context())
    await bridge.on_agent_command(make_update(with_message=False), make_context())

    assert service.prompts == []


async def test_published_command_names_are_matched_case_insensitively_and_without_a_slash():
    bridge, service = make_commands_bridge((AgentCommand(name="/Goal", description="Set a goal"),))

    await bridge.on_agent_command(make_update(text="/goal status"), make_context())

    assert [text for text, _ in service.prompts] == ["/goal status"]


def _installed_callbacks(bridge: TelegramBridge) -> list[object]:
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".")
    app = build_application(config, bridge)
    assert list(app.handlers) == [0]
    return [handler.callback for handler in app.handlers[0]]


async def test_bot_commands_are_registered_before_the_agent_command_gate():
    bridge = make_bridge()
    callbacks = _installed_callbacks(bridge)

    assert bridge.cmds in callbacks
    # PTB runs only the first matching handler in a group, so ordering is the precedence rule.
    assert callbacks.index(bridge.help) < callbacks.index(bridge.on_agent_command)
    assert callbacks.index(bridge.on_agent_command) < callbacks.index(bridge.on_message)


async def test_agent_command_filter_matches_a_leading_token_in_text_or_caption():
    gate = bridge_module._AGENT_COMMAND

    assert gate.filter(_fake_message(text="/mcp-config list"))
    assert gate.filter(_fake_message(caption="/goal review"))
    assert not gate.filter(_fake_message(text="hello /goal"))
    assert not gate.filter(_fake_message())


async def test_unmentioned_group_command_is_ignored_without_a_reply():
    bridge, service = make_commands_bridge((GOAL_COMMAND,), group_require_mention=True)
    update = make_update(text="/qwe poke", chat_type="group")

    await bridge.on_agent_command(update, make_context())

    assert update.message is not None
    assert update.message.replies == []
    assert service.prompts == []


async def test_unmentioned_advertised_group_command_is_not_forwarded():
    bridge, service = make_commands_bridge((GOAL_COMMAND,), group_require_mention=True)

    await bridge.on_agent_command(make_update(text="/goal status", chat_type="group"), make_context())

    assert service.prompts == []


async def test_group_command_mentioning_the_bot_is_forwarded_without_the_mention():
    bridge, service = make_commands_bridge((GOAL_COMMAND,), group_require_mention=True)

    await bridge.on_agent_command(make_update(text="/goal@echo_bot status", chat_type="group"), make_context())

    assert [text for text, _ in service.prompts] == [group_prompt("/goal status")]


async def test_unmentioned_group_command_caption_queues_its_photo_once():
    bridge, service = make_commands_bridge((GOAL_COMMAND,), group_require_mention=True, group_media_queue=True)

    await bridge.on_agent_command(
        make_update(caption="/qwe poke", chat_type="group", photo=[SimpleNamespace(file_id="photo-1")]),
        make_context(),
    )

    assert service.pending_media == [(100, 1, "photo-1", "image/jpeg")]
    assert service.prompts == []


async def test_mentioned_group_command_photo_is_forwarded_and_not_queued():
    bridge, service = make_commands_bridge((GOAL_COMMAND,), group_require_mention=True, group_media_queue=True)
    context = make_context()
    context.bot.files["photo-1"] = b"bytes"

    await bridge.on_agent_command(
        make_update(caption="/goal@echo_bot review", chat_type="group", photo=[SimpleNamespace(file_id="photo-1")]),
        context,
    )

    assert [text for text, _ in service.prompts] == [group_prompt("/goal review")]
    assert service.pending_media == []


async def test_bot_command_written_in_a_caption_runs_the_bot_handler():
    bridge, service = make_commands_bridge((GOAL_COMMAND,))
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".")
    bridge.install(build_application(config, bridge))
    update = make_update(caption="/cmds", photo=[SimpleNamespace(file_id="photo-1")])

    await bridge.on_agent_command(update, make_context())

    assert update.message is not None
    assert "Tap one to run it" in update.message.replies[0]
    assert _keyboard_rows(update.message)[0][0].callback_data == "cmd|goal"
    assert service.prompts == []
