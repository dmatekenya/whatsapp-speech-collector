# Editing and publishing this book

## Organization and ownership

Each component owns its methodology, manual, targets where relevant, and a separate QA file. The overall methodology uses MyST include directives to assemble the authoritative Markdown sources. Edit those sources; do not paste independent copies into the combined page.

Missing source documents are marked as stubs. When incorporating a Google Docs export, record its source URL, owner, version and conversion date. Review Markdown tables, links and lists after conversion. The contributor Word original is retained in `speech-recordings/sources/`; the Markdown is the book's working copy.

The decision log records changes in methods. Historical platform plans remain reference material, not current specifications. Old document paths retain short forwarding pages for repository readers.

## Local checks

Create a dedicated environment so building documentation needs neither application dependencies nor database access:

```bash
uv venv /tmp/chichewa-book
uv pip install --python /tmp/chichewa-book/bin/python -r docs/requirements.txt
/tmp/chichewa-book/bin/jupyter-book build docs --all --warningiserror --keep-going
```

Open `docs/_build/html/index.html` to preview. Add new pages to `docs/_toc.yml`; only listed pages are published. GitHub Pages serves a static site: there is no login or runtime connection to the collection database.

## Automatic checks and publication

The GitHub Actions documentation workflow builds on pull requests, pushes to `main`, and manual dispatch. A warning fails the build. Pull requests validate the book without deploying it. Successful builds on `main` upload the HTML and deploy through GitHub Pages, using the official Pages artifact and deployment actions.

One-time repository setup: under **Settings → Pages → Build and deployment**, select **GitHub Actions**. Ensure Actions can run and the `github-pages` environment permits deployments from `main`. Repository administrators can require the documentation build check in branch protection.

The published address is https://dmatekenya.github.io/whatsapp-speech-collector/ . Publication starts only after the changes are reviewed and pushed to `main`; local edits do not publish anything.

## Editing checks

The local pre-commit documentation hook runs a strict book build when docs change, using the isolated dependencies in `docs/requirements.txt`. Install pre-commit and run `pre-commit install` once; use `pre-commit run documentation-book --all-files` to check manually. CI runs the same strict build independently.

Use absolute book references such as `` {doc}`/speech-recordings/quality-assurance` `` when linking from content included in the overall methodology. Those links resolve correctly both standalone and when included.
