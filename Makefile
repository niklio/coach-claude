.PHONY: setup install dev

setup:
	python3.12 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	@if [ ! -f .env ]; then cp .env.example .env && echo "Created .env from .env.example — fill in your credentials."; else echo ".env already exists, skipping copy."; fi

install:
	pip install -r requirements.txt

dev:
	flask --app app run --reload --debug --port 8000
