from __future__ import annotations

# ruff: noqa: F403, F405, I001

import errno
import os
import signal

from tests.acp.support import *

PROMPT_MESSAGE_ID = 42
GROUP_HISTORY_CAP = 200


async def test_new_session_creates_missing_workspace(tmp_path: Path):
    missing = tmp_path / "missing"
    process = FakeProcess()
    connection = FakeConnection(session_id="created")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    session_id = await service.new_session(chat_id=1, workspace=missing)
    assert session_id == "created"
    assert missing.is_dir()


async def test_new_session_rejects_file_workspace(tmp_path: Path):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    invalid = tmp_path / "not-a-dir"
    invalid.write_text("x")

    with pytest.raises(ValueError):
        await service.new_session(chat_id=1, workspace=invalid)


async def test_new_session_rejects_process_without_stdio(tmp_path: Path):
    workspace = tmp_path

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return FakeProcess(with_pipes=False)

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn)

    with pytest.raises(RuntimeError):
        await service.new_session(chat_id=1, workspace=workspace)


async def test_acp_service_rejects_non_positive_stdio_limit():
    with pytest.raises(ValueError):
        AcpAgentService(SessionRegistry(), program="agent", args=[], stdio_limit=0)


async def test_acp_service_rejects_non_positive_connect_timeout():
    with pytest.raises(ValueError):
        AcpAgentService(SessionRegistry(), program="agent", args=[], connect_timeout=0)


async def test_new_session_passes_stdio_limit_to_spawner(tmp_path: Path):
    process = FakeProcess()
    limits: list[int] = []

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args
        limits.append(cast(int, kwargs["limit"]))
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        conn = FakeConnection(session_id="s-limit")
        conn.client = client
        return conn

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        stdio_limit=2_000_000,
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)
    assert limits == [2_000_000]


async def test_new_session_times_out_when_agent_does_not_handshake(tmp_path: Path):
    process = FakeProcess()
    connection = HangingInitializeConnection(session_id="never")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del client, input_stream, output_stream
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        connect_timeout=0.01,
        spawner=fake_spawn,
        connector=fake_connect,
    )

    with pytest.raises(RuntimeError, match="Timed out waiting for ACP agent handshake"):
        await service.new_session(chat_id=1, workspace=tmp_path)
    assert process.terminated


async def test_new_session_times_out_during_session_new(tmp_path: Path):
    process = FakeProcess()
    connection = HangingNewSessionConnection(session_id="never")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del client, input_stream, output_stream
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        connect_timeout=0.01,
        spawner=fake_spawn,
        connector=fake_connect,
    )
    with pytest.raises(RuntimeError, match="Timed out waiting for ACP agent handshake"):
        await service.new_session(chat_id=1, workspace=tmp_path)
    assert process.terminated


async def test_new_session_and_prompt(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="real-session")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        assert input_stream is process.stdin
        assert output_stream is process.stdout
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=["--x"],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    assert service.get_active_session_context(chat_id=2) is None

    session_id = await service.new_session(chat_id=2, workspace=tmp_path)
    assert session_id == "real-session"
    assert connection.initialized
    assert connection.cwd == str(tmp_path.resolve())
    assert connection.new_session_mcp_servers == []
    assert service.get_workspace(chat_id=2) == tmp_path.resolve()
    assert service.get_active_session_context(chat_id=2) == ("real-session", tmp_path.resolve())

    reply = await service.prompt(chat_id=2, text="hi")
    assert reply is not None
    assert reply.text == "hello from acp"
    assert connection.prompt_calls == ["real-session"]


async def test_load_session_and_supports_session_loading(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="real-session", supports_load_session=True)

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    loaded_id = await service.load_session(chat_id=2, session_id="loaded-session", workspace=tmp_path)
    assert loaded_id == "loaded-session"
    assert connection.cwd == str(tmp_path.resolve())
    assert connection.load_session_mcp_servers == []
    assert service.get_workspace(chat_id=2) == tmp_path.resolve()
    assert service.supports_session_loading(chat_id=2) is True


async def test_service_passes_configured_mcp_servers_to_new_and_load_session(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="real-session", supports_load_session=True)
    server = McpServerStdio(name="telegram-channel", command="uv", args=["run", "mcp-server"], env=[])

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        mcp_servers=(server,),
        spawner=fake_spawn,
        connector=fake_connect,
    )

    await service.new_session(chat_id=2, workspace=tmp_path)
    assert connection.new_session_mcp_servers == [server]
    await service.load_session(chat_id=2, session_id="loaded-session", workspace=tmp_path)
    assert connection.load_session_mcp_servers == [server]


async def test_service_persists_channel_session_mapping(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="mapped-session", supports_load_session=True)
    state_file = tmp_path / "channel-state.json"

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        channel_state_file=state_file,
        spawner=fake_spawn,
        connector=fake_connect,
    )

    await service.new_session(chat_id=9, workspace=tmp_path)
    assert load_session_chat_map(state_file) == {"mapped-session": 9}
    assert load_last_session_id(state_file) == "mapped-session"

    await service.stop(chat_id=9)
    assert load_session_chat_map(state_file) == {}
    assert load_last_session_id(state_file) is None


async def test_drop_channel_session_mapping_ignores_unknown_session(tmp_path: Path):
    state_file = tmp_path / "channel-state.json"
    save_session_chat_map(state_file, {"known": 11})
    save_last_session_id(state_file, "known")
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], channel_state_file=state_file)

    await service._drop_channel_session_mapping(session_id="unknown")

    assert load_session_chat_map(state_file) == {"known": 11}
    assert load_last_session_id(state_file) == "known"


async def test_prompt_context_refreshes_the_last_session(tmp_path: Path):
    """A turn in flight makes its session the one an attachment call falls back to."""
    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], channel_state_file=state_file)

    await service.set_prompt_message_context(session_id="latest", message_id=PROMPT_MESSAGE_ID)

    assert load_last_session_id(state_file) == "latest"


async def test_clearing_prompt_context_leaves_the_last_session_alone(tmp_path: Path):
    """End-of-turn cleanup clears the message id without repointing the fallback."""
    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], channel_state_file=state_file)
    await service.set_prompt_message_context(session_id="latest", message_id=PROMPT_MESSAGE_ID)

    await service.set_prompt_message_context(session_id="latest", message_id=None)

    assert load_prompt_message_id(state_file, "latest") is None
    assert load_last_session_id(state_file) == "latest"


async def test_set_prompt_message_context_updates_state_file(tmp_path: Path):
    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], channel_state_file=state_file)

    await service.set_prompt_message_context(session_id="latest", message_id=PROMPT_MESSAGE_ID)

    assert load_prompt_message_id(state_file, "latest") == PROMPT_MESSAGE_ID


async def test_set_prompt_message_context_without_state_file_is_noop():
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])

    await service.set_prompt_message_context(session_id="ignored", message_id=PROMPT_MESSAGE_ID)


async def test_record_pending_media_then_take_it(tmp_path: Path):
    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], channel_state_file=state_file)

    await service.record_pending_media(chat_id=9, message_id=PROMPT_MESSAGE_ID, file_id="f1", mime_type="image/jpeg")
    refs = await service.take_pending_media(chat_id=9)

    assert [ref["file_id"] for ref in refs] == ["f1"]
    assert refs[0]["message_id"] == PROMPT_MESSAGE_ID
    assert load_pending_media(state_file) == {}


async def test_pending_media_helpers_without_state_file_are_noop(tmp_path: Path):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])

    await service.record_pending_media(chat_id=9, message_id=1, file_id="f1", mime_type=None)

    assert await service.take_pending_media(chat_id=9) == ()
    assert not (tmp_path / "channel-state.json").exists()


async def test_record_group_message_then_read_it_back(tmp_path: Path):
    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], channel_state_file=state_file)

    await service.record_group_message(
        chat_id=9,
        message_id=PROMPT_MESSAGE_ID,
        sender_id=7,
        sender_name="alice",
        text="chuyện trong group",
        max_entries=GROUP_HISTORY_CAP,
    )
    entries = await service.read_group_history(chat_id=9)

    assert [entry["text"] for entry in entries] == ["chuyện trong group"]
    assert entries[0]["message_id"] == PROMPT_MESSAGE_ID
    assert entries[0]["sent_at"] != ""


async def test_group_history_helpers_without_state_file_are_noop(tmp_path: Path):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])

    await service.record_group_message(
        chat_id=9, message_id=1, sender_id=7, sender_name=None, text="x", max_entries=GROUP_HISTORY_CAP
    )

    assert await service.read_group_history(chat_id=9) == ()
    assert not (tmp_path / "channel-state.json").exists()


async def test_record_group_message_with_a_zero_cap_writes_nothing(tmp_path: Path):
    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], channel_state_file=state_file)

    await service.record_group_message(chat_id=9, message_id=1, sender_id=7, sender_name=None, text="x", max_entries=0)

    assert not state_file.exists()


async def test_load_session_rejects_when_capability_is_false(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(supports_load_session=False)

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del client, input_stream, output_stream
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    with pytest.raises(RuntimeError, match="does not support `session/load`"):
        await service.load_session(chat_id=1, session_id="x", workspace=tmp_path)


async def test_load_session_rejects_file_workspace(tmp_path: Path):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    invalid = tmp_path / "not-a-dir"
    invalid.write_text("x")
    with pytest.raises(ValueError):
        await service.load_session(chat_id=1, session_id="x", workspace=invalid)


async def test_load_session_replaces_existing_and_shuts_down(tmp_path: Path):
    first = FakeProcess()
    second = FakeProcess()
    calls: list[FakeProcess] = []

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        process = first if not calls else second
        calls.append(process)
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection = FakeConnection(supports_load_session=True)
        connection.client = client
        return connection

    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
        channel_state_file=state_file,
    )
    await service.new_session(chat_id=3, workspace=tmp_path)
    await service.load_session(chat_id=3, session_id="reloaded", workspace=tmp_path)
    assert first.terminated
    assert load_session_chat_map(state_file) == {"reloaded": 3}


async def test_load_session_times_out(tmp_path: Path):
    process = FakeProcess()
    connection = HangingLoadSessionConnection(supports_load_session=True)

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del client, input_stream, output_stream
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        connect_timeout=0.01,
        spawner=fake_spawn,
        connector=fake_connect,
    )
    with pytest.raises(RuntimeError, match="Timed out waiting for ACP agent handshake"):
        await service.load_session(chat_id=1, session_id="x", workspace=tmp_path)
    assert process.terminated


async def test_list_resumable_sessions_supported_and_filtered(tmp_path: Path):
    workspace = tmp_path / "w"
    workspace.mkdir()
    listed = [
        SessionInfo(
            session_id="s1",
            cwd=str(workspace),
            title="session one",
            updated_at="2026-03-02T12:00:00Z",
        ),
        SessionInfo(
            session_id="s2",
            cwd=str(tmp_path / "other"),
            title="other",
            updated_at="2026-03-01T12:00:00Z",
        ),
    ]
    process = FakeProcess()
    connection = FakeConnection(listed_sessions=listed, supports_session_list=True)

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del client, input_stream, output_stream
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    sessions = await service.list_resumable_sessions(chat_id=1, workspace=workspace)
    assert sessions is not None
    assert [item.session_id for item in sessions] == ["s1"]
    assert connection.list_calls


async def test_list_resumable_sessions_returns_none_when_not_supported(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(supports_session_list=False)

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del client, input_stream, output_stream
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    assert await service.list_resumable_sessions(chat_id=1, workspace=tmp_path) is None
    assert service.supports_session_loading(chat_id=1) is None


async def test_list_resumable_sessions_live_without_list_support_returns_none(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(supports_session_list=False)

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=8, workspace=tmp_path)
    assert await service.list_resumable_sessions(chat_id=8, workspace=tmp_path) is None


async def test_list_resumable_sessions_uses_live_connection_branch(tmp_path: Path):
    listed = [
        SessionInfo(
            session_id="s-live",
            cwd=str(tmp_path),
            title="live",
            updated_at="2026-03-02T14:00:00Z",
        )
    ]
    process = FakeProcess()
    connection = FakeConnection(listed_sessions=listed, supports_session_list=True)

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=7, workspace=tmp_path)
    sessions = await service.list_resumable_sessions(chat_id=7, workspace=tmp_path)
    assert sessions is not None
    assert sessions[0].session_id == "s-live"


async def test_list_resumable_sessions_returns_none_when_session_capabilities_missing(tmp_path: Path):
    process = FakeProcess()
    connection = NoSessionCapabilitiesConnection()

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del client, input_stream, output_stream
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    assert await service.list_resumable_sessions(chat_id=1, workspace=tmp_path) is None


async def test_prompt_without_active_session_returns_none():
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    reply = await service.prompt(chat_id=99, text="hi")
    assert reply is None


async def test_prompt_wraps_stdio_limit_error(tmp_path: Path) -> None:
    process = FakeProcess()
    connection = OversizeLineConnection(session_id="limit-session")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=1, workspace=tmp_path)
    with pytest.raises(AgentOutputLimitExceededError):
        await service.prompt(chat_id=1, text="oversize")


async def test_prompt_reraises_unrelated_value_error(tmp_path: Path) -> None:
    process = FakeProcess()
    connection = GenericValueErrorConnection(session_id="value-error-session")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=1, workspace=tmp_path)
    with pytest.raises(ValueError, match="unexpected"):
        await service.prompt(chat_id=1, text="boom")


async def test_prompt_resets_active_auto_approve_on_prompt_error(tmp_path: Path):
    process = FakeProcess()
    connection = GenericValueErrorConnection(session_id="value-error-session")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=1, workspace=tmp_path)
    await service.set_next_prompt_auto_approve(chat_id=1, enabled=True)
    with pytest.raises(ValueError, match="unexpected"):
        await service.prompt(chat_id=1, text="boom")

    live = service._live_by_chat[1]
    assert live.next_prompt_auto_approve is False
    assert live.active_prompt_auto_approve is False


def make_restart_fakes(
    connections: list[FakeConnection],
    processes: list[FakeProcess],
):
    """Spawner/connector pair handing out one process+connection per ACP start."""

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        if not processes:
            raise FileNotFoundError(AGENT_BINARY_MISSING_ERROR)
        return processes.pop(0)

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection = connections.pop(0)
        connection.client = client
        return connection

    return fake_spawn, fake_connect


async def test_prompt_revives_session_when_agent_dies_mid_turn(tmp_path: Path):
    dying = ConnectionLossConnection(session_id="crash-session")
    revived = FakeConnection(session_id="crash-session")
    processes = [FakeProcess(), FakeProcess()]
    spawn, connect = make_restart_fakes([dying, revived], processes)
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=spawn, connector=connect)
    await service.new_session(chat_id=1, workspace=tmp_path)

    reply = await service.prompt(chat_id=1, text="continue")

    assert reply is not None and reply.text == AGENT_EXIT_NOTICE
    # The half-finished turn is not replayed onto the fresh agent.
    assert revived.prompt_calls == []
    assert revived.load_session_mcp_servers == []
    assert revived.cwd == str(tmp_path.resolve())
    assert service._live_by_chat[1].acp_session_id == "crash-session"


async def test_prompt_runs_on_revived_agent_when_agent_died_between_turns(tmp_path: Path):
    dead = FakeConnection(session_id="exit-session")
    first_process = FakeProcess()
    revived = FakeConnection(session_id="exit-session")
    processes = [first_process, FakeProcess()]
    spawn, connect = make_restart_fakes([dead, revived], processes)
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=spawn, connector=connect)
    await service.new_session(chat_id=1, workspace=tmp_path)
    first_process.returncode = 137

    reply = await service.prompt(chat_id=1, text="hello again")

    assert reply is not None and reply.text == "hello from acp"
    assert revived.prompt_calls == ["exit-session"]


async def test_revive_starts_new_session_when_agent_cannot_load(tmp_path: Path):
    dying = ConnectionLossConnection(session_id="crash-session")
    # Two fresh agents take over: the one that refuses `session/load`, then the one that starts a session.
    refuses_load = FakeConnection(session_id="crash-session", supports_load_session=False)
    replacement = FakeConnection(session_id="replacement-session")
    processes = [FakeProcess(), FakeProcess(), FakeProcess()]
    spawn, connect = make_restart_fakes([dying, refuses_load, replacement], processes)
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=spawn, connector=connect)
    await service.new_session(chat_id=1, workspace=tmp_path)

    reply = await service.prompt(chat_id=1, text="continue")

    assert reply is not None and reply.text == AGENT_EXIT_NOTICE
    assert replacement.new_session_mcp_servers == []
    assert service._live_by_chat[1].acp_session_id == "replacement-session"


async def test_revive_raises_when_agent_cannot_be_restarted(tmp_path: Path):
    dying = ConnectionLossConnection(session_id="crash-session")
    processes = [FakeProcess()]
    spawn, connect = make_restart_fakes([dying], processes)
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=spawn, connector=connect)
    await service.new_session(chat_id=1, workspace=tmp_path)

    with pytest.raises(AgentConnectionLostError):
        await service.prompt(chat_id=1, text="continue")


async def test_revive_keeps_chat_permission_mode(tmp_path: Path):
    dying = ConnectionLossConnection(session_id="approve-session")
    revived = FakeConnection(session_id="approve-session")
    processes = [FakeProcess(), FakeProcess()]
    spawn, connect = make_restart_fakes([dying, revived], processes)
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=spawn, connector=connect)
    await service.new_session(chat_id=1, workspace=tmp_path)
    await service.set_session_permission_mode(chat_id=1, mode="approve")

    await service.prompt(chat_id=1, text="continue")

    policy = service.get_permission_policy(chat_id=1)
    assert policy is not None and policy.session_mode == "approve"


async def test_revive_drops_permission_requests_of_the_dead_session(tmp_path: Path):
    dying = ConnectionLossConnection(session_id="permission-session")
    revived = FakeConnection(session_id="permission-session")
    processes = [FakeProcess(), FakeProcess()]
    spawn, connect = make_restart_fakes([dying, revived], processes)
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=spawn, connector=connect)
    await service.new_session(chat_id=1, workspace=tmp_path)

    future: asyncio.Future[RequestPermissionResponse] = asyncio.get_running_loop().create_future()
    service._pending_permissions["req-1"] = _PendingPermission(
        request_id="req-1",
        chat_id=1,
        acp_session_id="permission-session",
        tool_title="run tests",
        tool_call_id="call-1",
        options=(),
        future=future,
    )
    # Another chat's approval and an already answered one must survive this revive untouched.
    other: asyncio.Future[RequestPermissionResponse] = asyncio.get_running_loop().create_future()
    service._pending_permissions["req-other"] = _PendingPermission(
        request_id="req-other",
        chat_id=2,
        acp_session_id="other-session",
        tool_title="read file",
        tool_call_id="call-2",
        options=(),
        future=other,
    )
    answered: asyncio.Future[RequestPermissionResponse] = asyncio.get_running_loop().create_future()
    answered.set_result(RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled")))
    service._pending_permissions["req-answered"] = _PendingPermission(
        request_id="req-answered",
        chat_id=1,
        acp_session_id="permission-session",
        tool_title="read file",
        tool_call_id="call-3",
        options=(),
        future=answered,
    )

    await service.prompt(chat_id=1, text="continue")

    assert future.done()
    assert future.result().outcome.outcome == "cancelled"
    assert "req-1" not in service._pending_permissions
    assert "req-other" in service._pending_permissions and not other.done()
    assert "req-answered" in service._pending_permissions


async def test_cancel_ignores_an_exited_agent(tmp_path: Path):
    connection = FakeConnection(session_id="cancel-session")
    process = FakeProcess()
    spawn, connect = make_restart_fakes([connection], [process])
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=spawn, connector=connect)
    await service.new_session(chat_id=1, workspace=tmp_path)
    process.returncode = 1

    assert await service.cancel(chat_id=1) is False
    assert connection.prompt_calls == []


async def test_prompt_resolves_file_uri_resource_as_image(tmp_path: Path):
    workspace = tmp_path / "ws"
    image_path = workspace / "img sample.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n")
    uri = image_path.as_uri()

    process = FakeProcess()
    connection = FileResourceConnection(
        session_id="file-image",
        resource=ResourceContentBlock(name="img sample.png", uri=uri, mime_type="image/png", type="resource_link"),
    )

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=1, workspace=workspace)
    reply = await service.prompt(chat_id=1, text="send image")
    assert reply is not None
    assert len(reply.images) == 1
    assert reply.images[0].mime_type == "image/png"
    assert reply.files == ()


async def test_prompt_resolves_file_uri_resource_as_document(tmp_path: Path):
    workspace = tmp_path / "ws"
    text_file = workspace / "note.txt"
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("hello file")

    process = FakeProcess()
    connection = FileResourceConnection(
        session_id="file-doc",
        resource=ResourceContentBlock(
            name="note.txt", uri=text_file.as_uri(), mime_type="text/plain", type="resource_link"
        ),
    )

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=1, workspace=workspace)
    reply = await service.prompt(chat_id=1, text="send doc")
    assert reply is not None
    assert reply.images == ()
    assert len(reply.files) == 1
    assert reply.files[0].mime_type == "text/plain"
    assert reply.files[0].data_base64 == base64.b64encode(b"hello file").decode("ascii")


async def test_prompt_reports_warning_for_outside_workspace_file_uri(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")

    process = FakeProcess()
    connection = FileResourceConnection(
        session_id="file-warning",
        resource=ResourceContentBlock(
            name="outside.png", uri=outside.as_uri(), mime_type="image/png", type="resource_link"
        ),
    )

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=1, workspace=workspace)
    reply = await service.prompt(chat_id=1, text="send outside")
    assert reply is not None
    assert reply.images == ()
    assert reply.files == ()
    assert "Attachment warning: outside.png: path is outside active workspace" in reply.text


async def test_prompt_resolves_percent_encoded_file_uri(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    encoded_name = "encoded file.png"
    encoded_file = workspace / encoded_name
    encoded_file.write_bytes(b"png-bytes")
    encoded_uri = f"file://{quote(str(encoded_file), safe='/')}"

    process = FakeProcess()
    connection = FileResourceConnection(
        session_id="encoded-uri",
        resource=ResourceContentBlock(name=encoded_name, uri=encoded_uri, mime_type="image/png", type="resource_link"),
    )

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=1, workspace=workspace)
    reply = await service.prompt(chat_id=1, text="send encoded")
    assert reply is not None
    assert len(reply.images) == 1
    assert reply.images[0].mime_type == "image/png"


async def test_cancel_and_stop_lifecycle(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="s1")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=7, workspace=tmp_path)

    assert await service.cancel(chat_id=7)
    assert connection.prompt_calls[-1] == "cancel:s1"
    assert await service.clear(chat_id=7)
    assert not await service.stop(chat_id=7)
    assert not await service.cancel(chat_id=7)


async def test_permission_policy_session_and_next_prompt(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="perm-session")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=9, workspace=tmp_path)

    policy = service.get_permission_policy(chat_id=9)
    assert policy is not None
    assert policy.session_mode == "ask"
    assert not policy.next_prompt_auto_approve

    assert await service.set_session_permission_mode(chat_id=9, mode="approve")
    assert await service.set_next_prompt_auto_approve(chat_id=9, enabled=True)
    policy = service.get_permission_policy(chat_id=9)
    assert policy is not None
    assert policy.session_mode == "approve"
    assert policy.next_prompt_auto_approve

    assert await service.prompt(chat_id=9, text="hello") is not None
    image = PromptImage(data_base64=base64.b64encode(b"i").decode("ascii"), mime_type="image/png")
    file_text = PromptFile(name="t.txt", text_content="abc")
    file_bin = PromptFile(name="b.bin", data_base64=base64.b64encode(b"bin").decode("ascii"))
    assert await service.prompt(chat_id=9, text="with files", images=(image,), files=(file_text, file_bin)) is not None
    policy = service.get_permission_policy(chat_id=9)
    assert policy is not None
    assert not policy.next_prompt_auto_approve

    assert not await service.set_session_permission_mode(chat_id=999, mode="deny")
    assert not await service.set_next_prompt_auto_approve(chat_id=999, enabled=True)
    assert service.get_permission_policy(chat_id=999) is None


async def test_decide_permission_states(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="perm")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)
    option = PermissionOption(kind="allow_once", name="Allow once", option_id="opt")

    tool_call = ToolCall(title="run", tool_call_id="tc")

    denied_no_option = await service._decide_permission("perm", [], tool_call)
    assert denied_no_option.outcome.outcome == "cancelled"

    denied_unknown_session = await service._decide_permission("unknown", [option], tool_call)
    assert denied_unknown_session.outcome.outcome == "cancelled"

    asked_mode = await service._decide_permission("perm", [option], tool_call)
    assert asked_mode.outcome.outcome == "cancelled"

    live = service._live_by_chat[1]
    live.permission_mode = "approve"
    approved_session = await service._decide_permission("perm", [option], tool_call)
    assert approved_session.outcome.outcome == "selected"

    live.permission_mode = "deny"
    denied_session = await service._decide_permission("perm", [option], tool_call)
    assert denied_session.outcome.outcome == "cancelled"

    live.active_prompt_auto_approve = True
    approved_prompt = await service._decide_permission("perm", [option], tool_call)
    assert approved_prompt.outcome.outcome == "selected"


async def test_decide_permission_ask_mode_with_handler(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="ask")
    captured: list[tuple[str, tuple[str, ...]]] = []

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)

    async def handler(request):
        captured.append((request.request_id, request.available_actions))
        await service.respond_permission_request(chat_id=1, request_id=request.request_id, action="once")

    service.set_permission_request_handler(handler)
    option = PermissionOption(kind="allow_once", name="Allow once", option_id="opt")
    tool_call = ToolCall(title="run", tool_call_id="tc-ask")
    response = await service._decide_permission("ask", [option], tool_call)
    assert response.outcome.outcome == "selected"
    assert captured
    assert "once" in captured[0][1]
    assert "deny" in captured[0][1]


async def test_respond_permission_request_always_enables_session_approve(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="always")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)

    async def handler(request):
        await service.respond_permission_request(chat_id=1, request_id=request.request_id, action="always")

    service.set_permission_request_handler(handler)
    option = PermissionOption(kind="allow_always", name="Always", option_id="opt-always")
    tool_call = ToolCall(title="run", tool_call_id="tc-always")
    response = await service._decide_permission("always", [option], tool_call)
    assert response.outcome.outcome == "selected"
    policy = service.get_permission_policy(chat_id=1)
    assert policy is not None
    assert policy.session_mode == "approve"


async def test_respond_permission_request_rejects_unknown_request(tmp_path: Path):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], default_permission_mode="ask")
    assert not await service.respond_permission_request(chat_id=1, request_id="missing", action="deny")


async def test_respond_permission_request_rejects_unavailable_action(tmp_path: Path):
    del tmp_path
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    future: asyncio.Future[RequestPermissionResponse] = asyncio.get_running_loop().create_future()
    service._pending_permissions["req-1"] = _PendingPermission(
        request_id="req-1",
        chat_id=1,
        acp_session_id="s-1",
        tool_title="Run",
        tool_call_id="tc-1",
        options=(PermissionOption(kind="allow_once", name="Allow once", option_id="opt-once"),),
        future=future,
    )

    accepted = await service.respond_permission_request(chat_id=1, request_id="req-1", action="always")
    assert accepted is False
    assert not future.done()


async def test_decide_permission_auto_approve_supports_allow_always_only(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="approve-always-only")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)
    live = service._live_by_chat[1]
    live.permission_mode = "approve"

    option = PermissionOption(kind="allow_always", name="Always", option_id="opt-always")
    tool_call = ToolCall(title="run", tool_call_id="tc-auto-always")
    response = await service._decide_permission("approve-always-only", [option], tool_call)
    assert response.outcome.outcome == "selected"


async def test_stop_cancels_pending_permission_requests(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="pending")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=7, workspace=tmp_path)

    async def scenario() -> None:
        future: asyncio.Future[RequestPermissionResponse] = asyncio.get_running_loop().create_future()
        service._pending_permissions["req"] = _PendingPermission(
            request_id="req",
            chat_id=7,
            acp_session_id="pending",
            tool_title="run",
            tool_call_id="tc",
            options=(),
            future=future,
        )
        assert await service.stop(chat_id=7)
        assert future.done()
        assert future.result().outcome.outcome == "cancelled"

    await scenario()


async def test_decide_permission_timeout_returns_cancelled(tmp_path: Path, monkeypatch):
    process = FakeProcess()
    connection = FakeConnection(session_id="timeout")

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)

    async def handler(request):
        del request

    service.set_permission_request_handler(handler)

    async def fake_wait_for(awaitable, **kwargs):
        del kwargs
        awaitable.cancel()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    option = PermissionOption(kind="allow_once", name="Allow once", option_id="opt")
    tool_call = ToolCall(title="run", tool_call_id="tc-timeout")
    response = await service._decide_permission("timeout", [option], tool_call)
    assert response.outcome.outcome == "cancelled"


async def test_build_permission_response_fallbacks():
    deny = AcpAgentService._build_permission_response(options=(), action="deny")
    assert deny.outcome.outcome == "cancelled"

    fallback = AcpAgentService._build_permission_response(options=(), action="once")
    assert fallback.outcome.outcome == "cancelled"

    no_once = AcpAgentService._build_permission_response(
        options=(PermissionOption(kind="allow_always", name="Always", option_id="opt-always"),),
        action="once",
    )
    assert no_once.outcome.outcome == "cancelled"

    no_always = AcpAgentService._build_permission_response(
        options=(PermissionOption(kind="allow_once", name="Allow once", option_id="opt-once"),),
        action="always",
    )
    assert no_always.outcome.outcome == "cancelled"


async def test_available_actions_reflect_agent_options():
    both = (
        PermissionOption(kind="allow_once", name="Allow once", option_id="opt-once"),
        PermissionOption(kind="allow_always", name="Always", option_id="opt-always"),
    )
    assert AcpAgentService._available_actions(both) == ("always", "once", "deny")

    always_only = (PermissionOption(kind="allow_always", name="Always", option_id="opt-always"),)
    assert AcpAgentService._available_actions(always_only) == ("always", "deny")

    once_only = (PermissionOption(kind="allow_once", name="Allow once", option_id="opt-once"),)
    assert AcpAgentService._available_actions(once_only) == ("once", "deny")

    assert AcpAgentService._auto_approve_action(()) == "deny"


async def test_report_permission_event_respects_output_mode(caplog: pytest.LogCaptureFixture):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[], permission_event_output="off")
    with caplog.at_level(logging.INFO):
        service._report_permission_event("session-x", "x")
    assert not caplog.records

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], permission_event_output="stdout")
    with caplog.at_level(logging.INFO):
        service._report_permission_event("session-y", "y")
    assert any("ACP permission event: y" in record.message for record in caplog.records)


async def test_forward_activity_event_routes_to_matching_chat(tmp_path: Path):
    process = FakeProcess()
    connection = FakeConnection(session_id="activity-session")
    received: list[tuple[int, AgentActivityBlock]] = []

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    async def capture(chat_id: int, block: AgentActivityBlock) -> None:
        received.append((chat_id, block))

    service = AcpAgentService(SessionRegistry(), program="agent", args=[], spawner=fake_spawn, connector=fake_connect)
    await service.new_session(chat_id=7, workspace=tmp_path)

    block = AgentActivityBlock(kind="think", title="t", status="completed", text="x")
    await service._forward_activity_event("activity-session", block)
    assert received == []

    service.set_activity_event_handler(capture)
    await service._forward_activity_event("unknown-session", block)
    assert received == []

    await service._forward_activity_event("activity-session", block)
    assert received == [(7, block)]


async def test_new_session_replaces_previous_and_shuts_down(tmp_path: Path, monkeypatch):
    first = FakeProcess()
    second = FakeProcess()
    calls: list[FakeProcess] = []

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        process = first if not calls else second
        calls.append(process)
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        conn = FakeConnection(session_id=f"s-{len(calls)}")
        conn.client = client
        return conn

    state_file = tmp_path / "channel-state.json"
    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
        channel_state_file=state_file,
    )
    await service.new_session(chat_id=5, workspace=tmp_path)
    await service.new_session(chat_id=5, workspace=tmp_path)

    assert first.terminated
    assert not second.terminated
    assert load_session_chat_map(state_file) == {"s-2": 5}

    async def fake_wait_for(fut, **kwargs):
        del kwargs
        fut.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    hanging = FakeProcess()
    await service._shutdown(hanging)
    assert hanging.killed

    finished = FakeProcess()
    finished.returncode = 0
    await service._shutdown(finished)
    assert not finished.terminated


async def test_new_session_passes_start_new_session_to_spawner(tmp_path: Path):
    process = FakeProcess()
    spawn_kwargs: list[dict] = []

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args
        spawn_kwargs.append(dict(kwargs))
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        conn = FakeConnection(session_id="s")
        conn.client = client
        return conn

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)
    assert spawn_kwargs[0].get("start_new_session") is True


async def test_shutdown_kills_process_group_on_posix(monkeypatch):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    process = FakeProcess(pid=1234)

    killed_groups: list[tuple[int, int]] = []

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        killed_groups.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)

    await service._shutdown(process)

    assert killed_groups == [(1234, signal.SIGTERM)]
    assert not process.terminated


async def test_shutdown_kills_process_group_on_timeout(monkeypatch):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    process = FakeProcess(pid=5678)

    killed_groups: list[tuple[int, int]] = []

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        killed_groups.append((pgid, sig))

    async def fake_wait_for(fut, **kwargs):
        del kwargs
        fut.close()
        raise TimeoutError

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    await service._shutdown(process)

    assert killed_groups == [(5678, signal.SIGTERM), (5678, signal.SIGKILL)]
    assert not process.killed


async def test_shutdown_falls_back_to_terminate_on_oserror(monkeypatch):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    process = FakeProcess(pid=9999)

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        raise OSError

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)

    await service._shutdown(process)

    assert process.terminated


async def test_shutdown_ignores_missing_process_race_on_sigterm(monkeypatch):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    process = FakeProcess(pid=9999)

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        del pgid, sig
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)

    await service._shutdown(process)

    assert not process.terminated


async def test_shutdown_falls_back_to_kill_on_oserror_and_timeout(monkeypatch):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    process = FakeProcess(pid=9999)
    call_count = 0

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        nonlocal call_count
        call_count += 1
        raise OSError

    async def fake_wait_for(fut, **kwargs):
        del kwargs
        fut.close()
        raise TimeoutError

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    await service._shutdown(process)

    assert call_count == 2  # noqa: PLR2004
    assert process.killed


async def test_shutdown_reraises_unexpected_terminate_oserror(monkeypatch):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    process = FakeProcess(pid=9999)

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        del pgid, sig
        raise OSError(errno.EPERM, "operation not permitted")

    def fake_terminate() -> None:
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    monkeypatch.setattr(process, "terminate", fake_terminate)

    with pytest.raises(PermissionError):
        await service._shutdown(process)


async def test_shutdown_reraises_unexpected_kill_oserror_on_timeout(monkeypatch):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    process = FakeProcess(pid=9999)

    def fake_getpgid(pid: int) -> int:
        return pid

    def fake_killpg(pgid: int, sig: int) -> None:
        del pgid, sig
        raise OSError(errno.EPERM, "operation not permitted")

    async def fake_wait_for(fut, **kwargs):
        del kwargs
        fut.close()
        raise TimeoutError

    def fake_kill() -> None:
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(process, "kill", fake_kill)

    with pytest.raises(PermissionError):
        await service._shutdown(process)


async def test_resolve_file_uri_resources_keeps_non_file_payloads(tmp_path: Path):
    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    response = AgentReply(
        text="ok",
        files=(
            FilePayload(name="inline.txt", text_content="plain text"),
            FilePayload(name="bin.dat", data_base64=base64.b64encode(b"x").decode("ascii")),
        ),
    )
    resolved = service._resolve_file_uri_resources(response=response, workspace=tmp_path)
    assert resolved.files == response.files
    assert resolved.images == ()


async def test_resolve_file_uri_resources_reports_unreadable_file(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    file_path = workspace / "a.txt"
    file_path.write_text("x")

    service = AcpAgentService(SessionRegistry(), program="agent", args=[])
    response = AgentReply(
        text="",
        files=(FilePayload(name="a.txt", text_content=file_path.as_uri(), mime_type="text/plain"),),
    )

    def fake_read_bytes(self: Path) -> bytes:
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    resolved = service._resolve_file_uri_resources(response=response, workspace=workspace)
    assert resolved.files == ()
    assert "Attachment warning: a.txt: unreadable file" in resolved.text


async def test_resolve_local_file_uri_validation(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    file_path = workspace / "ok.txt"
    file_path.write_text("ok")
    folder_path = workspace / "dir"
    folder_path.mkdir()

    _, warning_scheme = AcpAgentService._resolve_local_file_uri("http://example.com/x", workspace)
    assert warning_scheme == "unsupported URI scheme `http`"

    _, warning_host = AcpAgentService._resolve_local_file_uri("file://remote/abc", workspace)
    assert warning_host == "unsupported file URI host `remote`"

    _, warning_empty = AcpAgentService._resolve_local_file_uri("file://", workspace)
    assert warning_empty == "empty file URI path"

    _, warning_missing = AcpAgentService._resolve_local_file_uri((workspace / "missing.txt").as_uri(), workspace)
    assert warning_missing == "file not found"

    _, warning_not_file = AcpAgentService._resolve_local_file_uri(folder_path.as_uri(), workspace)
    assert warning_not_file == "path is not a file"

    def fake_resolve(self: Path, *, strict: bool = False) -> Path:
        del strict
        raise OSError("boom")

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    _, warning_os_error = AcpAgentService._resolve_local_file_uri(file_path.as_uri(), workspace)
    assert warning_os_error is not None
    assert warning_os_error.startswith("invalid file path")

    monkeypatch.undo()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    _, warning_outside = AcpAgentService._resolve_local_file_uri(outside.as_uri(), workspace)
    assert warning_outside == "path is outside active workspace"

    resolved, no_warning = AcpAgentService._resolve_local_file_uri(file_path.as_uri(), workspace)
    assert resolved == file_path.resolve()
    assert no_warning is None


async def test_log_text_preview_compacts_and_truncates():
    short = log_text_preview("  hello   world ")
    assert short == "hello world"

    long_text = "y" * 400
    preview = log_text_preview(long_text)
    assert preview.endswith("...")
    assert len(preview) == LOG_TEXT_PREVIEW_MAX_CHARS + 3

    assert log_text_preview("   ") == "<empty>"


def _commands_service(connection: CommandsConnection) -> AcpAgentService:
    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return FakeProcess()

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        return connection

    return AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )


async def test_available_commands_update_is_captured_and_exposed_per_chat(tmp_path: Path):
    service = _commands_service(CommandsConnection(session_id="cmds-session"))
    assert service.get_available_commands(chat_id=7) is None

    await service.new_session(chat_id=7, workspace=tmp_path)

    commands = service.get_available_commands(chat_id=7)
    assert commands is not None
    assert [(command.name, command.description, command.hint) for command in commands] == [
        ("goal", "Set or manage a persistent goal", "<objective> | status"),
        ("deep-research", "Research a topic", ""),
    ]
    assert service.get_available_commands(chat_id=8) is None


async def test_available_commands_are_replaced_by_the_latest_advertisement(tmp_path: Path):
    service = _commands_service(ReadvertiseCommandsConnection(session_id="cmds-session"))
    await service.new_session(chat_id=7, workspace=tmp_path)

    commands = service.get_available_commands(chat_id=7)
    assert commands is not None
    assert [command.name for command in commands] == ["goal", "deep-research"]

    await service.prompt(chat_id=7, text="hello")

    commands = service.get_available_commands(chat_id=7)
    assert commands is not None
    assert [(command.name, command.description, command.hint) for command in commands] == [
        ("extra-1", "Command added on prompt 1", "<target>"),
    ]

    await service.prompt(chat_id=7, text="hello again")

    commands = service.get_available_commands(chat_id=7)
    assert commands is not None
    assert [command.name for command in commands] == ["extra-2"]


async def test_available_commands_are_cleared_when_the_session_stops(tmp_path: Path):
    service = _commands_service(CommandsConnection(session_id="cmds-session"))
    await service.new_session(chat_id=7, workspace=tmp_path)
    assert service.get_available_commands(chat_id=7)

    await service.new_session(chat_id=7, workspace=tmp_path)
    assert service.get_available_commands(chat_id=7)

    await service.load_session(chat_id=7, session_id="loaded-session", workspace=tmp_path)
    assert service.get_available_commands(chat_id=7) == ()

    await service.new_session(chat_id=7, workspace=tmp_path)
    await service.stop(chat_id=7)
    assert service.get_available_commands(chat_id=7) is None
