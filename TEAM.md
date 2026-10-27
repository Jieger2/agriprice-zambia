# Team roster

This file is the single place where roles are recorded. **To change a role, edit this table and the matching lines in `.github/CODEOWNERS` in the same pull request.** Fill in names as members confirm them.

| GitHub | Name | Role | Owns (paths) | Thesis section |
|---|---|---|---|---|
| @Aaron28Zulu | Aaron Zulu | Team Lead & Cloud Architect | `infra/network.yaml`, `infra/data.yaml`, `services/matching/`, `docs/architecture.md` | 5, 6, 13 |
| @Benaiah09 | _(add name)_ | DevOps & Security Engineer | `.github/`, `infra/bootstrap.yaml`, `infra/budget.yaml`, `infra/observability.yaml`, `scripts/deploy.py`, `docs/security.md`, `docs/runbooks.md`, `docs/slo.md` | 7, 8, 9 |
| @Jedyn128 | _(add name)_ | Backend API Engineer | `services/listings_api/`, `services/alerts/`, `shared/` | 4, 5.2–5.4 |
| @oliverkakungu | _(add name)_ | Data & Analytics Engineer | `services/ingestion/`, `services/analytics_api/`, `data/`, `scripts/make_sample_data.py`, `docs/cost.md` | 5.5, 5.8, 10 |
| @Mwewacs | _(add name)_ | Frontend, USSD & QA Engineer | `services/ussd_gateway/`, `web/`, `load/`, `tests/`, `docs/demo-script.md`, `docs/i18n-review.md` | 5.6, 5.7, 11 |

## Responsibilities

- **Team Lead & Cloud Architect:** owns the overall design and cross-service decisions, reviews any change to shared infrastructure, and chairs the weekly sprint review.
- **DevOps & Security:** keeps the CI/CD pipeline green, runs the AWS bootstrap, manages repository variables and secrets, owns alarms, runbooks and the rollback demonstration.
- **Backend API:** owns the REST API, alerts and the shared library, and keeps the IAM justification table current for these services.
- **Data & Analytics:** owns price sources (including `WFP_CSV_URL`), the ingestion pipeline, Athena and analytics, and the cost report with the 10× projection.
- **Frontend, USSD & QA:** owns the USSD menus and their character budgets, the web app, load tests, the auto-scaling demonstration and overall test coverage.

Everyone commits to their own areas from their own account. The brief marks the commit history of each member.
