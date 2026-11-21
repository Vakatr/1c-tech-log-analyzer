# Security policy

## What must never be committed

- real 1C technological logs or customer data;
- ClickHouse passwords, tokens or connection strings with credentials;
- `config/clickhouse.local.json` and `config/presets.json` (both are gitignored);
- `vendor/logt/src/config.json` (external checkout, not part of this repository).

## How secrets are handled

- ClickHouse credentials are read from the GUI, environment variables
  (`CLICKHOUSE_*`) or a local gitignored config.
- Passwords are redacted from GUI errors, logs and child process arguments.
- The bundled `docker-compose.yml` uses the password `1` for a local development
  container only. Never reuse it outside a local machine.

## Reporting a vulnerability

Please use GitHub Security Advisories ("Report a vulnerability") instead of a
public issue. Include the version, reproduction steps and impact. Do not attach
real logs or credentials.
