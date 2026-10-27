# Contributing

The brief requires a meaningful commit history from **every** team member. Work like this:

1. Branch per change: `feat/<service>-<topic>`, `fix/...`, `docs/...`.
2. Commit as yourself (`git config user.name` and `user.email` must match your GitHub account). Write small commits with clear messages, e.g. `alerts: cap SMS at one per crop per day`.
3. Open a pull request into `main`. CI must be green: lint, gitleaks, Checkov, tests with ≥ 60% coverage.
4. At least one other member reviews it. Owners are listed in `.github/CODEOWNERS`.
5. Merging to `main` deploys to dev, then to prod as a canary.

## Ownership

Roles and owned paths are listed in **`TEAM.md`** (the single editable roster). `.github/CODEOWNERS` requests reviews from the owner automatically.

## First-time setup for each member

1. Accept the repository invitation (the owner adds you under Settings → Collaborators with the **Write** role).
2. `git clone https://github.com/Jieger2/agriprice-zambia.git`
3. `git config user.name "Your Name"` and `git config user.email "<email on your GitHub account>"`, so your commits count towards your history.
4. Make your first change to one of your own paths, for example adding your name to `TEAM.md`, and open a pull request.

## Local checks before pushing
```bash
pip install -r requirements-dev.txt pre-commit && pre-commit install
ruff check . && pytest --cov
```
