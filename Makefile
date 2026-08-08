# Shortcuts for the commands in README.md. Nothing here is required — every
# target is one line you could type by hand.

.DEFAULT_GOAL := help
.PHONY: help db db-stop db-reset api web dev test test-web lint

help:  ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t14

db:  ## Start postgres and wait until it accepts connections
	docker compose up -d --wait db

db-stop:  ## Stop postgres (data persists in the volume)
	docker compose down

db-reset:  ## Stop postgres AND erase all data
	docker compose down -v

api: db  ## Run the backend on :8000
	cd backend && uv run uvicorn backend.main:app --reload

web:  ## Run the frontend on :4200
	cd frontend && npm start

dev: db  ## Run backend and frontend together; Ctrl-C stops both
	@trap 'kill 0' INT TERM EXIT; \
	(cd backend && uv run uvicorn backend.main:app --reload) & \
	(cd frontend && npm start) & \
	wait

test:  ## Run the backend tests
	cd backend && uv run pytest

test-web:  ## Run the frontend tests once, headless
	cd frontend && npx ng test --watch=false --browsers=ChromeHeadless

lint:  ## Lint the backend
	cd backend && uv run ruff check .
