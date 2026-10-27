# Contributing

The brief requires a meaningful commit history from **every** team member. Work like this:

1. Branch per change: `feat/<service>-<topic>`, `fix/...`, `docs/...`.
2. Commit as yourself (`git config user.name` and `user.email` must match your GitHub account). Write small commits with clear messages, e.g. `alerts: cap SMS at one per crop per day`.
3. Open a pull request into `main`. CI must be green: lint, gitleaks, Checkov, tests with ≥ 60% coverage.
4. At least one other member reviews it. Owners are listed in `.github/CODEOWNERS`.
5. Merging to `main` deploys to dev, then to prod as a canary.

## Ownership

| Area | Paths | Owner |
|---|---|---|
| Team lead & cloud architect | `infra/`, `services/matching/`, `docs/architecture.md` | Aaron Zulu |
| DevOps & security | `.github/`, `infra/bootstrap.yaml`, `scripts/deploy.py`, `docs/security.md`, `docs/runbooks.md` | _TBD_ |
| Backend & data | `services/ingestion/`, `services/analytics_api/`, `services/listings_api/`, `shared/` | _TBD_ |
| Frontend, USSD & QA | `services/ussd_gateway/`, `web/`, `load/`, `tests/` | _TBD_ |

## Local checks before pushing
```bash
pip install -r requirements-dev.txt pre-commit && pre-commit install
ruff check . && pytest --cov
```
