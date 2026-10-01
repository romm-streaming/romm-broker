"""Read the RetroAchievements login RetroArch saved on exit, from either of its layouts.

RetroArch 1.22.x writes `cheevos_username` and `cheevos_token` into
`retroarch.cfg` in plaintext. Later releases move every credential into
`retroarch-keychain.cfg` beside it, sealed as `$kc1$<base64>`: a 12-byte
nonce, the ciphertext and a 16-byte tag, ChaCha20-Poly1305 under a data key,
with the setting's name as associated data.

The data key comes from `retroarch-keychain.key` (also beside the config) and
the machine identity, so it can be rebuilt here, in the same container:

* The key file's first line is a 32-byte salt in hex.
* With no `machine` line, the data key is HKDF-SHA256 of the machine id under
  that salt, info `retroarch-keychain-v1`.
* With a `machine` line, the data key is wrapped: HKDF-SHA256 the same way
  with info `retroarch-keychain-wrap-v1` gives the key that opens it, with
  `retroarch-keychain machine` as associated data. A keychain wrapped for
  another machine only opens with its passphrase, which the broker never
  has, so that case reads as unknown.

This mirrors `libretro-common/file/keychain.c` on RetroArch master. The
format is unreleased, so anything that does not open is logged and read as
unknown rather than guessed at.
"""

import base64
import binascii
import logging
import re
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .base import RetroAchievementsLogin

log = logging.getLogger(__name__)

KEYCHAIN_CFG = "retroarch-keychain.cfg"
"""The file, beside `retroarch.cfg`, where later RetroArch releases keep credentials."""

KEYCHAIN_KEY = "retroarch-keychain.key"
"""The file, beside `retroarch.cfg`, holding the keychain's salt and any wrapped data key."""

SEALED_PREFIX = "$kc1$"
"""What a sealed keychain value starts with."""

MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
"""Where RetroArch looks for the machine identity on Linux, in order."""

NO_MACHINE_ID = b"no-machine-id"
"""The identity RetroArch falls back to when neither machine-id file has one."""

MACHINE_ID_MAX = 127
"""How many bytes of a machine-id file RetroArch reads (its buffer is 128 with the NUL)."""

DATA_KEY_INFO = b"retroarch-keychain-v1"
"""HKDF info for a data key derived straight from the machine identity."""

WRAP_KEY_INFO = b"retroarch-keychain-wrap-v1"
"""HKDF info for the key that opens a wrapped data key."""

MACHINE_WRAP_AD = b"retroarch-keychain machine"
"""Associated data the key file's `machine` line is sealed under."""

NONCE_SIZE = 12
"""ChaCha20-Poly1305 nonce length, in bytes."""

KEY_SIZE = 32
"""Salt and key length, in bytes."""

TAG_SIZE = 16
"""Poly1305 tag length, in bytes."""

_CREDENTIAL_LINE = re.compile(
    r'^\s*(cheevos_username|cheevos_token)\s*=\s*(?:"([^"]*)"|(\S*))', re.MULTILINE
)


class KeychainError(Exception):
    """The keychain could not be opened: a missing or malformed key file, or one wrapped elsewhere."""


def _read_credentials(path: Path) -> dict[str, str]:
    """The `cheevos_username` and `cheevos_token` values a config file holds, as written.

    Args:
        path: A RetroArch config file.

    Returns:
        The keys present, mapped to their raw value (sealed or not). The last
        line for a key wins, as in RetroArch's own parser. Empty when the file
        is missing.

    Raises:
        OSError: When the file exists but cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for match in _CREDENTIAL_LINE.finditer(text):
        quoted, bare = match.group(2), match.group(3)
        values[match.group(1)] = quoted if quoted is not None else bare
    return values


def _machine_id() -> bytes:
    """This machine's identity, read the way RetroArch reads it.

    Returns:
        The first machine-id file's contents with surrounding whitespace
        trimmed, or `NO_MACHINE_ID` when neither file has one.
    """
    for path in MACHINE_ID_PATHS:
        try:
            raw = path.read_bytes()[:MACHINE_ID_MAX]
        except OSError:
            continue
        ident = raw.split(b"\0", 1)[0].strip()
        if ident:
            return ident
    return NO_MACHINE_ID


def _hkdf(salt: bytes, ikm: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 to one 32-byte key.

    Args:
        salt: The keychain salt.
        ikm: The machine identity.
        info: Which key this is.

    Returns:
        The derived key.
    """
    return HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=salt, info=info).derive(ikm)


def _key_file_line(text: str, tag: str) -> Optional[str]:
    """The value of the first key file line that starts with `tag` and a space.

    Args:
        text: The key file's contents.
        tag: The line's tag, `machine` or `passphrase`.

    Returns:
        The rest of the line, trimmed, or None when no line carries the tag.
    """
    for line in text.split("\n"):
        if line.startswith(tag + " "):
            return line[len(tag) + 1 :].strip()
    return None


def _data_key(key_file: Path) -> bytes:
    """Rebuild the keychain's data key from its key file and this machine's identity.

    Args:
        key_file: `retroarch-keychain.key`.

    Returns:
        The 32-byte key the values are sealed under.

    Raises:
        KeychainError: When the key file is missing, malformed, or wrapped for
            another machine.
    """
    try:
        text = key_file.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        raise KeychainError(f"cannot read {key_file}: {exc}") from exc
    try:
        salt = bytes.fromhex(text[: KEY_SIZE * 2])
    except ValueError as exc:
        raise KeychainError(f"{key_file} does not start with a hex salt") from exc
    if len(salt) != KEY_SIZE:
        raise KeychainError(f"{key_file} does not start with a hex salt")
    ident = _machine_id()
    machine = _key_file_line(text, "machine")
    if machine is None:
        return _hkdf(salt, ident, DATA_KEY_INFO)
    try:
        wrap = base64.b64decode(machine, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeychainError(f"{key_file} has a malformed machine line") from exc
    if len(wrap) != NONCE_SIZE + KEY_SIZE + TAG_SIZE:
        raise KeychainError(f"{key_file} has a malformed machine line")
    kek = _hkdf(salt, ident, WRAP_KEY_INFO)
    try:
        return ChaCha20Poly1305(kek).decrypt(wrap[:NONCE_SIZE], wrap[NONCE_SIZE:], MACHINE_WRAP_AD)
    except InvalidTag as exc:
        raise KeychainError(
            f"{key_file} is wrapped for another machine and needs its passphrase"
        ) from exc


def _open(key: bytes, name: str, sealed: str) -> str:
    """Open one sealed value.

    Args:
        key: The keychain's data key.
        name: The setting the value belongs to, which is its associated data.
        sealed: The stored value, `$kc1$` and all.

    Returns:
        The plaintext.

    Raises:
        KeychainError: When the value is malformed or does not open under `key`.
    """
    try:
        blob = base64.b64decode(sealed[len(SEALED_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeychainError(f"{name} is not valid base64") from exc
    if len(blob) < NONCE_SIZE + TAG_SIZE:
        raise KeychainError(f"{name} is too short to be sealed")
    try:
        plain = ChaCha20Poly1305(key).decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], name.encode())
    except InvalidTag as exc:
        raise KeychainError(f"{name} does not open with this keychain") from exc
    return plain.decode("utf-8", errors="replace")


def read_saved_login(config_path: Path) -> Optional[RetroAchievementsLogin]:
    """The RetroAchievements login RetroArch saved, from whichever layout it used.

    The keychain file wins when it holds either credential; otherwise the
    plaintext config is read. Only a file RetroArch wrote this session should
    hold either, since the broker scrubs both before every launch.

    Args:
        config_path: RetroArch's `retroarch.cfg`.

    Returns:
        The saved login, whose token is empty when RetroArch saved the player
        logged out. None when nothing was saved, or when it could not be read
        or opened (logged), so the caller can tell "unknown" from "logged out".
    """
    keychain_cfg = config_path.parent / KEYCHAIN_CFG
    try:
        values = _read_credentials(keychain_cfg)
        source = keychain_cfg
        if not values:
            values = _read_credentials(config_path)
            source = config_path
    except OSError as exc:
        log.warning("ra login capture: could not read RetroArch's config: %s", exc)
        return None
    if not values:
        log.debug("ra login capture: RetroArch saved no login")
        return None
    key: Optional[bytes] = None
    opened: dict[str, str] = {}
    try:
        for name, value in values.items():
            if not value.startswith(SEALED_PREFIX):
                opened[name] = value
                continue
            if key is None:
                key = _data_key(config_path.parent / KEYCHAIN_KEY)
            opened[name] = _open(key, name, value)
    except KeychainError as exc:
        log.warning("ra login capture: could not open %s: %s", source, exc)
        return None
    return RetroAchievementsLogin(
        username=opened.get("cheevos_username", ""), token=opened.get("cheevos_token", "")
    )
