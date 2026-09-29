# Dialt recipes agent instructions

[README.md](README.md) owns what each recipe does and how to run it. A recipe's own `README.md` is
the spec for its behavior: read it before changing that recipe.

## Boundary

Dialt owns conversation; recipes own application policy and orchestration.

- Build only on the published `dialt-sdk` and `@dialt/sdk` surfaces. If a recipe needs something
  the SDK does not expose, the fix belongs in the SDK, not in a workaround here.
- Never add a parallel simulator client or an eval-only conversation engine. A simulated user is
  another Dialt session, and text and voice are two kinds of I/O on the same session.

## Commands

CI (`.github/workflows/ci.yml`) runs these on Python 3.12. No linter or formatter is configured.

```sh
uv sync --frozen
uv run pytest
uv sync --project examples/integrations/twilio --frozen
uv run --project examples/integrations/twilio pytest examples/integrations/twilio -q
```

## Working conventions

- A change to a recipe's behavior updates its eval cases and its README in the same change, and
  the root README summary if that changes too.
- The default test suite needs no credentials and no network. Live checks belong in the manual
  `live-smoke.yml` workflow.
- Browser code never holds a persistent `ck_` or `dk_` account key; it takes a short-lived scoped
  session key.
- Commit messages are conventional and scoped to the recipe, for example `feat(policy):` or
  `chore(guided_customer_research):`.
- In an Amp orb, `.agents/setup` points `.env` at a local fake Dialt stack (`.amp/services.yaml`).
  Elsewhere, set a real `DIALT_API_KEY` from `.env.example`.
