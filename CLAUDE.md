# webstation-broker - Repository Guide for Contributors & Agents

Session broker and collaboration interface for the RomM webstation container. It launches emulators over REST, restores/archives save data, and runs the collab room (chat, webcam fanout, input routing) that the Selkies stream is embedded into.

---

## The stack at a glance

| Path                | Language              | Notes                                                    |
| -------------------- | --------------------- | --------------------------------------------------------- |
| `webstation_broker/` | Python 3.11+ (FastAPI) | pip-installable, console script `webstation-broker`       |
| `frontend/`          | Vanilla JS (Vite)      | room UI, served under the `SUBFOLDER` prefix              |
| `tests/`             | pytest                 | one test module per emulator/subsystem                    |
| `docs/`              | Markdown               | standalone-emulator and integration notes                 |

See [README.md](README.md) for the full request flow and save-archive layout.

---

## Conventions - read before touching code

[CONTRIBUTING.md](CONTRIBUTING.md) holds the house rules for this repo: test coverage, comment and docstring style, type hints, logging, secrets and configuration, the security invariants, and PR/commit conventions. Read it before writing, editing, or reviewing any code, comments, tests, log statements, or PR/commit descriptions here. These rules are strict, not suggestions: follow them exactly, every time, even when not reminded.

Most of them are enforced: `ruff`'s `D` and `ANN` rules make the docstring and type-hint rules a CI failure rather than a review note.

---

## Repo-wide rules

**Branch off `master`; open PRs against `master`.** Don't push to `master` directly.
**Lint:** `.venv/bin/ruff check webstation_broker tests` (CI runs exactly this, pinned to `ruff==0.16.1`, on every push/PR to `master`; see `.github/workflows/ci.yml`). The `D` and `ANN` rules feed the generated developer reference, so a malformed docstring is a docs regression as well as a lint failure.
**Tests travel with code.** New logic gets a test in `tests/`; new endpoints get endpoint tests.
**Don't commit until approved.** Never run `git commit` (or push) without the user explicitly signing off first.
**Link PRs to issues.** `Fixes #XXXX` for bug fixes, `Closes #XXXX` for feature implementations.

Full detail on comments, docstrings, logging, secrets, and the security invariants lives in [CONTRIBUTING.md](CONTRIBUTING.md) - read it, don't duplicate it here.

---

## Quick command reference

The toolchain lives in `.venv/`; `ruff` and `pytest` are not on `PATH`. Call
them by path, or activate the venv first.

```bash
uv venv && uv pip install -e . pytest pytest-asyncio "ruff==0.16.1"   # first time only

.venv/bin/ruff check webstation_broker tests          # lint
.venv/bin/pytest -q                                   # run tests (3442, ~45s)
.venv/bin/pytest tests/test_flycast.py                # run a subset
.venv/bin/webstation-broker                           # run the app (console script)

# CI's third gate: catches an import or syntax error that would otherwise
# only surface when s6 restarts the service in the container.
BROKER_DEV_MODE=true .venv/bin/python -c "from webstation_broker.app import create_app; create_app()"

cd frontend && npm ci && npm run build                # room UI, not covered by CI
```

The three CI gates are lint, the `create_app()` import check, and `pytest -q`.
The `frontend/` build is not one of them, so run it yourself when you touch
`frontend/src`.
