# romm-broker

Session broker and collaboration interface for the RomM webstation container.
The container hosts one play session at a time: a game is activated over REST,
the broker launches the emulator (restoring save data if provided) and returns
a token URL that lands on a collab room with the selkies stream iframed inside
it. Exiting saves state, stops the emulator, and archives the session's save
delta.

**Documentation: https://romm-streaming.github.io/romm-broker/**

## Quickstart

Starting point: you already have RomM running and want to add emulators to it.

Run the container, [linuxserver/docker-webstation](https://github.com/linuxserver/docker-webstation)
on the `romm` branch:

```yaml
services:
  webstation:
    image: lscr.io/linuxserver/webstation:romm
    container_name: webstation
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      - SUBFOLDER=/streaming/
      - BROKER_SECRET=change-me
    devices:
      - /dev/dri:/dev/dri
    volumes:
      - /path/to/config:/config
      - /path/to/roms:/romm
    ports:
      - 3000:3000
      - 3001:3001
    shm_size: "1gb"
    restart: unless-stopped
```

```bash
docker compose up -d
```

`/dev/dri` is GPU passthrough for Intel/AMD. NVIDIA needs the container
runtime *and* the `/dev/nvidia-modeset` node; without that node emulators
render but never present, so the stream stays black. BIOS and firmware can
also be pre-seeded with an optional volume mount instead of dragging files
into the desktop; see [Running the container](https://romm-streaming.github.io/romm-broker/docs/container).

Then point RomM at it: add a `webstation` container under
`streaming.containers` in RomM's `config.yml`, matching the `SUBFOLDER` and
`BROKER_SECRET` above, and open each emulator once from the desktop to
install its BIOS/firmware and confirm controllers work.

For the full walkthrough, including the `config.yml` example, NVIDIA GPU
setup, reverse proxying the container behind RomM's own origin, and
per-emulator setup:

| | |
| --- | --- |
| [Running the container](https://romm-streaming.github.io/romm-broker/docs/container) | the compose/CLI examples above in full, GPU details, first run |
| [Reverse proxy](https://romm-streaming.github.io/romm-broker/docs/deployment/reverse-proxy) | serving the container from RomM's origin |
| [Emulator setup](https://romm-streaming.github.io/romm-broker/docs/emulators/setup) | the one-time BIOS/firmware/controller setup each emulator needs |

## Documentation

| | |
| --- | --- |
| [Overview](https://romm-streaming.github.io/romm-broker/docs) | what the broker is and how a session flows |
| [Configuration](https://romm-streaming.github.io/romm-broker/docs/configuration) | every environment variable, for the broker and for each emulator |
| [Emulators](https://romm-streaming.github.io/romm-broker/docs/emulators) | what each launcher supports and its one-time desktop setup |
| [REST API](https://romm-streaming.github.io/romm-broker/docs/api) | activate, join, save states, exit, save archives, status |
| [Using the room](https://romm-streaming.github.io/romm-broker/docs/using-the-room) | the collab room from a player's side: chat, webcam, controller handoff |
| [Developer guide](https://romm-streaming.github.io/romm-broker/docs/developer) | layout, conventions, adding an emulator, the generated Python reference |

## Layout

```
webstation_broker/       FastAPI app (pip installable, console script webstation-broker)
  api.py                 activate / join / exit / status / state and archive REST endpoints
  room.py                collab websocket (chat, webcam fanout, resolution, input passing)
  session.py             single-session state, room broadcast, gamepad/MK assignment
  selkies.py             token pushes to the selkies control plane
  saves.py               save archive restore on activate, delta dump on exit
  memcard.py             whole memory card capture and hydrate
  callback.py            exit-time save archive upload to the parent
  settings.py            environment-driven configuration
  main.py                console-script entrypoint; refuses to start without BROKER_SECRET or dev mode
  app.py                 application factory, SUBFOLDER mount, orphan reaping on start
  emulators/             one launcher per emulator, all subclassing emulators.base.Emulator
frontend/                vite vanilla-JS room interface
tests/                   pytest suite
docs/                    the documentation site (Fumadocs, deployed to GitHub Pages)
```

## Development

```bash
uv venv && uv pip install -e . pytest "ruff==0.16.1"
.venv/bin/pytest -q
.venv/bin/ruff check webstation_broker tests
```

Every module, class and function carries a Google-style docstring and full
type hints; ruff enforces both and the developer reference on the docs site is
generated from them. To run the broker from source inside the container, set
`BROKER_DEV_MODE=true` and mount the checkout at `/broker`; see
[Dev mode](https://romm-streaming.github.io/romm-broker/docs/container/dev-mode).

Building the docs locally:

```bash
cd docs && npm ci && cd ..
uv pip install ./docs/node_modules/fumadocs-python
PYTHONPATH=. .venv/bin/fumapy-generate webstation_broker --dir docs
cd docs && npm run generate && npm run dev
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow.

A session can stream a full desktop with a real terminal on it. Before
exposing this beyond your own LAN, read [SECURITY.md](SECURITY.md).

## Releases

Versions are semver, cut by release-please off `master` from conventional
commits (`feat:` bumps the minor, `fix:` the patch). Each release is
consumable as a source tarball at
`https://github.com/romm-streaming/romm-broker/archive/refs/tags/vX.Y.Z.tar.gz`.
See [Releases](https://romm-streaming.github.io/romm-broker/docs/releases) and
[CHANGELOG.md](CHANGELOG.md).

## License

[GNU AGPLv3](LICENSE)
