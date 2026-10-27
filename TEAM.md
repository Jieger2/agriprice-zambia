# Team roster

This file is the single place where roles are recorded. **To change a role, edit this table and the matching lines in `.github/CODEOWNERS` in the same pull request.** Correct or fill in names as members confirm them.

| GitHub | Name | Role | Owns (paths) | Thesis section |
|---|---|---|---|---|
| @Jieger2 | Idan Musama Mwaba | Team Lead & Cloud Architect | `infra/network.yaml`, `infra/data.yaml`, `services/matching/`, `docs/architecture.md` | 5, 6, 13 |
| @Benaiah09 | Benaiah Masase | DevOps & Security Engineer | `.github/`, `infra/bootstrap.yaml`, `scripts/deploy.py`, `docs/security.md` | 7, 8 |
| @victorwilliamkangomu-bot | Victor William Kangomu _(confirm)_ | Observability & Reliability Engineer | `infra/observability.yaml`, `infra/budget.yaml`, `docs/slo.md`, `docs/runbooks.md`, `tests/smoke/` | 9 |
| @Jedyn128 | Jeriah Chikwanda | Backend API Engineer | `services/listings_api/`, `services/alerts/`, `shared/` | 4, 5.2–5.4 |
| @oliverkakungu | Oliver Kakungu | Data & Analytics Engineer | `services/ingestion/`, `services/analytics_api/`, `data/`, `scripts/make_sample_data.py`, `docs/cost.md` | 5.5, 5.8, 10 |
| @Mwewacs | Mwewa Chibu | Frontend, USSD & QA Engineer | `services/ussd_gateway/`, `web/`, `load/`, `tests/unit/`, `docs/demo-script.md`, `docs/i18n-review.md` | 5.6, 5.7, 11 |

Lecturer: Mr Mwiiya

## Responsibilities

- **Team Lead & Cloud Architect:** owns the overall design and cross-service decisions, reviews changes to shared infrastructure, and chairs the weekly sprint review.
- **DevOps & Security:** keeps the CI/CD pipeline green, runs the AWS bootstrap, manages repository variables and secrets, and owns the IAM justification and the automated-rollback demonstration.
- **Observability & Reliability:** owns the dashboard, alarms, SLOs, runbooks, budget alerts and post-deploy smoke tests, and runs the alarm drills.
- **Backend API:** owns the REST API, alerts and the shared library, and keeps the IAM justification table current for these services.
- **Data & Analytics:** owns the price sources (including `WFP_CSV_URL`), the ingestion pipeline, Athena and analytics, and the cost report with the 10× projection.
- **Frontend, USSD & QA:** owns the USSD menus and their character budgets, the web app, load tests, the auto-scaling demonstration and overall unit-test coverage.

Everyone commits to their own areas from their own account. The brief marks the commit history of each member.
