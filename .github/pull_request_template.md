## What

<!-- One sentence: what changes for a user. -->

## Why

<!-- The failure mode this fixes, or the gap it closes. -->

## Checks

- [ ] `python -m unittest discover -s tests -v` passes
- [ ] No keys, tokens or personal paths in the diff
- [ ] `--json` output shape unchanged, or the change is documented
- [ ] `docs/environment.md` updated if a new env var was added
- [ ] Degradation path covered: missing key, timeout, 429, malformed response
