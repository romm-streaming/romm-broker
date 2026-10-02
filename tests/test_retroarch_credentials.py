"""Tests for reading back the RetroAchievements login RetroArch saved on exit.

The sealed values and key files below were produced by RetroArch's own
`libretro-common/file/keychain.c` (master at 388637b6), run under a bind-mounted
`/etc/machine-id`, so they pin the broker's reader to what RetroArch writes
rather than to a reimplementation of it. Every one seals username `alice` and
token `tok123`. `tests/fixtures/ra-keychain/` holds the generator and how to
rerun it.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import retroarch_credentials
from webstation_broker.emulators.base import RetroAchievementsLogin

MACHINE_A = "0123456789abcdef0123456789abcdef\n"
"""The machine id the plain and wrapped key files were made on."""

MACHINE_B = "fedcba9876543210fedcba9876543210\n"
"""A second machine: the wrapped key file is locked here, the moved one opens."""

PLAIN_KEY = "92c1c0805cd0a9588b9a052b0402d923e294d9c3a7f9616e8229ff5f316105d3\n"
"""A key file with no machine line: the data key comes straight from machine A."""

PLAIN_VALUES = {
    "cheevos_username": "$kc1$HYgpZhUuLmeU44dxk/uDpiGuq7aDJ6dh8opyVu69bLgE",
    "cheevos_token": "$kc1$xZgVyiH6zURoKomL+dqKp06SthXID+mmo1KNto5uM2CIBw==",
    "cheevos_password": "$kc1$LSikoo3uik/pTJzqtNoDMhutqYl3IzomUDSiIg==",
}
"""Values sealed under `PLAIN_KEY` on machine A."""

WRAPPED_SALT = "ed94dbe745a65f88bb027855d919d773b91228ea8ed0d8b58f86fb4d59636560"
"""The salt line both the wrapped and the moved key file share."""

PASSPHRASE_LINE = (
    "passphrase 200000 gRcSAvmNvUg70teev0ZV3KDy3VirrjLUS+ASNYwiaBJI6TsZlNwIxfZ77JlR0WUJ"
    "5cR+JIfHnV1ZNi14c/6NRxa/An2lUsdeCMgNGA=="
)
"""The passphrase line (passphrase `hunter2`), unchanged when the keychain moves."""

WRAPPED_KEY = (
    f"{WRAPPED_SALT}\n"
    "machine ZOudTFE16tr2gwiVKV4iuVAFgSceAEv7+zPVTavhT73P0VKgzx87/ZK9XKL7w6qcmKrA7cbFHp3mHFHt\n"
    f"{PASSPHRASE_LINE}\n"
)
"""A key file with a passphrase set on machine A, so its data key is wrapped for A."""

MOVED_KEY = (
    f"{WRAPPED_SALT}\n"
    "machine l5TyBOHmxxumec07XIgpNeRKruVbUXymKVdadQLIvykS+YNOZGQeO31OZEo4dRABc+YDb6oiaPfBAP0C\n"
    f"{PASSPHRASE_LINE}\n"
)
"""`WRAPPED_KEY` after RetroArch unlocked it with the passphrase on machine B."""

WRAPPED_VALUES = {
    "cheevos_username": "$kc1$/wo3LMy+Y0ZVB7VURggptuXe6wOlC93ZSlvPkNQ2pYsu",
    "cheevos_token": "$kc1$IstGXE9Pyz1q5LF/P7LJvPHq0uA8E4AxrdVt51SN9khBHA==",
    "cheevos_password": "$kc1$v6cofgPD+SWIYkWiMh4/nWjfScFw7Z4x0VWFzA==",
}
"""Values sealed under the wrapped data key, which `MOVED_KEY` still holds."""

NONE_KEY = "bd7e1bcc9c95cadf79f113763460d0016abc3f0827d488644746be681ccf2504\n"
"""A key file made with no machine id at all, so under `no-machine-id`."""

NONE_VALUES = {
    "cheevos_username": "$kc1$m24tqayFLranr8ZM9ZeocbVjKHzpXdFLWQ9qaXaM6E6Y",
    "cheevos_token": "$kc1$juaLd8pgfmu3sbPw7DZl+qOJT3j/iMqLci29TNF8+7rjYg==",
    "cheevos_password": "$kc1$+VWsrGZ86Xes/3MamXNQr1ir8NSSivuVJlrzUg==",
}
"""Values sealed under `NONE_KEY`."""

ALICE = RetroAchievementsLogin(username="alice", token="tok123")


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """An empty RetroArch config directory, returning where `retroarch.cfg` would be."""
    root = tmp_path / "retroarch"
    root.mkdir()
    return root / "retroarch.cfg"


def _machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ident: str) -> None:
    """Point the reader's machine-id lookup at a file holding `ident`.

    Args:
        monkeypatch: The test's monkeypatch.
        tmp_path: Where to write the file.
        ident: The machine-id file's contents; empty means none.
    """
    path = tmp_path / "machine-id"
    path.write_text(ident)
    monkeypatch.setattr(
        retroarch_credentials, "MACHINE_ID_PATHS", (path, tmp_path / "no-dbus-machine-id")
    )


def _cfg(path: Path, values: dict[str, str], header: str = "") -> None:
    """Write a RetroArch config file the way RetroArch does.

    Args:
        path: The file to write.
        values: The settings, quoted on write.
        header: Lines to put first.
    """
    body = "".join(f'{key} = "{value}"\n' for key, value in values.items())
    path.write_text(header + body)


def _keychain(config_path: Path, key: str, values: dict[str, str]) -> None:
    """Write a keychain beside `config_path`: its key file and its sealed values.

    Args:
        config_path: The `retroarch.cfg` the keychain sits beside.
        key: The key file's contents.
        values: The sealed settings.
    """
    (config_path.parent / retroarch_credentials.KEYCHAIN_KEY).write_text(key)
    _cfg(
        config_path.parent / retroarch_credentials.KEYCHAIN_CFG,
        values,
        header='keychain_version = "1"\n',
    )


class TestPlaintextLayout:
    """RetroArch 1.22.x: the login sits in `retroarch.cfg` in the clear."""

    def test_the_saved_login_is_read(self, config_path: Path) -> None:
        """The username and token come back as written."""
        _cfg(
            config_path,
            {
                "video_fullscreen": "true",
                "cheevos_username": "alice",
                "cheevos_token": "tok123",
                "cheevos_password": "",
            },
        )
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_a_logged_out_player_reads_as_an_empty_token(self, config_path: Path) -> None:
        """A saved, empty token is "logged out", not "unknown"."""
        _cfg(config_path, {"cheevos_username": "alice", "cheevos_token": ""})
        assert retroarch_credentials.read_saved_login(config_path) == RetroAchievementsLogin(
            username="alice", token=""
        )

    def test_nothing_saved_reads_as_unknown(self, config_path: Path) -> None:
        """A config without the keys, or no config at all, is None."""
        assert retroarch_credentials.read_saved_login(config_path) is None
        _cfg(config_path, {"video_fullscreen": "true"})
        assert retroarch_credentials.read_saved_login(config_path) is None

    def test_the_last_line_for_a_key_wins(self, config_path: Path) -> None:
        """RetroArch's parser keeps the last value, so the reader does too."""
        config_path.write_text(
            'cheevos_username = "old"\ncheevos_token = "x"\n'
            'cheevos_username = "alice"\ncheevos_token = "tok123"\n'
        )
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_an_unquoted_value_is_read(self, config_path: Path) -> None:
        """A hand-edited config without quotes still reads."""
        config_path.write_text("cheevos_username = alice\ncheevos_token = tok123\n")
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_a_bare_empty_value_does_not_reach_the_next_line(self, config_path: Path) -> None:
        """`cheevos_token =` with nothing after it is an empty token, not the next line's key."""
        config_path.write_text("cheevos_token =\ncheevos_username = alice\n")
        assert retroarch_credentials.read_saved_login(config_path) == RetroAchievementsLogin(
            username="alice", token=""
        )

    def test_a_commented_key_is_not_read(self, config_path: Path) -> None:
        """Only a real setting line counts."""
        config_path.write_text('# cheevos_username = "alice"\n')
        assert retroarch_credentials.read_saved_login(config_path) is None


class TestKeychainLayout:
    """Later RetroArch: the login is sealed in `retroarch-keychain.cfg`."""

    def test_a_plain_keychain_opens(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """A data key derived from the machine id opens the values."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_a_wrapped_keychain_opens_on_its_own_machine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """A passphrase-set keychain still opens without the passphrase where it was made."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(config_path, WRAPPED_KEY, WRAPPED_VALUES)
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_a_keychain_unlocked_on_a_new_machine_opens_there(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """After the passphrase moves it, the new machine opens the same values."""
        _machine(monkeypatch, tmp_path, MACHINE_B)
        _keychain(config_path, MOVED_KEY, WRAPPED_VALUES)
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_a_keychain_wrapped_elsewhere_reads_as_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        config_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A keychain that needs its passphrase here is None, with a warning."""
        _machine(monkeypatch, tmp_path, MACHINE_B)
        _keychain(config_path, WRAPPED_KEY, WRAPPED_VALUES)
        with caplog.at_level(logging.WARNING):
            assert retroarch_credentials.read_saved_login(config_path) is None
        assert "passphrase" in caplog.text

    def test_a_keychain_made_without_a_machine_id_opens(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """No machine id falls back to RetroArch's `no-machine-id`."""
        _machine(monkeypatch, tmp_path, "")
        _keychain(config_path, NONE_KEY, NONE_VALUES)
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_the_machine_id_is_read_with_its_whitespace_trimmed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """Padding around the id does not change the key."""
        _machine(monkeypatch, tmp_path, f"  {MACHINE_A.strip()}\n\n")
        _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_the_wrong_machine_reads_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """A plain keychain from another machine does not open, and is not guessed at."""
        _machine(monkeypatch, tmp_path, MACHINE_B)
        _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
        assert retroarch_credentials.read_saved_login(config_path) is None

    def test_a_value_sealed_under_another_name_does_not_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """The setting name is bound in, so a swapped value fails rather than reads wrong."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(
            config_path,
            PLAIN_KEY,
            {
                "cheevos_username": PLAIN_VALUES["cheevos_token"],
                "cheevos_token": PLAIN_VALUES["cheevos_username"],
            },
        )
        assert retroarch_credentials.read_saved_login(config_path) is None

    def test_the_keychain_wins_over_the_plain_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """A stale plaintext login beside a keychain is ignored."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
        _cfg(config_path, {"cheevos_username": "mallory", "cheevos_token": "stale"})
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_a_value_left_in_the_clear_is_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """RetroArch writes the clear text when its keychain is unavailable."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _cfg(
            config_path.parent / retroarch_credentials.KEYCHAIN_CFG,
            {"cheevos_username": "alice", "cheevos_token": "tok123"},
        )
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param(None, id="missing"),
            pytest.param("not hex at all\n", id="no-salt"),
            pytest.param(f"{WRAPPED_SALT}\nmachine !!!\n", id="bad-machine-line"),
            pytest.param(f"{WRAPPED_SALT}\nmachine AAAA\n", id="short-machine-line"),
        ],
    )
    def test_a_broken_key_file_reads_as_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        config_path: Path,
        key: Optional[str],
    ) -> None:
        """No key file, or one that does not parse, is None rather than an exception."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(config_path, key or "", PLAIN_VALUES)
        if key is None:
            (config_path.parent / retroarch_credentials.KEYCHAIN_KEY).unlink()
        assert retroarch_credentials.read_saved_login(config_path) is None

    @pytest.mark.parametrize(
        "sealed",
        [
            pytest.param("$kc1$not base64!", id="not-base64"),
            pytest.param("$kc1$AAAA", id="too-short"),
            pytest.param("$kc1$" + "A" * 44, id="does-not-open"),
        ],
    )
    def test_a_malformed_value_reads_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path, sealed: str
    ) -> None:
        """A sealed value that cannot be opened is None, never a half-read login."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(
            config_path,
            PLAIN_KEY,
            {"cheevos_username": PLAIN_VALUES["cheevos_username"], "cheevos_token": sealed},
        )
        assert retroarch_credentials.read_saved_login(config_path) is None


def test_the_token_is_never_logged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither a successful read nor a failed one puts the token in the log."""
    _machine(monkeypatch, tmp_path, MACHINE_A)
    _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
    with caplog.at_level(logging.DEBUG):
        retroarch_credentials.read_saved_login(config_path)
        _machine(monkeypatch, tmp_path, MACHINE_B)
        retroarch_credentials.read_saved_login(config_path)
    assert "tok123" not in caplog.text
    assert PLAIN_VALUES["cheevos_token"] not in caplog.text


def test_an_unreadable_config_reads_as_unknown(
    config_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A config path that cannot be read is None, with a warning."""
    config_path.mkdir()
    with caplog.at_level(logging.WARNING):
        assert retroarch_credentials.read_saved_login(config_path) is None
    assert "could not read" in caplog.text


LAUNCH = 1_800_000_000.0
"""A stand-in launch wall time for the freshness tests."""


def _age(path: Path, mtime: float) -> None:
    """Set a file's modification time.

    Args:
        path: The file.
        mtime: Its new modification time, as a wall time.
    """
    os.utime(path, (mtime, mtime))


class TestOnlyThisSessionsLogin:
    """With `not_before`, a login in a file RetroArch did not write this session is not trusted."""

    def test_a_config_older_than_the_launch_reads_as_unknown(
        self, config_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A login left over from before the launch is None, logged by path only."""
        _cfg(config_path, {"cheevos_username": "bob", "cheevos_token": "oldtok"})
        _age(config_path, LAUNCH - 60)
        with caplog.at_level(logging.DEBUG):
            assert retroarch_credentials.read_saved_login(config_path, not_before=LAUNCH) is None
        assert str(config_path) in caplog.text
        assert "bob" not in caplog.text
        assert "oldtok" not in caplog.text

    @pytest.mark.parametrize("offset", [0.0, 5.0], ids=["at-launch", "after-launch"])
    def test_a_config_written_since_the_launch_is_read(
        self, config_path: Path, offset: float
    ) -> None:
        """A file modified at or after the launch is trusted."""
        _cfg(config_path, {"cheevos_username": "alice", "cheevos_token": "tok123"})
        _age(config_path, LAUNCH + offset)
        assert retroarch_credentials.read_saved_login(config_path, not_before=LAUNCH) == ALICE

    def test_without_not_before_any_age_is_read(self, config_path: Path) -> None:
        """The check is opt-in, so an old file still reads when no launch time is given."""
        _cfg(config_path, {"cheevos_username": "alice", "cheevos_token": "tok123"})
        _age(config_path, LAUNCH - 60)
        assert retroarch_credentials.read_saved_login(config_path) == ALICE

    def test_an_old_keychain_falls_through_to_a_fresh_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """A keychain left from before the launch gives way to this session's plain config."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
        _age(config_path.parent / retroarch_credentials.KEYCHAIN_CFG, LAUNCH - 60)
        _cfg(config_path, {"cheevos_username": "carol", "cheevos_token": "fresh"})
        _age(config_path, LAUNCH + 1)
        assert retroarch_credentials.read_saved_login(
            config_path, not_before=LAUNCH
        ) == RetroAchievementsLogin(username="carol", token="fresh")

    def test_an_old_keychain_beside_an_old_config_reads_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """When neither file was written this session, the login is unknown."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
        _age(config_path.parent / retroarch_credentials.KEYCHAIN_CFG, LAUNCH - 60)
        _cfg(config_path, {"cheevos_username": "bob", "cheevos_token": "oldtok"})
        _age(config_path, LAUNCH - 60)
        assert retroarch_credentials.read_saved_login(config_path, not_before=LAUNCH) is None

    def test_a_fresh_keychain_still_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path
    ) -> None:
        """A keychain written this session beats a plain config, as without the check."""
        _machine(monkeypatch, tmp_path, MACHINE_A)
        _keychain(config_path, PLAIN_KEY, PLAIN_VALUES)
        _age(config_path.parent / retroarch_credentials.KEYCHAIN_CFG, LAUNCH + 1)
        _cfg(config_path, {"cheevos_username": "carol", "cheevos_token": "fresh"})
        _age(config_path, LAUNCH + 1)
        assert retroarch_credentials.read_saved_login(config_path, not_before=LAUNCH) == ALICE
