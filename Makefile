.PHONY: install test run seed up down logs restart backup

install:
	python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt

test:
	./.venv/bin/python -m pytest -q

run:
	./.venv/bin/python -m bot

seed:
	./.venv/bin/python -m bot.seed

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

restart:
	docker compose restart

backup:
	cp data/bot.db data/bot-manual-$$(date +%Y%m%d-%H%M).db
