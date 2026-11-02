# Contributing to ProAgent OS

Thanks for helping. Short version:

1. Open an issue first for large features (use the templates).
2. Fork → branch → PR against `main`.
3. Run `pytest -q` before opening the PR.
4. Never commit secrets (`.env`, keys, live databases under `data/`).

## First-time fork pull requests

GitHub may hold a first-time fork PR's Actions run at `action_required` until a maintainer approves it. After approval, CI runs `pytest -q` on Python 3.11 and 3.12. To retry a failed job, open the workflow run from the Actions tab and choose **Re-run jobs → Re-run failed jobs**.

Vertical packs that plug in via `verticals/` + `PROAGENT_SKILLS_DIRS` without core changes are especially welcome.

## Packaging

PyPI upload is owner-gated. Do not run `twine upload`, create tokens, or tag a release from a contribution. Checklist: [docs/PYPI.md](docs/PYPI.md).
