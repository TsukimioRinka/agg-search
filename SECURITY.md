# Security Policy

## Reporting a vulnerability

Use GitHub's private advisory flow:
[Report a vulnerability](https://github.com/TsukimioRinka/agg-search/security/advisories/new).

Please do not open a public issue for anything that could expose credentials,
allow SSRF, or turn a fetched page into code execution.

Include the affected version, a minimal reproduction (query, engine, config),
and what an attacker gains. You will get an acknowledgement within a few days.

## Scope

- The fetch path accepts user-supplied URLs. Reports about SSRF, redirect
  handling and scheme handling are in scope.
- API keys are read from the environment and never logged or persisted. Any
  path that writes a key to disk or stdout is in scope.
- The MCP server shells out to the CLI with the caller's arguments. Anything
  that allows argument injection beyond the documented flags is in scope.

## Out of scope

- Rate limits or billing behaviour of third-party engines.
- Results quality of a specific engine.
- The external `crawl-agent` container image, which is not part of this
  repository.
