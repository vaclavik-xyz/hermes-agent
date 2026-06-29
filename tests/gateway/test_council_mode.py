from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.council import handle_council_event, prepare_council_handoff
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _config(tmp_path):
    return GatewayConfig(
        council={
            "enabled": True,
            "state_path": str(tmp_path / "council-state.json"),
            "chats": [
                {
                    "chat_id": "-1004443743090",
                    "bot_ids": ["8386801222", "8533244178"],
                    "bot_profiles": {"8386801222": "default", "8533244178": "work"},
                    "moderator_bot_id": "8386801222",
                    "owner_user_ids": ["6168627430"],
                    "max_turns": 2,
                    "timeout_seconds": 120,
                }
            ],
        }
    )


def _event(text, *, user_id="6168627430", user_name="Filip", is_bot=False):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1004443743090",
        chat_type="group",
        user_id=user_id,
        user_name=user_name,
        is_bot=is_bot,
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


def test_human_start_allows_only_moderator_to_consume_trigger(tmp_path):
    cfg = _config(tmp_path)
    event = _event("Proberte to spolu na 2 kola a dejte mi závěr")

    moderator = handle_council_event(cfg, event, own_bot_id="8386801222")
    assert moderator.action == "continue"
    assert "Council mode instruction" in moderator.event.text
    assert "Maximum bot handoff turns: 2" in moderator.event.text

    non_moderator = handle_council_event(cfg, event, own_bot_id="8533244178")
    assert non_moderator.action == "skip"


def test_bot_messages_are_skipped_unless_council_session_is_active(tmp_path):
    cfg = _config(tmp_path)
    bot_event = _event("Anton says hello", user_id="8386801222", user_name="Anton", is_bot=True)

    decision = handle_council_event(cfg, bot_event, own_bot_id="8533244178")
    assert decision.action == "skip"
    assert bot_event.source.role_authorized is False


def test_active_council_authorizes_other_bot_and_final_turn_stops(tmp_path):
    cfg = _config(tmp_path)
    handle_council_event(cfg, _event("/council 2"), own_bot_id="8386801222")

    first = _event("Anton: první pohled", user_id="8386801222", user_name="Anton", is_bot=True)
    first_decision = handle_council_event(cfg, first, own_bot_id="8533244178")
    assert first_decision.action == "continue"
    assert first.source.role_authorized is True
    assert "Turn 1/2" in first_decision.event.text
    assert "Keep it concise" in first_decision.event.text

    second = _event("Klaudie: doplnění", user_id="8533244178", user_name="Klaudie", is_bot=True)
    second_decision = handle_council_event(cfg, second, own_bot_id="8386801222")
    assert second_decision.action == "continue"
    assert second.source.role_authorized is True
    assert "Turn 2/2" in second_decision.event.text
    assert "final allowed handoff" in second_decision.event.text

    third = _event("Anton: should not continue", user_id="8386801222", user_name="Anton", is_bot=True)
    third_decision = handle_council_event(cfg, third, own_bot_id="8533244178")
    assert third_decision.action == "skip"


def test_prepare_council_handoff_targets_other_profile(tmp_path):
    cfg = _config(tmp_path)
    start = _event("proberte to spolu")
    handle_council_event(cfg, start, own_bot_id="8386801222")

    handoff = prepare_council_handoff(cfg, start, "8386801222", "Antonův první pohled")
    assert handoff is not None
    assert handoff["profile"] == "work"
    assert handoff["target"] == "telegram:-1004443743090"
    assert "Antonův první pohled" in handoff["prompt"]


def test_human_stop_is_acknowledged_only_by_moderator(tmp_path):
    cfg = _config(tmp_path)
    handle_council_event(cfg, _event("proberte to spolu"), own_bot_id="8386801222")

    stop = _event("stop")
    moderator = handle_council_event(cfg, stop, own_bot_id="8386801222")
    assert moderator.action == "respond"
    assert moderator.response is not None
    assert "Zastavuju" in moderator.response

    # Start again for the non-moderator branch.
    handle_council_event(cfg, _event("proberte to spolu"), own_bot_id="8386801222")
    non_moderator = handle_council_event(cfg, stop, own_bot_id="8533244178")
    assert non_moderator.action == "skip"


def test_auto_council_topic_starts_without_command_and_stays_topic_scoped(tmp_path):
    cfg = _config(tmp_path)
    cfg.council["chats"][0]["auto_start_thread_ids"] = ["2"]
    cfg.council["chats"][0]["auto_start_max_turns"] = 3

    general = _event("obyčejná zpráva bez triggeru")
    assert handle_council_event(cfg, general, own_bot_id="8386801222").event.text == general.text

    topic = _event("Potřebuju oba pohledy na tohle rozhodnutí")
    topic.source.thread_id = "2"
    moderator = handle_council_event(cfg, topic, own_bot_id="8386801222")
    assert moderator.action == "continue"
    assert "Council mode instruction" in moderator.event.text
    assert "Maximum bot handoff turns: 3" in moderator.event.text
    assert "auto-started" in moderator.event.text

    non_moderator = handle_council_event(cfg, topic, own_bot_id="8533244178")
    assert non_moderator.action == "skip"


def test_auto_council_topic_does_not_start_from_stop_word(tmp_path):
    cfg = _config(tmp_path)
    cfg.council["chats"][0]["auto_start_thread_ids"] = ["2"]

    stop = _event("stop")
    stop.source.thread_id = "2"
    decision = handle_council_event(cfg, stop, own_bot_id="8386801222")
    assert decision.action == "continue"
    assert "Council mode instruction" not in decision.event.text


def test_telegram_build_message_event_marks_bot_authors():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    adapter._GENERAL_TOPIC_THREAD_ID = "1"  # type: ignore[attr-defined]
    adapter._get_dm_topic_info = lambda chat_id, thread_id: None  # type: ignore[method-assign]
    adapter._cache_dm_topic_from_message = lambda chat_id, thread_id, topic_name: None  # type: ignore[method-assign]
    adapter._extract_rich_reply_text = lambda reply_to_message: None  # type: ignore[method-assign]

    user = SimpleNamespace(id=8533244178, is_bot=True, full_name="Klaudie", first_name="Klaudie")
    chat = SimpleNamespace(id=-1004443743090, type="supergroup", title="Kortex", is_forum=False)
    msg = SimpleNamespace(
        chat=chat,
        from_user=user,
        message_id=123,
        message_thread_id=None,
        is_topic_message=False,
        text="bot text",
        caption=None,
        reply_to_message=None,
        date=None,
    )

    event = adapter._build_message_event(msg, MessageType.TEXT, update_id=10)
    assert event.source.is_bot is True
    assert event.source.user_id == "8533244178"
