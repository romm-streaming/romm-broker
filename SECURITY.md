# Security

This container streams a full Linux desktop to your browser. Setting
`emulator: "desktop"` starts `selkies-desktop`, the same way starting any
other emulator session works, so anyone who can control that session gets a
real terminal (`foot`, by default) and can run anything the container's user
can run. That's not a bug, it's what desktop mode is for: configuring
emulators through a graphical interface. It does mean that what you're
protecting here is different from a normal web app: anyone who can reach a
session has a shell, not just access to a few buttons on a page. Treat it
that way when you deploy it, especially the moment it's reachable from
outside your own local network (LAN).

## What the broker already enforces

Read this before you add anything else, it already covers part of the
problem:

- **The broker refuses to start unless `BROKER_SECRET` is set**, unless you
  explicitly set `BROKER_DEV_MODE=true`
  ([main.py](webstation_broker/main.py)). Once `BROKER_SECRET` is set, every
  session-lifecycle endpoint (`activate`, `join`, save/load state, swap-disc,
  state-file, memory-card, exports, imports, status) requires a matching
  `X-Broker-Secret` header on every request.
- **`BROKER_DEV_MODE=true` turns that check off on purpose**, for local
  development where you have the source code mounted in. The broker logs a
  warning every time it starts this way. Never set it on anything reachable
  from outside your own workstation.
- **Every emulator the broker launches, including the desktop session, gets
  a cleaned-up environment.** Before launch, the broker strips out
  `BROKER_SECRET`, `SELKIES_MASTER_TOKEN`, `GITHUB_TOKEN`, and anything
  shaped like `*_SECRET` / `*_TOKEN` / `*_PASSWORD` / `*_KEY`
  ([base.py](webstation_broker/emulators/base.py)). The helper tools the
  broker runs on untrusted files (`7z`, `unrar`, `pkg_extractor`,
  `qemu-img`, the PCSX2 patch fetch) get the same cleanup. That means a terminal
  opened inside a desktop session can't read the broker's own secret back
  out of its environment. This exists because RetroArch loads third-party
  cores with no protective sandbox around them (nothing walling off what a
  misbehaving core can do), and the desktop session gets the same cleanup as
  a side effect, not as its main purpose. Don't rely on it alone.

None of this makes the terminal safe to expose. It just means the secret
that guards reaching it isn't also sitting there once you're inside.

## The real boundary: `BROKER_SECRET`

`BROKER_SECRET` is the one thing standing between the public internet and a
working shell in this container, once `activate` is reachable at all. Treat
it like a root password (the master password for the whole machine), not
like an ordinary API key:

- Generate it randomly (`openssl rand -hex 32` or similar), not a phrase you
  can remember.
- Never commit it, log it, or put it in a Dockerfile `ENV` line, since that
  bakes it into the image itself, where anyone with the image can read it.
  Pass it in at runtime instead.
- Rotate it (change it to a new value) if it may have been exposed: shared
  in chat, pasted into a bug report, visible in a process listing on a
  shared host.
- One secret unlocks every session type this broker knows, `desktop`
  included. This repo has no separate concept of "who may request desktop
  mode." If you need that distinction, it has to come from whatever calls
  `activate` (RomM's own permission system), not from the broker.

## Network isolation

Assume any session, not just desktop, is one exploited emulator away from a
shell (RetroArch cores and every other emulator here are native code with no
sandbox, parsing ROM files you can't fully trust). Set up this container as
if that shell already exists:

- Put it on its own network segment or VLAN (an isolated network) with no
  path to anything it doesn't need: no NAS shares beyond the ROM library
  mount, no other containers, no management interfaces.
- Mount only `ROM_ROOT` and `/config`. Nothing broader. Mount `ROM_ROOT`
  read-only if the broker's write path (state files under the save
  subtrees) doesn't need the whole tree writable.
- Run rootless, drop capabilities (Linux permissions) you're not using,
  never use `--privileged`, and never mount your container runtime's own
  socket into it.
- Give it a resource ceiling (a CPU and memory limit) so a session that goes
  wrong is a contained problem, not a host-wide one.

## Reverse proxy

The [reverse proxy](https://romm-streaming.github.io/romm-broker/docs/deployment/reverse-proxy)
guide covers the mount mechanics (handling the URL prefix, upgrading
connections for websockets, and recipes for specific proxies). Two more
things matter only once this is public:

**Handle real TLS encryption at the proxy, never at the container.** (TLS is
what puts the padlock in a browser's address bar.) Port 3001's certificate
is self-signed, meaning a browser will never trust it on its own; it exists
only for the short hop between the proxy and the container. The proxy is
the only thing that should ever hold a certificate a browser is meant to
trust.

**The proxy is what actually faces the internet, so the protections the
broker doesn't implement belong there.** The broker deliberately has no rate
limiting (no cap on how many requests someone can make per minute) and no IP
allowlisting (restricting access to specific addresses); this repo's scope
stops at the session API. A public deployment needs both in front of it,
especially on `activate` and `join`. A secret is not a rate limit: even if
your `BROKER_SECRET` leaks or someone brute-forces it (guesses it through
repeated automated attempts), they shouldn't also get unlimited tries.

**Session and controller tokens ride in the URL** (`?token=...`), by design,
for the room links this broker hands out. That's fine on a LAN. On the
public internet it means the token sits in your browser history, in
`Referer` headers sent to any other site the room page loads content from,
and in your proxy's access logs. A leaked controller token during an active
desktop session is a shell handoff, not just a spectator link. Turn off
query-string logging for the proxied path, or scrub it, on any
public-facing proxy.

## Gating desktop mode

The broker can't tell "an admin configuring emulators" apart from "anyone
who can call activate"; `desktop` is just another `emulator` value on the
same endpoint. If you want that distinction, enforce it upstream:

- In RomM, restrict whichever role or action reaches `activate` with
  `emulator: "desktop"` to accounts you'd trust with a shell on this host.
  Don't treat "logged into RomM" as the same thing as "trusted with a
  terminal."
- If RomM's permission system can't make that split today, don't expose
  desktop mode publicly at all. Run it LAN-only (bind the proxy rule to an
  internal network, or leave desktop out of a public-facing config) until it
  can.

## Before you expose this publicly

- [ ] `BROKER_SECRET` is set to a strong random value; `BROKER_DEV_MODE` is
      unset (confirm with the broker's own startup log, it announces both)
- [ ] The container's own port is not reachable from outside its network
      segment; only the reverse proxy and RomM's `broker_host` can reach it
- [ ] The reverse proxy handles real TLS (encryption) and is the only
      public listener
- [ ] The reverse proxy rate-limits or restricts by IP address on `activate`
      and `join`
- [ ] The reverse proxy does not log the `token` query parameter for the
      proxied path
- [ ] `ROM_ROOT` and `/config` are the only mounts, and nothing else on the
      host is reachable from this container's network segment
- [ ] The container runs rootless, without `--privileged`, without the
      container runtime's socket mounted in
- [ ] Whoever can reach `emulator: "desktop"` is someone you would hand a
      terminal on this host to directly

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private reporting for this repository (Security tab -> Report a
vulnerability) so it can be assessed before details are public.
