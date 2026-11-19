# Releasing

## One-time PyPI setup

1. Create the project on PyPI: **Publishing → Trusted publishers → Add**.
   - Owner: `TsukimioRinka`
   - Repository: `agg-search`
   - Workflow: `publish.yml`
   - Environment: `pypi`
2. In the GitHub repository, create an environment named `pypi`
   (Settings → Environments). No secrets are needed; OIDC handles auth.
3. Until that is done, `publish.yml` fails at the last step and the artifacts
   stay in the workflow run for a manual `twine upload`.

## Every release

```bash
# 1. bump version in pyproject.toml, add a CHANGELOG entry
# 2. verify locally
python -m unittest discover -s tests -v
python -m build
python -m pip install dist/agg_search-*.whl --force-reinstall
agg-search --help

# 3. tag and push
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z

# 4. publish the GitHub Release; publish.yml builds and uploads to PyPI
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file CHANGELOG.md
```

## MCP directories

- **Smithery**: `smithery.yaml` is already in the repo; submit the repository
  URL at smithery.ai.
- **MCP Registry**: publish `server.json` after the first PyPI release so the
  package URL resolves.

## Post-release checks

- `pip install agg-search` in a clean venv, run `agg-search --help` and
  `agg-search-mcp` with an `initialize` + `tools/list` handshake.
- Confirm the README badges point at a green run and the new tag.
