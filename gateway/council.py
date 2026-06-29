"""Bounded bot-council mode for messaging gateways.

This module keeps bot-to-bot conversations opt-in and bounded. It is designed
for two independent Hermes gateways that share a chat (for example a Telegram
"council" group with a default/personal bot and a work bot). The default safety
posture remains: bot-authored messages are ignored/unauthorized unless a human
starts a short council session.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

_START_RE = re.compile(
    r"(?:^|\s)(?:/council|/debata|/diskuse|proberte\s+to\s+spolu|proberte|pobavte\s+se|diskutujte|vy\s+dva)(?:\s|$)",
    re.IGNORECASE,
)
_STOP_WORDS = {"stop", "konec", "zastavit", "dost", "stačí", "staci"}
_STOP_COMMANDS = {"/stop", "/stopdebata", "/stopcouncil", "/konec", "/zastavit"}
_TURNS_RE = re.compile(r"(?:max\s*)?(\d{1,2})\s*(?:kol|kola|turn|turns|odpověd|odpoved)", re.IGNORECASE)


@dataclass
class CouncilDecision:
    """Decision returned to the gateway before normal auth/dispatch."""

    action: str = "continue"  # continue | skip | respond
    event: Any = None
    response: Optional[str] = None


@contextmanager
def _locked_json_state(path: Path) -> Iterator[Dict[str, Any]]:
    """Read/modify/write a shared JSON state file under an advisory lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        try:
            import fcntl  # Unix/macOS only; Hermes gateway targets include macOS/Linux.

            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            logger.debug("Council state lock unavailable; continuing best-effort", exc_info=True)
        try:
            try:
                state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                if not isinstance(state, dict):
                    state = {}
            except Exception:
                logger.warning("Council state file %s is invalid; resetting", path)
                state = {}
            yield state
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        finally:
            try:
                import fcntl

                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _string_set(value: Any) -> set[str]:
    return {str(v).strip() for v in _as_list(value) if str(v).strip()}


def _council_config(config: Any) -> Dict[str, Any]:
    raw = getattr(config, "council", None)
    return raw if isinstance(raw, dict) else {}


def _chat_entry(council: Dict[str, Any], event: Any) -> Optional[Dict[str, Any]]:
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    if not chat_id:
        return None
    for entry in _as_list(council.get("chats")):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("chat_id", "")).strip() != chat_id:
            continue
        topics = _string_set(entry.get("thread_ids") or entry.get("topics"))
        if topics:
            thread_id = str(getattr(source, "thread_id", "") or "general")
            if thread_id not in topics:
                continue
        return entry
    return None


def _state_path(council: Dict[str, Any], entry: Dict[str, Any]) -> Path:
    configured = entry.get("state_path") or council.get("state_path")
    if configured:
        return Path(str(configured)).expanduser()
    return get_hermes_home() / "council" / "state.json"


def _state_key(event: Any) -> str:
    source = event.source
    thread = str(getattr(source, "thread_id", "") or "general")
    return f"{source.platform.value}:{source.chat_id}:{thread}"


def _configured_bot_ids(entry: Dict[str, Any]) -> list[str]:
    return list(_string_set(entry.get("bot_ids")))


def _moderator_bot_id(entry: Dict[str, Any], bot_ids: list[str]) -> Optional[str]:
    configured = str(entry.get("moderator_bot_id") or "").strip()
    return configured or (bot_ids[0] if bot_ids else None)


def _is_owner(entry: Dict[str, Any], user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    owners = _string_set(entry.get("owner_user_ids") or entry.get("owners"))
    return not owners or str(user_id) in owners


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_stop_text(text: str) -> bool:
    stripped = (text or "").strip()
    lowered = _normalize_text(stripped)
    first = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    return lowered in _STOP_WORDS or first in _STOP_COMMANDS


def _is_start_text(text: str) -> bool:
    return bool(_START_RE.search(text or ""))


def _event_thread_id(event: Any) -> str:
    source = getattr(event, "source", None)
    return str(getattr(source, "thread_id", "") or "general")


def _is_auto_start_thread(entry: Dict[str, Any], event: Any) -> bool:
    topics = _string_set(
        entry.get("auto_start_thread_ids")
        or entry.get("auto_start_topics")
        or entry.get("auto_council_thread_ids")
    )
    if not topics:
        return False
    return _event_thread_id(event) in topics


def _parse_max_turns(text: str, entry: Dict[str, Any], council: Dict[str, Any]) -> int:
    default = entry.get("max_turns", council.get("max_turns", 6))
    try:
        parsed_default = int(default)
    except Exception:
        parsed_default = 6
    match = _TURNS_RE.search(text or "")
    if match:
        try:
            return max(1, min(int(match.group(1)), 20))
        except Exception:
            pass
    return max(1, min(parsed_default, 20))


def _auto_start_max_turns(entry: Dict[str, Any], council: Dict[str, Any]) -> int:
    value = entry.get("auto_start_max_turns", council.get("auto_start_max_turns", 3))
    try:
        return max(1, min(int(value), 20))
    except Exception:
        return 3


def _timeout_seconds(entry: Dict[str, Any], council: Dict[str, Any]) -> int:
    value = entry.get("timeout_seconds", council.get("timeout_seconds", 300))
    try:
        return max(30, int(value))
    except Exception:
        return 300


def _active_session(state: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    session = state.get(key)
    if not isinstance(session, dict) or not session.get("active"):
        return None
    try:
        if float(session.get("expires_at", 0)) <= time.time():
            session["active"] = False
            session["stopped_reason"] = "timeout"
            return None
    except Exception:
        session["active"] = False
        session["stopped_reason"] = "invalid-expires-at"
        return None
    return session


def _with_council_instruction(event: Any, instruction: str) -> Any:
    text = (getattr(event, "text", "") or "").rstrip()
    new_text = f"{text}\n\n[Council mode instruction: {instruction}]"
    return dataclasses.replace(event, text=new_text)


def handle_council_event(config: Any, event: Any, own_bot_id: Optional[str]) -> CouncilDecision:
    """Apply bounded council start/stop/bot-handoff rules.

    This runs before normal authorization. It may mark bot-authored messages as
    role_authorized only while a human-started council session is active.
    """

    council = _council_config(config)
    if not council or council.get("enabled") is not True:
        return CouncilDecision(event=event)
    entry = _chat_entry(council, event)
    if not entry:
        return CouncilDecision(event=event)

    source = event.source
    bot_ids = _configured_bot_ids(entry)
    if not bot_ids:
        return CouncilDecision(event=event)
    own_bot_id = str(own_bot_id or "").strip() or None
    moderator = _moderator_bot_id(entry, bot_ids)
    key = _state_key(event)
    state_path = _state_path(council, entry)
    text = getattr(event, "text", "") or ""
    now = time.time()

    with _locked_json_state(state_path) as state:
        sessions = state.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
            state["sessions"] = sessions
        session = _active_session(sessions, key)

        is_bot = bool(getattr(source, "is_bot", False))
        user_id = str(getattr(source, "user_id", "") or "")

        # Human stop wins and is acknowledged by the moderator only to avoid duplicates.
        if not is_bot and session and _is_owner(entry, user_id) and _is_stop_text(text):
            session["active"] = False
            session["stopped_reason"] = "human-stop"
            session["stopped_at"] = now
            if own_bot_id and moderator and own_bot_id != str(moderator):
                return CouncilDecision(action="skip", event=event)
            return CouncilDecision(
                action="respond",
                event=event,
                response="Zastavuju council debatu. Dál budeme reagovat jen na tvoje zprávy nebo explicitní oslovení.",
            )

        # While an auto-council session is active, only the moderator should
        # consume human follow-up messages. Otherwise both gateways may answer
        # the same human message before the bot handoff begins.
        if (
            not is_bot
            and session
            and _is_auto_start_thread(entry, event)
            and own_bot_id
            and moderator
            and own_bot_id != str(moderator)
        ):
            return CouncilDecision(action="skip", event=event)

        # Human start creates a bounded session. Only the moderator consumes the trigger.
        explicit_start = _is_start_text(text)
        auto_start = (
            not explicit_start
            and not session
            and _is_auto_start_thread(entry, event)
            and not _is_stop_text(text)
        )
        if not is_bot and _is_owner(entry, user_id) and (explicit_start or auto_start):
            max_turns = _parse_max_turns(text, entry, council) if explicit_start else _auto_start_max_turns(entry, council)
            timeout = _timeout_seconds(entry, council)
            sessions[key] = {
                "active": True,
                "chat_id": str(source.chat_id),
                "thread_id": _event_thread_id(event),
                "owner_user_id": user_id,
                "bot_ids": bot_ids,
                "moderator_bot_id": moderator,
                "max_turns": max_turns,
                "turn_count": 0,
                "auto_started": bool(auto_start),
                "started_at": now,
                "expires_at": now + timeout,
            }
            if own_bot_id and moderator and own_bot_id != str(moderator):
                return CouncilDecision(action="skip", event=event)
            instruction = (
                f"You are the council moderator. Start a bounded bot-to-bot discussion for Filip. Invite the other bot explicitly. Maximum bot handoff turns: {max_turns}. If Filip writes stop/konec/zastavit/dost, stop immediately."
            )
            if auto_start:
                instruction += " This council was auto-started because the message was posted in an auto-council topic; keep the discussion especially concise."
            return CouncilDecision(
                event=_with_council_instruction(
                    event,
                    instruction,
                )
            )

        # Bot-authored messages are admitted only inside an active bounded session.
        if is_bot:
            if not session:
                return CouncilDecision(action="skip", event=event)
            if user_id not in set(bot_ids):
                return CouncilDecision(action="skip", event=event)
            if own_bot_id and user_id == own_bot_id:
                return CouncilDecision(action="skip", event=event)
            try:
                max_turns = int(session.get("max_turns", entry.get("max_turns", 6)))
                turn_count = int(session.get("turn_count", 0))
            except Exception:
                max_turns = 6
                turn_count = 0
            if turn_count >= max_turns:
                session["active"] = False
                session["stopped_reason"] = "max-turns"
                session["stopped_at"] = now
                return CouncilDecision(action="skip", event=event)
            turn_count += 1
            session["turn_count"] = turn_count
            session["expires_at"] = now + _timeout_seconds(entry, council)
            final_turn = turn_count >= max_turns
            if final_turn:
                session["active"] = False
                session["stopped_reason"] = "max-turns-after-final"
                session["stopped_at"] = now

            # Bypass normal user allowlist for this one bounded bot handoff.
            source.role_authorized = True
            instruction = (
                f"You are in council mode responding to the other bot for Filip. Turn {turn_count}/{max_turns}. "
                + (
                    "This is the final allowed handoff: give a concise conclusion for Filip and do not invite another bot reply."
                    if final_turn
                    else "Keep it concise and advance the discussion; do not loop aimlessly."
                )
            )
            return CouncilDecision(event=_with_council_instruction(event, instruction))

    return CouncilDecision(event=event)


def prepare_council_handoff(config: Any, event: Any, own_bot_id: Optional[str], response: str) -> Optional[Dict[str, str]]:
    """Record an assistant response and prepare the next bounded bot handoff.

    Telegram may not reliably deliver bot-authored messages to every other bot
    in a group. The runtime therefore uses the same bounded council state to
    synthesize the next participant turn explicitly, while still showing the
    reply publicly from that participant's own profile/bot.
    """

    if not response or not str(response).strip():
        return None
    council = _council_config(config)
    if not council.get("enabled"):
        return None
    entry = _chat_entry(council, event)
    if not entry:
        return None
    bot_ids = _configured_bot_ids(entry)
    own_bot_id = str(own_bot_id or "").strip()
    if not own_bot_id or own_bot_id not in set(bot_ids):
        return None
    key = _state_key(event)
    state_path = _state_path(council, entry)
    source = getattr(event, "source", None)

    with _locked_json_state(state_path) as state:
        sessions = state.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
            state["sessions"] = sessions
        session = _active_session(sessions, key)
        if not session:
            return None
        try:
            max_turns = int(session.get("max_turns", entry.get("max_turns", 6)))
            turn_count = int(session.get("turn_count", 0))
        except Exception:
            max_turns = 6
            turn_count = 0
        if turn_count >= max_turns:
            session["active"] = False
            session["stopped_reason"] = "max-turns"
            session["stopped_at"] = time.time()
            return None

        next_bot_id = next((bot_id for bot_id in bot_ids if bot_id != own_bot_id), None)
        profiles = entry.get("bot_profiles") or council.get("bot_profiles") or {}
        if not isinstance(profiles, dict):
            profiles = {}
        next_profile = str(profiles.get(str(next_bot_id), "") or "").strip()
        if not next_bot_id or not next_profile:
            logger.warning("Council handoff missing bot_profiles mapping for bot_id=%s", next_bot_id)
            return None

        turn_count += 1
        session["turn_count"] = turn_count
        session["last_bot_id"] = own_bot_id
        session["last_response"] = str(response)[:4000]
        session["expires_at"] = time.time() + _timeout_seconds(entry, council)
        final_turn = turn_count >= max_turns
        if final_turn:
            session["active"] = False
            session["stopped_reason"] = "max-turns-after-final"
            session["stopped_at"] = time.time()

    thread_id = _event_thread_id(event)
    target = f"telegram:{getattr(source, 'chat_id', '')}"
    if thread_id and thread_id != "general":
        target = f"{target}:{thread_id}"
    prompt = (
        f"Jsi účastník řízené Kortex bot debaty pro Filipa. Předchozí bot napsal:\n\n{response}\n\n"
        f"Odpověz jako svůj profil v češtině, stručně a navazuj přímo na předchozí botovu zprávu. "
        f"Turn {turn_count}/{max_turns}. "
        + ("Toto je poslední povolený handoff: zakonči debatu stručným závěrem a nevyzývej dalšího bota." if final_turn else "Posuň debatu dál a klidně explicitně vyzvi druhého bota k reakci.")
    )
    return {"profile": next_profile, "target": target, "prompt": prompt}
