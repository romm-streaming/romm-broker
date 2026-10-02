# RetroArch keychain fixture generator

`gen.c` produced the sealed values and key files in
`tests/test_retroarch_credentials.py`. It calls RetroArch's own keychain
code, so the tests check the broker's reader against what RetroArch actually
writes. Rerun it when the keychain format changes upstream. It is not built in
CI, and the tests only use the constants it printed.

## On every RetroArch bump

`webstation_broker/emulators/retroarch_credentials.py` mirrors the keychain
at `388637b6`, which no RetroArch release ships yet. Whenever the image's
RetroArch changes, diff `file/keychain.c`, `crypto/crypto.c` and
`crypto/kdf.c` in libretro-common against `388637b6`:

```sh
git -C ra-src diff 388637b6 <new ref> -- \
  libretro-common/file/keychain.c libretro-common/crypto/
```

If they changed, port the change to the reader, rebuild `gen` at the new ref,
regenerate the constants below, and update the ref in this file and in the
module docstring. A format the reader does not follow reads as an unknown
login, so the exit reports no change: a login made in the emulator never
reaches RomM, and the only sign is a log line.

## Build

Against RetroArch master at `388637b6` (libretro-common, MIT):

```sh
git clone --filter=blob:none --sparse https://github.com/libretro/RetroArch.git ra-src
git -C ra-src checkout 388637b6
git -C ra-src sparse-checkout set libretro-common
L=ra-src/libretro-common
gcc -O1 -w -DHAVE_KEYCHAIN -I$L/include gen.c \
  $L/file/keychain.c $L/crypto/crypto.c $L/crypto/kdf.c \
  $L/encodings/encoding_base64.c $L/encodings/encoding_utf.c \
  $L/streams/file_stream.c $L/file/file_path.c $L/file/file_path_io.c \
  $L/string/stdstring.c $L/string/rstrtod.c \
  $L/compat/compat_strl.c $L/compat/compat_strldup.c $L/compat/compat_strcasestr.c \
  $L/compat/fopen_utf8.c $L/vfs/vfs_implementation.c $L/time/rtime.c \
  $L/hash/lrc_hash.c $L/features/features_cpu.c $L/utils/md5.c \
  -o gen
```

## Run

The keychain keys off `/etc/machine-id`, so each run bind-mounts a fake one
with `bwrap`. RetroArch falls back to `/var/lib/dbus/machine-id` when that
one is empty, so the run also hides that directory, or the `machine-none`
run would pick up the host's real id:

```sh
mkdir -p fx
echo 0123456789abcdef0123456789abcdef > fx/machine-a   # MACHINE_A
echo fedcba9876543210fedcba9876543210 > fx/machine-b   # MACHINE_B
: > fx/machine-none                                   # empty machine id
DBUS_MASK=$([ -d /var/lib/dbus ] && echo --tmpfs /var/lib/dbus)
run() { bwrap --ro-bind / / --bind "$PWD/fx" "$PWD/fx" \
  --ro-bind "$PWD/fx/$1" /etc/machine-id $DBUS_MASK \
  --dev /dev --proc /proc ./gen "${@:2}"; }
```

`gen <key file> <mode> seal` creates the key file if it is missing, then
prints `cheevos_username` (`alice`), `cheevos_token` (`tok123`) and
`cheevos_password` (empty), each sealed. `<mode>` is `-` for none,
`set:<passphrase>` to wrap the data key, or `unlock:<passphrase>` to open a
locked one. `gen <key file> <mode> open <name> <sealed value>` opens one value.

| Constants | Command |
|-----------|---------|
| `PLAIN_KEY`, `PLAIN_VALUES` | `run machine-a fx/plain.key - seal` |
| `WRAPPED_KEY`, `WRAPPED_VALUES` | `run machine-a fx/wrapped.key set:hunter2 seal` |
| `MOVED_KEY` | `cp fx/wrapped.key fx/moved.key`, then `run machine-b fx/moved.key unlock:hunter2 open cheevos_token '<a WRAPPED_VALUES token>'`; unlocking rewraps the key for machine B |
| `NONE_KEY`, `NONE_VALUES` | `run machine-none fx/none.key - seal` |

Each key file's contents are the `*_KEY` constant; the printed lines are the
`*_VALUES`.
