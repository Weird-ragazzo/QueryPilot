"""Bounded, process-local temporary conversations isolated by anonymous session."""
from __future__ import annotations

import hashlib
import time
import uuid
from threading import RLock

_lock = RLock()
_store: dict[tuple[str, str], dict] = {}
_TTL_SECONDS = 60 * 60 * 12
_MAX_CONVERSATIONS = 1000
_MAX_TURNS = 50


def _owner(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def create_conversation(session_id: str) -> dict:
    now = time.time()
    key = (_owner(session_id), uuid.uuid4().hex)
    with _lock:
        for old_key, value in list(_store.items()):
            if now - value["updated_at"] > _TTL_SECONDS:
                _store.pop(old_key, None)
        while len(_store) >= _MAX_CONVERSATIONS:
            oldest = min(_store, key=lambda item: _store[item]["updated_at"])
            _store.pop(oldest, None)
        _store[key] = {"conversation_id": key[1], "created_at": now, "updated_at": now, "messages": []}
    return {"conversation_id": key[1], "messages": []}


def get_conversation(session_id: str, conversation_id: str, create: bool = False) -> dict:
    key = (_owner(session_id), conversation_id)
    with _lock:
        conversation = _store.get(key)
        if conversation and time.time() - conversation["updated_at"] <= _TTL_SECONDS:
            conversation["updated_at"] = time.time()
            return conversation
        _store.pop(key, None)
    if create:
        return _store[(_owner(session_id), create_conversation(session_id)["conversation_id"])]
    raise KeyError("Conversation is unavailable or expired. Start a new conversation.")


def append_turn(session_id: str, conversation_id: str, user_message: dict, assistant_message: dict) -> None:
    conversation = get_conversation(session_id, conversation_id)
    with _lock:
        conversation["messages"].extend([user_message, assistant_message])
        conversation["messages"] = conversation["messages"][-_MAX_TURNS * 2:]
        conversation["updated_at"] = time.time()


def reset_conversation(session_id: str, conversation_id: str) -> dict:
    conversation = get_conversation(session_id, conversation_id)
    with _lock:
        conversation["messages"] = []
        conversation["updated_at"] = time.time()
    return {"conversation_id": conversation_id, "messages": []}


def remove_source_results(session_id: str, source_id: str) -> None:
    owner = _owner(session_id)
    with _lock:
        for (conv_owner, _), conversation in _store.items():
            if conv_owner != owner:
                continue
            for message in conversation["messages"]:
                if source_id in message.get("database_ids", []):
                    message["historical_source_deleted"] = True
                    if message.get("role") == "assistant":
                        message["result"] = {"summary": message.get("content", ""),
                                             "result_not_retained": "The source for this result was deleted."}
