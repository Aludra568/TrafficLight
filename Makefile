# Светофор — частые команды. Внутри Docker: make up / test / eval. Локально (без Docker): make test-local.
COMPOSE ?= docker compose
API     := $(COMPOSE) exec api
RUN     := $(COMPOSE) run --rm api
SET     ?= synthetic

.PHONY: eval-ocr demo-cassette up down logs ps build test smoke acceptance test-local lint lint-local fmt eval eval-retrieval eval-baseline eval-generate eval-boundary eval-boundary-sweep demo-package stress-package migrate seed reembed rules-import backup reset

up:              ## Поднять всё приложение (http://localhost:8080)
	$(COMPOSE) up --build -d

down:            ## Остановить
	$(COMPOSE) down

logs:            ## Логи API и воркеров
	$(COMPOSE) logs -f api worker-ingest worker-llm

ps:
	$(COMPOSE) ps

build:
	$(COMPOSE) build

test:            ## Все тесты внутри контейнера (включая помеченные docker)
	# образ собран без dev-зависимостей: pytest ставится во временный контейнер (--rm);
	# тесты — на исходниках репозитория (/repo: им нужны rules/, eval/, samples/ рядом с backend),
	# LibreOffice и Tesseract — из образа; база — отдельная svetofor_test (тесты делают TRUNCATE)
	$(COMPOSE) up -d --wait postgres
	-$(COMPOSE) exec -T postgres createdb -U svetofor svetofor_test
	$(COMPOSE) run --rm -u root -v ./:/repo -w /repo/backend \
		-e TEST_DATABASE_URL=postgresql+psycopg://svetofor:svetofor@postgres:5432/svetofor_test \
		api sh -c "uv sync --frozen --quiet && pytest"

smoke:           ## Демо-сценарий на запущенном стеке: вход, демо-пакеты, светофор, Word-отчёт
	python3 scripts/smoke_demo.py http://localhost:$${APP_PORT:-8080}

acceptance:      ## Критерии приёмки на запущенном стеке → таблица; PACKAGE20=пакет.zip для критерия 5
	python3 scripts/acceptance.py http://localhost:$${APP_PORT:-8080} \
		$(if $(PACKAGE20),--package20 $(PACKAGE20)) --out eval/results/acceptance.json

test-local:      ## Тесты без Docker (пропускает помеченные docker)
	cd backend && uv run pytest -m "not docker"

lint:            ## Линтеры бэкенда и фронтенда (ruff, mypy, eslint, tsc)
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy svetofor
	cd frontend && npm run lint

lint-local:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy svetofor

fmt:
	cd backend && uv run ruff format . && uv run ruff check --fix .

eval:            ## Метрики качества: make eval SET=synthetic|real
	$(RUN) python -m svetofor.eval run --set $(SET)

eval-retrieval:  ## Поиск без модели: нужный фрагмент в контексте (make eval-retrieval SET=real)
	$(RUN) python -m svetofor.eval retrieval --set $(SET)

eval-baseline:   ## Базовая линия «текст файлов + модель» на том же наборе (нужен ключ модели)
	$(RUN) python -m svetofor.eval baseline --set $(SET)

eval-generate:   ## Пересобрать синтетический набор eval/synthetic из шаблонов
	cd backend && RULES_DIR=../rules uv run python -m svetofor.eval generate --out ../eval/synthetic

eval-ocr:        ## Точность OCR (критерий 4): текстовые PDF демо → сканы → Tesseract → сравнение
	$(RUN) python -m svetofor.eval ocr --source samples/demo_44fz_postavka_pdf.zip

eval-boundary:   ## Набор «граница GREEN»: риск словами, которых система не знает (нужен ключ модели)
	cd backend && uv run python -m svetofor.eval boundary --out ../eval/boundary
	$(RUN) python -m svetofor.eval run --set boundary

demo-cassette:    ## Записать ответы модели для демо без ключа (нужен ключ модели в .env)
	$(COMPOSE) run --rm -v ./samples:/app/samples api python -m svetofor.eval cassette
	$(COMPOSE) up -d --build   # кассета входит в образ: пересобрать, чтобы воркеры её прочли

eval-boundary-sweep: ## То же с поштучной проверкой пунктов (GREEN_SWEEP): разница — польза шага 3
	$(RUN) env GREEN_SWEEP=true python -m svetofor.eval run --set boundary

stress-package:  ## Пересобрать стресс-пакет с ловушками samples/stress (для защиты)
	cd backend && RULES_DIR=../rules uv run python -m svetofor.eval stress --out ../samples/stress

demo-package:    ## Пересобрать демо-пакет samples/demo_44fz_postavka.zip
	cd backend && RULES_DIR=../rules uv run python -m svetofor.eval demo --out ../samples/demo_44fz_postavka.zip

migrate:         ## Новая миграция: make migrate MSG="описание"
	$(RUN) alembic revision --autogenerate -m "$(MSG)"

seed:            ## Повторно применить сиды (идемпотентно)
	$(RUN) python -m svetofor.seed

reembed:         ## Пересчитать эмбеддинги после смены EMBED_MODEL
	$(RUN) python -m svetofor.search.reembed

rules-import:    ## make rules-import FILE=rules/44fz_supplier_base.yaml
	$(RUN) python -m svetofor.seed.import_rules /app/$(FILE)

backup:          ## Резервная копия БД в ./backups
	mkdir -p backups && $(COMPOSE) exec -T postgres pg_dump -U svetofor svetofor > backups/svetofor_$$(date +%Y%m%d_%H%M%S).sql

reset:           ## Полный сброс данных (осторожно!)
	$(COMPOSE) down -v
