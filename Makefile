# Convenience wrapper. On Windows without make, run the python commands directly.
ENV ?= dev

.PHONY: install test lint scan bootstrap deploy outputs pipeline load-ussd load-prices demo-rollback destroy sample-data

install:
	pip install -r requirements-dev.txt

test:
	pytest --cov --cov-report=term-missing --cov-fail-under=60

lint:
	ruff check .
	cfn-lint infra/*.yaml services/*/template.yaml --ignore-checks W2506

scan:
	gitleaks git --config .gitleaks.toml --redact .
	checkov -d infra -d services --config-file .checkov.yaml

bootstrap:
	python scripts/deploy.py bootstrap

deploy:
	python scripts/deploy.py deploy --env $(ENV)

outputs:
	python scripts/deploy.py outputs --env $(ENV)

pipeline:
	python scripts/deploy.py run-pipeline --env $(ENV)

load-ussd:
	k6 run -e OUTPUTS=.deploy-outputs-$(ENV).json load/ussd_load.js

load-prices:
	k6 run -e OUTPUTS=.deploy-outputs-$(ENV).json load/prices_load.js

demo-rollback:
	python scripts/deploy.py demo-rollback --env prod

destroy:
	python scripts/deploy.py destroy --env $(ENV)

sample-data:
	python scripts/make_sample_data.py
