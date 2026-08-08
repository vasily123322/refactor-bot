from __future__ import annotations

import base64
import binascii
import struct

from telethon.crypto import AuthKey
from telethon.sessions import StringSession


# Telegram's production DC endpoints used by MTProto clients. The legacy
# Pyrogram session string stores only the DC id and auth key, while Telethon's
# StringSession also stores the concrete server endpoint.
_PRODUCTION_DC_IPV4 = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}

_PYROGRAM_CURRENT_FORMAT = ">BI?256sQ?"
_PYROGRAM_OLD_FORMAT = ">B?256sI?"
_PYROGRAM_OLD_FORMAT_64 = ">B?256sQ?"
_PYROGRAM_OLD_SIZE = 351
_PYROGRAM_OLD_SIZE_64 = 356


def _decode_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def decode_pyrogram_session(value: str) -> tuple[int, int | None, bool, bytes, int, bool]:
    """Decode a Pyrogram v2 session string without importing Pyrogram."""
    raw = _decode_urlsafe(value)
    if len(value) in {_PYROGRAM_OLD_SIZE, _PYROGRAM_OLD_SIZE_64}:
        fmt = (
            _PYROGRAM_OLD_FORMAT
            if len(value) == _PYROGRAM_OLD_SIZE
            else _PYROGRAM_OLD_FORMAT_64
        )
        dc_id, test_mode, auth_key, user_id, is_bot = struct.unpack(fmt, raw)
        return dc_id, None, test_mode, auth_key, int(user_id), is_bot

    expected = struct.calcsize(_PYROGRAM_CURRENT_FORMAT)
    if len(raw) != expected:
        raise ValueError("not a supported Pyrogram session string")
    dc_id, api_id, test_mode, auth_key, user_id, is_bot = struct.unpack(
        _PYROGRAM_CURRENT_FORMAT, raw
    )
    return dc_id, int(api_id), test_mode, auth_key, int(user_id), is_bot


def pyrogram_to_telethon_session(value: str, *, configured_api_id: int) -> str:
    """Convert a production Pyrogram session string into Telethon StringSession."""
    dc_id, embedded_api_id, test_mode, auth_key, _user_id, _is_bot = (
        decode_pyrogram_session(value)
    )
    if test_mode:
        raise ValueError("legacy Pyrogram test-mode sessions are not supported")
    if embedded_api_id is not None and embedded_api_id != int(configured_api_id):
        raise ValueError("legacy Pyrogram session API_ID does not match configured API_ID")
    try:
        address = _PRODUCTION_DC_IPV4[int(dc_id)]
    except KeyError as exc:
        raise ValueError(f"unsupported Telegram DC id: {dc_id}") from exc

    session = StringSession()
    session.set_dc(int(dc_id), address, 443)
    session.auth_key = AuthKey(auth_key)
    return session.save()


def normalize_userbot_session(value: str | None, *, configured_api_id: int) -> tuple[str | None, bool]:
    """Return a Telethon-compatible session and whether legacy conversion occurred.

    Telethon StringSession values are accepted as-is. Values that decode using
    Pyrogram's documented v2 format are converted in memory, so existing
    deployments can upgrade without re-authorizing the user account.
    """
    if not value:
        return None, False

    try:
        converted = pyrogram_to_telethon_session(
            value, configured_api_id=configured_api_id
        )
    except (ValueError, struct.error, binascii.Error):
        # If it isn't a Pyrogram session, let Telethon validate its own format.
        return value, False
    return converted, True
