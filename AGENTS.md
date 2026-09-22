# AGENTS.md

Runnable Dialt recipes: application policy and orchestration built only on the public
[`dialt-sdk`](https://pypi.org/project/dialt-sdk/) and [`@dialt/sdk`](https://www.npmjs.com/package/@dialt/sdk)
surfaces.

[README.md](README.md) documents every recipe and the commands that run it — read it first, and
read a recipe's own README before changing that recipe. This file covers what those don't: how to
work in the repo.

## The boundary that defines this repo

Dialt owns conversation; recipes own application policy and orchestration. Keep it that way:

- Build only on published SDK surfaces. If a recipe needs something the SDK doesn't expose, the
  fix belongs in the SDK, not in a local workaround here.
- Never add a parallel simulator client or an eval-only conversation engine. A simulated user is
  another Dialt session.
- Text and voice select different I/O on the same session primitive. They are not separate code
  paths to fork.

## Layout

- `src/dialt_recipes/` — the reusable library. Its public surface is `__all__` in `__init__.py`;
  the `dialt-guided`, `dialt-sim` and `dialt-evals` console scripts all enter through `cli.py`.
- `examples/<recipe>/` — one directory per recipe. Its `README.md` is the spec for that recipe's
  behaviour.
- `examples/<recipe>/evals/` — that recipe's eval cases, rendered from its workflow.
- `examples/simulations/` — standalone eval cases; `appointment_booking.json` is a complete one.
- `examples/integrations/twilio/` — a standalone uv project with its own `pyproject.toml`,
  lockfile and tests. Sync and test it separately.
- `tests/` — offline and secret-free, one module per recipe.

## Working in it

```sh
uv sync --frozen                                                   # uv only, never pip
uv run pytest                                                      # default suite
uv run --project examples/integrations/twilio pytest examples/integrations/twilio -q
```

- `.github/workflows/ci.yml` runs exactly those on Python 3.12 (the package itself needs 3.11+).
  Match it before pushing.
- No linter or formatter is configured. Match the style of the file you are editing.
- Changing a recipe's behaviour means updating its eval cases and its README in the same change.
- Commits are conventional and scoped to the recipe — `feat(policy):`, `fix(tools):`,
  `chore(guided_customer_research):`. Work lands through PRs.

## Secrets

- `DIALT_API_KEY` lives in `.env` (gitignored); copy `.env.example`. Never inline a key or commit
  one.
- Browser examples take a short-lived scoped session key, never a persistent `ck_` or `dk_`
  account key.
- Keep the default suite runnable with no credentials and no network. Anything live belongs in the
  manual `live-smoke.yml` workflow.

## Evals

Run cases locally before pushing them hosted; see the README for both commands and
[the evals guide](https://dialt.com/docs/api/evals/) for the case format. Locally, `judge` checks
are reported as skipped and an undeclared tool fails closed — so a local pass means the case
declares every tool the agent uses. Cases are matched by name, so re-pushing updates a hosted case
rather than duplicating it.

## Local Dialt stack

`.agents/setup` expects the `dialt` application checked out as an additional repository, then
starts the supervised fake stack in `.amp/services.yaml` on port 24100 and points `.env` at it.
Outside that environment ignore both and use a real `DIALT_API_KEY`.
