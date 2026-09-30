# Contributing

Thanks for looking at romm-broker. A few ground rules before you open a PR.

## Workflow

- Branch off `master`; open PRs against `master`. Don't push to `master` directly.
- If an issue already exists for what your PR resolves, link it in the PR
  description: `Fixes #XXXX` for a bug fix, `Closes #XXXX` for a feature. A PR
  with no existing issue doesn't need one opened first.
- Keep commit messages short, concise, and accurate: what changed and why, no
  filler.
- Every commit must be signed, so GitHub marks it Verified. `commit.gpgsign` is
  the default for `git commit` and `git merge`, but plumbing such as
  `git commit-tree` ignores it: pass `-S`. Check with `git log --format='%h %G?'`
  (an `N` means unsigned).

## Before you open a PR

```bash
uv venv && uv pip install -e . pytest pytest-asyncio "ruff==0.16.1"
.venv/bin/ruff check webstation_broker tests
BROKER_DEV_MODE=true .venv/bin/python -c "from webstation_broker.app import create_app; create_app()"
.venv/bin/pytest -q
```

CI runs four gates on every push and PR to `master`: lint, a `create_app()`
import check under `BROKER_DEV_MODE=true` (catching an import or syntax error
before it would otherwise only surface when s6 restarts the service in the
container), the test suite, and `npm test` in `frontend/`. That last one only
covers the helpers split out of `room.js` with a `*.test.js` beside them.
There is no frontend lint or build step in CI, so if you touch `frontend/`,
read your diff carefully and build it before opening the PR.

## Code conventions

### Docstrings and type hints

`ruff`'s `D` and `ANN` rules enforce both, so these are CI failures rather than
review notes. The developer reference is generated from the docstrings, which
makes a malformed one a docs regression too.

- **Google-style docstrings** on every module, class, and any function that
  isn't trivially self-describing: one summary line, then `Args:` / `Returns:` /
  `Yields:` / `Raises:` when the signature needs them.
- **Full type hints**, staying `Optional`/`Union` from `typing` rather than
  `X | Y`. The whole codebase reads this way; don't start a second dialect in
  one file.
- **Module-level constants get an attribute docstring** naming the env var they
  read and the default that applies:

  ```python
  BROKER_SECRET = os.environ.get("BROKER_SECRET", "")
  """Shared secret for the session lifecycle endpoints, from `BROKER_SECRET`.

  Leaving it unset refuses to start unless `BROKER_DEV_MODE` is set, which
  starts the broker unauthenticated instead.
  """
  ```

- **A pydantic field whose meaning is non-obvious gets one too**, carrying the
  why. `RomIn.language` is the model: it explains that ScummVM boots one target
  per language, so without the field a multilingual game starts in whichever
  sorts first.

### Comments

- **Why, not what.** Skip anything the code already says.
- **No inline changelog.** Never record what a line used to do. That belongs in
  the commit message and the PR body.
- **Delete a comment the moment it stops matching the code.** Stale is worse
  than absent.
- **Do leave a why-comment on a fix that closes a race or a non-obvious trap.**
  The diff alone won't convey it and nobody re-derives it.
- **No em-dashes** in code, comments, docstrings, log strings, or
  documentation. Use a comma, parentheses, a colon, or split into two
  sentences.

### Logging

- **Module-level logger**: `log = logging.getLogger(__name__)`. No second
  `basicConfig`; `app.py` owns the one format.
- **Lazy `%s` interpolation, never f-strings**, so arguments aren't formatted
  when the level is off and messages stay groupable:
  `log.warning("save upload to %s failed: %s", url, exc)`.
- **Prefix by verb, not by module.** The format already carries `%(name)s`, so
  repeating the module is noise, but `"disc swap: ..."` or `"load state: ..."`
  inside a large emulator module tells you which operation failed.
- **Log every error path**, with enough context to act on it (platform, rom,
  session id, URL, status code). Successful operations log at `info` or `debug`
  and say what changed.

### Configuration and secrets

- **Never commit secrets.** No API keys, passwords, tokens, or broker secrets,
  hardcoded, in a fixture, or in a doc example.
- **All configuration is env vars read through `webstation_broker/settings.py`**,
  once, at import time. Don't scatter `os.environ.get(...)` through an endpoint
  or emulator module; add the setting with its attribute docstring and import it.
- **Compare secrets with `hmac.compare_digest`**, never `==`, which is a timing
  oracle. Encode both sides first (`.encode("utf-8", "replace")`) so a non-ASCII
  header rejects instead of raising.

### Tests

- **Tests travel with code.** New logic gets a test in `tests/`. New endpoints
  get endpoint tests. One module per emulator or subsystem.
- **Tests are linted too.** CI runs `ruff check webstation_broker tests`, so the
  docstring and annotation rules apply to fixtures as well.
- **Reset module-global state with an autouse fixture** on both sides of the
  test. Session state is module-global, so a leak between tests is a false pass.
- **Redirect on-disk locations into `tmp_path`.** Emulator modules read their
  paths at import time into module globals, so the redirect is a monkeypatch of
  those globals, not of the environment. No test may touch a real container's
  config tree.
- **Factor repeated fixture construction into typed helpers** once it appears
  three or more times in a file.

## Security invariants

Each of these is a bug this project shipped once. A regression on any of them
blocks a PR.

- **Validate every caller-supplied rom or disc path against `ROM_ROOT`** with
  `os.path.realpath` (symlinks resolved) before it reaches a subprocess. Outside
  the root is a clean 400, not a launch.
- **Put a RomM-facing route on `api.secret_router`**, never on `api.router`.
  The secret is checked there before anything else, so the route can't forget
  to. `api.router` is for routes with a credential of their own (a seat
  token); adding one means adding it to `_OPEN_ROUTES` in `tests/test_api.py`.
- **Put `--` before a path in an argv list**, so a leading-dash filename can't
  become a flag.
- **Never `shell=True`.** Always list-form argv.
- **Never hold a lock across a network write.** Snapshot state under the lock,
  release, then write the response.
- **Reset state in the `except` when a launch fails.** Setting `running = True`
  before a `Popen` that raises leaves the broker permanently busy.
- **Write config files atomically**: `NamedTemporaryFile` in the target
  directory, then `os.replace`. Keep the trailing newline, and insert a missing
  key inside its `[Section]`, not at end of file.

## Adding an emulator

There's a dedicated walkthrough for this:
[Adding an emulator](https://romm-streaming.github.io/romm-broker/docs/developer/adding-an-emulator).

## Documentation

Docs live under `docs/content/docs/` (Fumadocs) and deploy to GitHub Pages on
merge to `master`. If your change affects behavior a user or contributor would
read about, update the relevant page in the same PR.
