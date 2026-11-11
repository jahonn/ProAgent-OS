# PyPI publish checklist

**Do not publish without owner approval.** Nothing in this repository uploads to [PyPI](https://pypi.org/) or [TestPyPI](https://test.pypi.org/). Do not run `twine upload`, do not create API tokens, and do not push a release tag as part of packaging prep. Roadmap [issue #2](https://github.com/jahonn/ProAgent-OS/issues/2) item **P3** (“PyPI publish (`proagent-os`) once API stable enough”) stays open until the owner says to upload.

This page is the checklist for that later yes.

## What is already prepared

- `pyproject.toml` names the project `proagent-os`, keeps version `0.1.1`, and uses `README.md` as the PyPI long description (`text/markdown`).
- License metadata is the SPDX expression `MIT` (`License-Expression`). That is the form PyPI validates (PEP 639). The older Trove classifier `License :: OSI Approved :: MIT License` is deprecated and is **not** set, because current setuptools rejects it beside `License-Expression`. The text is still [LICENSE](../LICENSE).
- Project URLs: Homepage, Repository, Issues, and Changelog, all on `https://github.com/jahonn/ProAgent-OS`.
- The importable distribution is the `proagent_os` package only (see [Package layout](#package-layout)).
- [`.github/workflows/pypi-check.yml`](../.github/workflows/pypi-check.yml) is manual (`workflow_dispatch` only). It builds, runs `twine check`, and uploads the `dist/` files as a GitHub Actions artifact. It has no publish job and no `id-token: write` permission.

Version is intentionally still `0.1.1`. This prep does not bump it.

## Version the owner must choose

The GitHub Release tag `v0.1.1` and current `main` are not the same tree. `pyproject.toml` still says `0.1.1`, and PyPI will not let you replace a file once that version is published.

Before any upload, re-check:

```bash
git fetch origin tag v0.1.1
git log --oneline v0.1.1..origin/main
```

Then pick one:

1. **Publish the `v0.1.1` tag.** Build from that commit so PyPI `0.1.1` matches the GitHub Release. Later commits stay unreleased until a new version.
2. **Bump the version** (and the changelog) in a follow-up, then publish that new version from the matching commit.

Do not upload current `main` as version `0.1.1`. That would label post-release commits with the existing GitHub Release version, and the number could not be corrected in place.

## Build and check

From a clean checkout, with Python 3.11+:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade build twine
python -m build
python -m twine check --strict dist/*
python scripts/check_package_layout.py
```

`python -m build` writes `dist/` (gitignored): one sdist and one wheel. Isolated builds install `setuptools>=77` from the `build-system` requirement. `twine check` validates the long description. It does not contact PyPI. `--strict` turns warnings into errors. `scripts/check_package_layout.py` expects version `0.1.1` in the wheel metadata; update that check in the same change as any version bump.

setuptools also writes a two-line `setup.cfg` **inside the sdist only** (`[egg_info]` with empty `tag_build` and `tag_date = 0`) so older installers do not append a date suffix. That file does not set the name or version. The repository no longer has a `setup.cfg`.

The same commands run in the manual workflow **PyPI package check** (Actions → PyPI package check → Run workflow). That workflow does not run on push or on pull requests. Artifacts expire after 7 days. Downloading them is not a publish.

## Package layout

| Path | Wheel (what `pip install` installs) | Sdist (source archive) |
|---|---|---|
| `proagent_os/` including `web/templates` and `web/static` | yes | yes |
| `LICENSE`, README long description | yes | yes |
| `config/`, `skills/`, `fixtures/`, `verticals/` | no | yes |
| `tests/`, `docs/` | no | yes |

Runtime defaults are still relative to the process working directory (`./config/agents.yaml`, `./skills`, `./verticals`, `./fixtures`). `proagent_os.services` merges `Path("verticals")` from the cwd. A checkout (or an unpacked sdist used as the cwd) is how demos find those files. A bare `pip install` of the wheel does **not** copy them into site-packages and does **not** drop them into the cwd.

That is acceptable for a library-style first upload only if the owner agrees. Making the wheel self-contained would change defaults and is out of scope for this prep.

## TestPyPI and PyPI

| | TestPyPI | PyPI |
|---|---|---|
| Site | https://test.pypi.org/ | https://pypi.org/ |
| Upload host | `https://test.pypi.org/legacy/` | `https://upload.pypi.org/legacy/` |
| Purpose | Rehearsal | The real index |
| Account | Separate from PyPI | The account that will own `proagent-os` |

On 2026-11-02, `https://pypi.org/pypi/proagent-os/json` and `https://test.pypi.org/pypi/proagent-os/json` both returned **404**. That is not a reservation. Check both again immediately before an upload; anyone can register the name first.

A TestPyPI install still needs real PyPI for dependencies:

```bash
python -m pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  proagent-os
```

Use that command only **after** a TestPyPI upload exists. Until then it cannot succeed.

PyPI versions are immutable. Yank is possible later; replace-in-place is not. Prefer one TestPyPI rehearsal, then one production upload of the version you meant.

## Trusted publishing and API tokens

Do not put tokens, `.pypirc` passwords, or `PYPI_API_TOKEN` values in git, issues, or workflow files.

### Preferred: trusted publishing (OIDC)

When the owner is ready, register a **pending publisher** before the project exists (PyPI → account → Publishing):

- PyPI project name: `proagent-os`
- Owner: `jahonn`
- Repository: `ProAgent-OS`
- Workflow filename: a **new** workflow file, not `pypi-check.yml`
- Environment: a named GitHub environment (for example `pypi`) if you want a required reviewer

The publish job would need `permissions: id-token: write` and the official `pypa/gh-action-pypi-publish` action, on `environment: pypi`. No long-lived token is stored in GitHub. Adding the pending publisher does nothing until that workflow runs. **Do not add the workflow or the publisher until the owner approves an upload.**

`pypi-check.yml` must stay a check-only workflow. Do not give it `id-token: write` and do not point a PyPI publisher at it.

### Alternative: API token

Use a token only if trusted publishing is not available.

- Create it in the PyPI or TestPyPI web UI. A brand-new project cannot have a project-scoped token until the project exists, so the first upload is either a pending publisher (above) or a user-scoped token. After the project exists, replace a user-scoped token with one limited to `proagent-os`.
- Store it as a GitHub Actions secret or in a local untracked `.pypirc`. Never commit it.
- Twine’s username for a token is the literal `__token__`. The password is the token value, which is shown once and starts with `pypi-`.
- Environment form, still not for this repo’s workflows:

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=...   # local shell only; do not commit
```

TestPyPI and PyPI tokens are different secrets. A TestPyPI token will not upload to PyPI.

## Owner approval checklist

The owner still has to:

1. Say, explicitly, that an upload is allowed (“可以发 PyPI” or equivalent).
2. Choose the version: the `v0.1.1` tag, or a new version for a later commit. Do not publish `main` as `0.1.1` while `main` has moved past the tag.
3. Re-check that the name `proagent-os` is still free on TestPyPI and PyPI.
4. Accept the wheel layout (cwd-relative `config/`, `skills/`, `verticals/`, `fixtures/`) or schedule a follow-up that vendors them before the first upload.
5. Optionally rehearse on TestPyPI.
6. Choose trusted publishing (preferred) or a project-scoped API token kept outside git.
7. Add a publish workflow in a **separate** change. Leave `pypi-check.yml` as build + `twine check` only.
8. After the production upload, add a `pip install proagent-os` line to the README and only then check off P3 on issue #2.

Until step 1, the allowed commands are `python -m build`, `twine check`, and `python scripts/check_package_layout.py`.
