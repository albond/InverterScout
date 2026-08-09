# Contributing to InverterScout

InverterScout accepts focused changes that preserve its local-first security model and passive inverter access.

## Development setup

Python 3.12 and Docker with the Compose plugin are the supported development tools.

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the complete local quality gate before proposing a change:

```sh
make lint
make test
docker compose build
```

## Engineering rules

- Keep production code under `src/inverterscout/` and respect the documented dependency boundaries.
- Keep inverter communication passive. Modbus write functions are not accepted.
- Add or update tests for every behavior change.
- Store user data only through the encrypted storage layer.
- Never add credentials, serial numbers, chat IDs, private network addresses, databases, keys, or diagnostic archives.
- Keep code, comments, logs, and documentation in English. User-facing translations belong in JSON locale catalogs.
- Keep hardware access opt-in. Automated tests must use synthetic data and must not contact live accounts or devices.

## Pull requests

A pull request should contain one coherent change, a concise rationale, test evidence, and any security implications. Large refactors should describe migration risk and compatibility impact.

Security vulnerabilities should follow [SECURITY.md](SECURITY.md) instead of a public issue.
