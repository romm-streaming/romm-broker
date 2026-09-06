"""Session broker for the RomM webstation streaming container.

The package serves one FastAPI application (built by `webstation_broker.app`)
that hosts a single play session at a time: `webstation_broker.api` exposes the
lifecycle routes RomM drives (activate, exit, save states, memory cards),
`webstation_broker.session` holds the session and room state,
`webstation_broker.room` fans presence, chat and media out over a websocket,
`webstation_broker.selkies` keeps the streaming input tokens in sync, and
`webstation_broker.saves` with `webstation_broker.memcard` move save data in
and out. Emulator back ends live under `webstation_broker.emulators`.
"""

__version__ = "0.7.0"
"""The package version string, bumped by the release tooling."""
