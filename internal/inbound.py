"""SSE dispatch and inbound DM, group, and file handling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import httpx

from gateway.platforms.base import MessageEvent, MessageType

from .bech32 import normalize_channel_id, normalize_npub
from .constants import (
    DOWNLOAD_TIMEOUT,
    INBOUND_DEDUP_MAX,
    SSE_RETRY_DELAY_INITIAL,
    SSE_RETRY_DELAY_MAX,
    SSE_STALE_TIMEOUT,
)
from .env import _community_download_all, _group_context_enabled, _pairing_enabled
from .groups import (
    _group_file_pending_path,
    _group_file_pointer_path,
    _group_sender_is_authorized,
    _group_slash_command,
    _is_superseded_replay,
    _mention_remainder,
    _mentions_bot,
    _pending_inbox_key,
    _remember_channel,
    _reply_to_bot,
    _sender_is_authorized,
    _truncate_npub,
)
from . import paths
from .paths import (
    _inbound_media_max_bytes,
    _message_type_for_mime,
    _mime_for_attachment,
    _sanitize_filename,
    _unique_path,
    sandbox_breadcrumb_path,
    sandbox_turn_path,
)

logger = logging.getLogger("hermes_plugins.vector_platform.adapter")


class InboundDispatcher:
    """Turns sidecar SSE events into Hermes messages.

    Callbacks into the adapter (``handle_message``, ``send``, and any method
    a test replaces on the instance) go through ``self.adapter``.
    """

    def __init__(self, adapter) -> None:
        self.adapter = adapter


    async def _sse_listener(self) -> None:
        url = f"{self.adapter.bridge_url}/events"
        backoff = SSE_RETRY_DELAY_INITIAL

        while self.adapter._running:
            if self.adapter._bridge_process and self.adapter._bridge_process.poll() is not None:
                await self.adapter._handle_bridge_exit()
                break

            try:
                logger.debug("Vector SSE: connecting to %s", url)
                headers = {
                    **self.adapter._token_headers(),
                    "Accept": "text/event-stream",
                }
                if self.adapter._sse_last_event_id:
                    headers["Last-Event-ID"] = self.adapter._sse_last_event_id
                async with self.adapter._http_client.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=None,
                ) as response:
                    if response.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"/events returned {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    backoff = SSE_RETRY_DELAY_INITIAL
                    logger.info("Vector SSE: connected")

                    buffer = ""
                    pending_id = ""
                    aiter = response.aiter_text().__aiter__()
                    while self.adapter._running:
                        try:
                            chunk = await asyncio.wait_for(
                                aiter.__anext__(), timeout=SSE_STALE_TIMEOUT
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Vector SSE: no data in %.0fs, reconnecting",
                                SSE_STALE_TIMEOUT,
                            )
                            break
                        except StopAsyncIteration:
                            break

                        if (
                            self.adapter._bridge_process
                            and self.adapter._bridge_process.poll() is not None
                        ):
                            await self.adapter._handle_bridge_exit()
                            return

                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.rstrip("\r")
                            if not line or line.startswith(":"):
                                continue
                            if line.startswith("id:"):
                                pending_id = line[3:].strip()
                                continue
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if not data_str:
                                    continue
                                try:
                                    data = json.loads(data_str)
                                    await self.adapter._dispatch_sse_event(data)
                                    # Commit the resume point only once the event
                                    # is handed off; a failure leaves it
                                    # uncommitted so the sidecar replays it.
                                    if pending_id:
                                        self.adapter._sse_last_event_id = pending_id
                                except json.JSONDecodeError:
                                    logger.debug(
                                        "Vector SSE: invalid JSON: %s",
                                        data_str[:120],
                                    )
                                except Exception:
                                    logger.exception(
                                        "Vector SSE: error handling event"
                                    )
                                finally:
                                    pending_id = ""

            except asyncio.CancelledError:
                break
            except httpx.HTTPError as e:
                if self.adapter._running:
                    logger.warning(
                        "Vector SSE: HTTP error: %s (reconnecting in %.0fs)",
                        e,
                        backoff,
                    )
            except Exception as e:
                if self.adapter._running:
                    logger.warning(
                        "Vector SSE: error: %s (reconnecting in %.0fs)",
                        e,
                        backoff,
                    )

            if self.adapter._running:
                if (
                    self.adapter._bridge_process
                    and self.adapter._bridge_process.poll() is not None
                ):
                    await self.adapter._handle_bridge_exit()
                    break
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, SSE_RETRY_DELAY_MAX)

    async def _dispatch_sse_event(self, data: dict) -> None:
        event_type = data.get("type", "")
        if event_type == "ready":
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            npub = (inner or {}).get("npub")
            if npub:
                self.adapter._npub = npub
            logger.info("Vector SSE: sidecar emitted 'ready'")
        elif event_type == "message":
            await self.adapter._handle_message_event(data)
        elif event_type == "message_update":
            await self.adapter._handle_message_update(data)
        elif event_type == "message_delete":
            await self.adapter._handle_message_delete(data)
        elif event_type == "community_joined":
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            inner = inner or {}
            channels = inner.get("channels") if isinstance(inner.get("channels"), list) else []
            community_id = str(inner.get("community_id") or "")
            community_name = str(inner.get("name") or "").strip()
            if channels:
                await self.adapter._notify_joined_channels(
                    community_id, channels, community_name=community_name
                )
            elif community_id:
                await self.adapter._sync_joined_channels()
        else:
            logger.debug("Vector SSE: unhandled event type '%s'", event_type)

    async def _handle_message_event(self, msg_data: dict) -> None:
        if isinstance(msg_data, dict) and msg_data.get("type") == "message" and "data" in msg_data:
            msg_data = msg_data["data"]
        if not isinstance(msg_data, dict):
            return

        if msg_data.get("is_mine"):
            return
        if msg_data.get("is_group"):
            await self.adapter._handle_group_message(msg_data)
            return

        msg_id = str(msg_data.get("id") or "")
        if msg_id and self.adapter._is_duplicate(msg_id):
            logger.debug("Vector: dropping duplicate inbound id=%s", msg_id[:16])
            return

        text = str(msg_data.get("text") or "")
        attachments = msg_data.get("attachments") if isinstance(msg_data.get("attachments"), list) else []
        is_file = bool(msg_data.get("is_file") or attachments)
        if not text.strip() and not is_file:
            return

        raw_peer = msg_data.get("npub") or msg_data.get("chat_id") or ""
        peer = normalize_npub(raw_peer) or str(raw_peer).strip()
        if not peer:
            return

        bot_npub = normalize_npub(self.adapter._npub or "") if self.adapter._npub else None
        if bot_npub and peer == bot_npub:
            return

        if await self.adapter._is_blocked(peer):
            logger.info(
                "Vector: dropping blocked sender %s",
                _truncate_npub(peer),
            )
            return

        # VECTOR_PAIRING=off: drop before handle_message so pairing codes are not sent.
        if not _pairing_enabled() and not _sender_is_authorized(peer):
            logger.info(
                "Vector: dropping unauthorized sender %s (VECTOR_PAIRING=off)",
                _truncate_npub(peer),
            )
            return

        if await self.adapter._try_block_command(peer, text):
            if msg_id:
                self.adapter._record_last_inbound(peer, msg_id)
            return

        if await self.adapter._try_invite_command(peer, text):
            if msg_id:
                self.adapter._record_last_inbound(peer, msg_id)
            return

        if msg_id:
            self.adapter._record_last_inbound(peer, msg_id)

        name = self.adapter._peer_label(peer)
        source = self.adapter.build_source(
            chat_id=peer,
            chat_name=name,
            chat_type="dm",
            user_id=peer,
            user_name=name,
            message_id=msg_id or None,
        )
        reply_to_text = msg_data.get("reply_to_text") or None

        saved: List[Tuple[Path, dict, str]] = []
        if is_file and attachments and _sender_is_authorized(peer):
            saved = await self.adapter._save_inbound_attachments(
                peer, attachments, msg_id=msg_id, caption=text, at_ms=msg_data.get("at_ms")
            )

        pending_key = _pending_inbox_key(peer)
        if is_file and not text.strip():
            if _sender_is_authorized(peer):
                await self.adapter._ack_file_only(peer, saved)
                await self.adapter._write_inbox_breadcrumb(source, saved)
                self.adapter._queue_pending_inbox(pending_key, saved)
            elif _pairing_enabled():
                event = MessageEvent(
                    text="(file attachment)",
                    message_type=MessageType.DOCUMENT,
                    source=source,
                    message_id=msg_id or None,
                    reply_to_text=reply_to_text,
                )
                await self.adapter.handle_message(event)
            return

        if _is_superseded_replay(msg_data):
            logger.info(
                "Vector: replayed message superseded by a newer one from %s; "
                "filing as context",
                _truncate_npub(peer),
            )
            await self.adapter._file_superseded_replay(source, text, saved)
            return

        media_urls, media_types, msg_type = self.adapter._media_for_event(
            pending_key, saved, is_file=is_file
        )

        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=msg_id or None,
            reply_to_text=reply_to_text,
            media_urls=media_urls,
            media_types=media_types,
        )
        await self.adapter.handle_message(event)

    async def _handle_group_message(self, msg_data: dict) -> None:
        """Mention-gated Concord channel message → Hermes ``chat_type=group``.

        Vector's client sends community files with empty caption (no @mention on
        the file event). Default: stash metadata only, download when someone
        replies to that file and @mentions the bot. Mention-only reply = store
        + session breadcrumb, no turn. Mention + extra text = turn with
        ``media_urls``. ``VECTOR_COMMUNITY_DOWNLOAD_ALL=on`` downloads on
        arrival (still silent; the same reply+mention starts a turn).
        """
        raw_chat = str(msg_data.get("chat_id") or "")
        channel_id = normalize_channel_id(raw_chat)
        if not channel_id:
            return
        _remember_channel(channel_id)
        await self.adapter._notify_joined_channels(
            str(msg_data.get("community_id") or ""),
            [{"channel_id": channel_id, "name": ""}],
        )

        msg_id = str(msg_data.get("id") or "")
        if msg_id and self.adapter._is_duplicate(msg_id):
            logger.debug("Vector: dropping duplicate inbound id=%s", msg_id[:16])
            return

        text = str(msg_data.get("text") or "")
        attachments = (
            msg_data.get("attachments")
            if isinstance(msg_data.get("attachments"), list)
            else []
        )
        is_file = bool(msg_data.get("is_file") or attachments)
        if not text.strip() and not is_file:
            logger.debug("Vector: skip empty group message id=%s", msg_id[:16])
            return

        raw_peer = msg_data.get("npub") or ""
        peer = normalize_npub(raw_peer) or str(raw_peer).strip()
        if not peer:
            return

        bot_npub = normalize_npub(self.adapter._npub or "") if self.adapter._npub else None
        if bot_npub and peer == bot_npub:
            return

        community_id = str(msg_data.get("community_id") or "") or None
        at_ms = msg_data.get("at_ms")

        if is_file and attachments and msg_id:
            self.adapter._stash_group_file_pending(
                channel_id,
                msg_id,
                peer=peer,
                attachments=attachments,
                community_id=community_id,
                at_ms=at_ms,
            )
            if _community_download_all():
                saved = await self.adapter._ingest_group_file_event(
                    channel_id,
                    msg_id,
                    caption=text,
                )
                if saved:
                    await self.adapter._write_inbox_breadcrumb(
                        self.adapter._group_source(
                            channel_id, peer, msg_id, community_id
                        ),
                        saved,
                    )
            if not text.strip():
                return

        reply_to = str(msg_data.get("reply_to") or "")
        is_command = bool(msg_data.get("is_command"))
        if (
            not _mentions_bot(text, bot_npub, self.adapter.bot_name)
            and not _reply_to_bot(reply_to, self.adapter._sent_message_ids)
            and not _group_slash_command(text, is_command=is_command)
        ):
            logger.debug("Vector: drop group message (no mention) id=%s", msg_id[:16])
            return

        if not _group_sender_is_authorized(peer, channel_id):
            logger.debug(
                "Vector: drop group message from unauthorized sender %s",
                _truncate_npub(peer),
            )
            return

        if msg_id:
            self.adapter._record_last_inbound(channel_id, msg_id)

        source = self.adapter._group_source(channel_id, peer, msg_id, community_id)
        reply_to_text = msg_data.get("reply_to_text") or None

        saved: List[Tuple[Path, dict, str]] = []
        if reply_to:
            saved = await self.adapter._ingest_group_file_event(
                channel_id, reply_to, caption=text
            )
        if is_file and attachments and msg_id:
            this_saved = await self.adapter._ingest_group_file_event(
                channel_id, msg_id, caption=text
            )
            saved.extend(this_saved)

        remainder = _mention_remainder(text, bot_npub, self.adapter.bot_name)
        mention_only = (
            saved
            and not remainder
            and not _group_slash_command(text, is_command=is_command)
            and not _reply_to_bot(reply_to, self.adapter._sent_message_ids)
        )
        if mention_only:
            await self.adapter._write_inbox_breadcrumb(source, saved)
            return

        if _is_superseded_replay(msg_data):
            logger.info(
                "Vector: replayed channel message superseded by a newer one; "
                "filing as context",
            )
            await self.adapter._file_superseded_replay(source, text, saved)
            return

        media_urls = [sandbox_turn_path(path, mime=mime) for path, _att, mime in saved]
        media_types = [mime for _path, _att, mime in saved]
        msg_type = MessageType.TEXT
        if media_types:
            msg_type = _message_type_for_mime(media_types[0])
        channel_context = None
        if _group_context_enabled():
            channel_context = await self.adapter._group_channel_context(
                channel_id, trigger_id=msg_id, trigger_at_ms=at_ms
            )
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=msg_id or None,
            reply_to_text=reply_to_text,
            media_urls=media_urls,
            media_types=media_types,
            channel_context=channel_context,
        )
        await self.adapter.handle_message(event)

    async def _handle_message_delete(self, msg_data: dict) -> None:
        """Peer (or we) deleted a bubble. No Hermes turn — forget local pointers."""
        if (
            isinstance(msg_data, dict)
            and msg_data.get("type") == "message_delete"
            and "data" in msg_data
        ):
            msg_data = msg_data["data"]
        if not isinstance(msg_data, dict):
            return
        msg_id = str(msg_data.get("id") or msg_data.get("message_id") or "")
        chat_id = str(msg_data.get("chat_id") or "")
        if msg_id:
            self.adapter._is_duplicate(msg_id)
        self.adapter._forget_last_inbound(chat_id, msg_id)
        if msg_id:
            pointer = _group_file_pointer_path(msg_id)
            try:
                pointer.unlink(missing_ok=True)
            except OSError:
                pass
        channel = normalize_channel_id(chat_id)
        if channel and msg_id:
            pending = _group_file_pending_path(channel, msg_id)
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
        logger.debug(
            "Vector: message deleted id=%s chat=%s",
            (msg_id or "")[:16],
            _truncate_npub(chat_id) if chat_id else "",
        )

    async def _handle_message_update(self, msg_data: dict) -> None:
        """Peer reaction on a message we sent → ``reaction:added:<emoji>``."""
        if (
            isinstance(msg_data, dict)
            and msg_data.get("type") == "message_update"
            and "data" in msg_data
        ):
            msg_data = msg_data["data"]
        if not isinstance(msg_data, dict):
            return

        reactions = msg_data.get("reactions")
        if not isinstance(reactions, list) or not reactions:
            return

        target_id = str(msg_data.get("id") or "")
        raw_peer = msg_data.get("npub") or msg_data.get("chat_id") or ""
        peer = normalize_npub(raw_peer) or str(raw_peer).strip()
        if not peer or not target_id:
            return

        bot_npub = normalize_npub(self.adapter._npub or "") if self.adapter._npub else None
        ours = bool(msg_data.get("mine")) or target_id in self.adapter._sent_message_ids
        last = reactions[-1] if isinstance(reactions[-1], dict) else None
        if not isinstance(last, dict):
            return
        react_id = str(last.get("id") or "")
        author = normalize_npub(str(last.get("author_id") or "")) or str(
            last.get("author_id") or ""
        ).strip()
        emoji = str(last.get("emoji") or "")
        already_seen = bool(react_id) and react_id in self.adapter._seen_reaction_ids
        for row in reactions:
            if isinstance(row, dict) and row.get("id"):
                self.adapter._remember_reaction_id(str(row["id"]))
        if already_seen or not ours or not emoji:
            return
        if _is_superseded_replay(msg_data):
            # A stale reaction is not worth a turn once a newer event for the
            # same chat has already been replayed.
            return
        if bot_npub and author == bot_npub:
            return
        if not _pairing_enabled() and not _sender_is_authorized(peer):
            return
        name = self.adapter._peer_label(peer)
        source = self.adapter.build_source(
            chat_id=peer,
            chat_name=name,
            chat_type="dm",
            user_id=peer,
            user_name=name,
            message_id=react_id or None,
        )
        event = MessageEvent(
            text=f"reaction:added:{emoji}",
            message_type=MessageType.TEXT,
            source=source,
            message_id=react_id or None,
            reply_to_message_id=target_id,
            reply_to_text=str(msg_data.get("text") or "") or None,
            reply_to_is_own_message=True,
            raw_message=msg_data,
        )
        await self.adapter.handle_message(event)

    def _is_duplicate(self, msg_id: str) -> bool:
        """Return True if this inbound id was already seen (LRU ~1024)."""
        if not msg_id:
            return False
        seen = self.adapter._inbound_ids
        if msg_id in seen:
            seen.move_to_end(msg_id)
            return True
        seen[msg_id] = None
        while len(seen) > INBOUND_DEDUP_MAX:
            seen.popitem(last=False)
        return False

    def _queue_pending_inbox(
        self, key: str, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        pending = self.adapter._pending_inbox.setdefault(key, [])
        seen = {p for p, _m in pending}
        for path, _att, mime in saved:
            path_key = sandbox_turn_path(path, mime=mime)
            if path_key not in seen:
                pending.append((path_key, mime))
                seen.add(path_key)

    def _media_for_event(
        self,
        key: str,
        saved: List[Tuple[Path, dict, str]],
        *,
        is_file: bool,
    ) -> Tuple[List[str], List[str], MessageType]:
        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT
        if saved:
            media_urls = [sandbox_turn_path(path, mime=mime) for path, _att, mime in saved]
            media_types = [mime for _path, _att, mime in saved]
        elif not is_file:
            pending = self.adapter._pending_inbox.pop(key, [])
            media_urls = [p for p, _m in pending]
            media_types = [m for _p, m in pending]
        if media_types:
            msg_type = _message_type_for_mime(media_types[0])
        if saved:
            self.adapter._pending_inbox.pop(key, None)
        return media_urls, media_types, msg_type

    def _group_source(self, channel_id: str, peer: str, msg_id: str, community_id: Optional[str]):
        name = self.adapter._peer_label(peer)
        chat_name = self.adapter._group_label_cached(channel_id, community_id)
        return self.adapter.build_source(
            chat_id=channel_id,
            chat_name=chat_name,
            chat_type="group",
            user_id=peer,
            user_name=name,
            message_id=msg_id or None,
            parent_chat_id=community_id,
            scope_id=community_id,
            role_authorized=True,
        )

    def _stash_group_file_pending(
        self,
        channel_id: str,
        msg_id: str,
        *,
        peer: str,
        attachments: List[dict],
        community_id: Optional[str],
        at_ms: Any,
    ) -> None:
        path = _group_file_pending_path(channel_id, msg_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "channel_id": channel_id,
                        "msg_id": msg_id,
                        "peer": peer,
                        "attachments": attachments,
                        "community_id": community_id or "",
                        "at_ms": at_ms,
                    },
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Vector: failed to stash group file metadata id=%s", msg_id[:16])

    def _load_group_file_pointer(
        self, msg_id: str
    ) -> List[Tuple[Path, dict, str]]:
        path = _group_file_pointer_path(msg_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        rows = data.get("files") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        saved: List[Tuple[Path, dict, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            file_path = Path(str(row.get("path") or ""))
            if not file_path.is_file():
                return []
            mime = str(row.get("mime") or "application/octet-stream")
            att = {
                "id": row.get("attachment_id") or "",
                "name": row.get("name") or file_path.name,
            }
            saved.append((file_path, att, mime))
        return saved

    def _write_group_file_pointer(
        self, msg_id: str, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        if not msg_id or not saved:
            return
        path = _group_file_pointer_path(msg_id)
        payload = {
            "msg_id": msg_id,
            "files": [
                {
                    "path": str(file_path),
                    "mime": mime,
                    "name": att.get("name") or file_path.name,
                    "attachment_id": att.get("id"),
                }
                for file_path, att, mime in saved
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("Vector: failed to write by-event pointer for %s", msg_id[:16])

    async def _ingest_group_file_event(
        self,
        channel_id: str,
        event_id: str,
        *,
        caption: str,
    ) -> List[Tuple[Path, dict, str]]:
        """Return already-saved bytes for ``event_id``, or download from pending metadata."""
        existing = self.adapter._load_group_file_pointer(event_id)
        if existing:
            return existing
        pending_path = _group_file_pending_path(channel_id, event_id)
        if not pending_path.is_file():
            return []
        try:
            data = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(data, dict):
            return []
        attachments = data.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return []
        peer = str(data.get("peer") or "")
        saved = await self.adapter._save_inbound_attachments(
            peer,
            attachments,
            msg_id=event_id,
            caption=caption,
            at_ms=data.get("at_ms"),
            channel_id=channel_id,
            community_id=str(data.get("community_id") or "") or None,
        )
        if saved:
            self.adapter._write_group_file_pointer(event_id, saved)
        return saved

    async def _save_inbound_attachments(
        self,
        peer: str,
        attachments: List[dict],
        *,
        msg_id: str,
        caption: str,
        at_ms: Any,
        channel_id: Optional[str] = None,
        community_id: Optional[str] = None,
    ) -> List[Tuple[Path, dict, str]]:
        """Download attachments into files/inbox/{npub|channel/npub}/{YYYY-MM-DD}/."""
        saved: List[Tuple[Path, dict, str]] = []
        max_bytes = _inbound_media_max_bytes()
        try:
            when = datetime.fromtimestamp(int(at_ms) / 1000.0) if at_ms else datetime.now()
        except (TypeError, ValueError, OSError):
            when = datetime.now()
        day = when.strftime("%Y-%m-%d")
        stamp = when.strftime("%H%M%S")
        cid = normalize_channel_id(channel_id or "") if channel_id else None
        if cid:
            inbox = paths.resolve_files_root() / "inbox" / cid / peer / day
        else:
            inbox = paths.resolve_files_root() / "inbox" / peer / day
        author = peer
        for i, att in enumerate(attachments):
            if not isinstance(att, dict):
                continue
            try:
                size = int(att.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if max_bytes and size > max_bytes:
                logger.warning(
                    "Vector: skip inbound attachment over cap (%s bytes)", size
                )
                continue
            orig = str(att.get("name") or "").strip() or f"file-{att.get('id') or i}"
            ext = str(att.get("extension") or "").lstrip(".")
            if ext and not orig.lower().endswith(f".{ext.lower()}"):
                orig = f"{orig}.{ext}"
            filename = _sanitize_filename(f"{stamp}-{orig}")
            dest = _unique_path(inbox, filename)
            path = await self.adapter._download_attachment(att, dest, author_npub=author)
            if path is None:
                continue
            mime = _mime_for_attachment(att)
            sha = ""
            try:
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
            meta = {
                "original_name": orig,
                "saved_as": path.name,
                "size": path.stat().st_size if path.exists() else size,
                "mime": mime,
                "sha256": sha,
                "vector_event_id": msg_id,
                "attachment_id": att.get("id"),
                "peer": peer,
                "caption": caption or "",
                "saved_at": time.time(),
            }
            if cid:
                meta["channel_id"] = cid
            if community_id:
                meta["community_id"] = community_id
            try:
                path.with_name(path.name + ".meta.json").write_text(
                    json.dumps(meta, indent=2) + "\n", encoding="utf-8"
                )
            except OSError:
                logger.warning("Vector: failed to write inbox meta for %s", path.name)
            self.adapter._append_inbox_index(meta | {"path": str(path)})
            saved.append((path, att, mime))
        return saved

    async def _download_attachment(
        self, att: dict, dest: Path, *, author_npub: str
    ) -> Optional[Path]:
        if not self.adapter._http_client:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = await self.adapter._http_client.post(
                f"{self.adapter.bridge_url}/download-attachment",
                json={
                    "attachment": att,
                    "dest": str(dest),
                    "author_npub": author_npub,
                },
                headers=self.adapter._token_headers(),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.warning("Vector: download-attachment failed: %s", e)
            return None
        if resp.status_code != 200:
            logger.warning(
                "Vector: /download-attachment returned %s: %s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return None
        if dest.is_file():
            return dest
        try:
            data = resp.json()
            p = Path(data.get("path") or "")
            if p.is_file():
                return p
        except (ValueError, json.JSONDecodeError, TypeError):
            pass
        return None

    def _append_inbox_index(self, row: dict) -> None:
        index = paths.resolve_files_root() / "index.jsonl"
        try:
            index.parent.mkdir(parents=True, exist_ok=True)
            with index.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            logger.warning("Vector: failed to append files/index.jsonl")

    async def _ack_file_only(self, chat_id: str, saved: List[Tuple[Path, dict, str]]) -> None:
        if not saved:
            await self.adapter.send(chat_id=chat_id, content="couldn't save attachment")
            return
        names = [path.name.split("-", 1)[-1] if "-" in path.name else path.name for path, *_ in saved]
        if len(names) == 1:
            body = f"saved {names[0]}"
        else:
            body = f"saved {len(names)} files: " + ", ".join(names)
        await self.adapter.send(chat_id=chat_id, content=body)

    async def _file_superseded_replay(
        self, source, text: str, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        """Record a superseded replayed message as context, with no agent turn.

        A reconnect can hand back a whole burst the peer sent while the stream
        was down. Someone who fires off five messages is waiting on an answer to
        the last one, not five answers — and five turns is five times the GPU.
        The sidecar flags every replayed message that a newer one in the same
        chat supersedes; those land in the session transcript so the agent still
        sees them, and only the newest actually runs.
        """
        lines = [
            "[Vector] Earlier message, delivered late after a reconnect "
            "(context only — answered as part of the newest message):"
        ]
        if text.strip():
            lines.append(text.strip())
        for path, att, mime in saved:
            orig = att.get("name") or path.name
            visible = sandbox_breadcrumb_path(path, mime=mime)
            lines.append(f"- attachment {orig} ({mime}) `{visible}`")
        try:
            await asyncio.to_thread(
                self.adapter._append_session_breadcrumb, source, "\n".join(lines)
            )
        except Exception:
            logger.warning(
                "Vector: failed to write superseded replay breadcrumb", exc_info=True
            )

    async def _write_inbox_breadcrumb(
        self, source, saved: List[Tuple[Path, dict, str]]
    ) -> None:
        if not saved:
            return
        lines = [
            "[Vector inbox] Saved file(s) with no caption (not processed). "
            "Paths for later reference:"
        ]
        for path, att, mime in saved:
            orig = att.get("name") or path.name
            visible = sandbox_breadcrumb_path(path, mime=mime)
            lines.append(f"- {orig} ({mime}) `{visible}`")
        content = "\n".join(lines)
        try:
            await asyncio.to_thread(self.adapter._append_session_breadcrumb, source, content)
        except Exception:
            logger.warning("Vector: failed to write inbox session breadcrumb", exc_info=True)

    def _append_session_breadcrumb(self, source, content: str) -> None:
        """Write into the gateway SessionStore's real session_id, not the routing key."""
        store = getattr(self.adapter, "_session_store", None)
        if store is None or not hasattr(store, "get_or_create_session"):
            logger.warning("Vector: session store unavailable for inbox breadcrumb")
            return
        entry = store.get_or_create_session(source, touch_activity=False)
        session_id = getattr(entry, "session_id", None)
        if not session_id:
            logger.warning("Vector: session store returned no session_id for breadcrumb")
            return
        runner = getattr(self.adapter, "gateway_runner", None)
        db = getattr(runner, "_session_db", None) if runner is not None else None
        inner = getattr(db, "_db", db) if db is not None else None
        if inner is None or not hasattr(inner, "append_message"):
            logger.warning("Vector: session db unavailable for inbox breadcrumb")
            return
        ensure = getattr(inner, "ensure_session", None)
        if callable(ensure):
            ensure(session_id, source="vector")
        inner.append_message(
            session_id,
            "user",
            content,
            display_kind="internal_notification",
            platform_message_id=getattr(source, "message_id", None),
        )
        logger.info("Vector: inbox breadcrumb written to session %s", session_id)

