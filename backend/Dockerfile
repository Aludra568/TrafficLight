# syntax=docker/dockerfile:1.7
# Образ svetofor-app: API и воркеры Celery.
# Контекст сборки — корень репозитория: в образ нужны rules/ и samples/.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/app

# Зеркало Debian: из России deb.debian.org отдаёт пакеты в 3–10 раз медленнее, и одна только
# установка LibreOffice шла бы десятки минут. Официальное — опцией сборки:
#   docker compose build --build-arg DEBIAN_MIRROR=http://deb.debian.org/debian
# Подписи пакетов apt проверяет одинаково для любого зеркала.
ARG DEBIAN_MIRROR=http://mirror.yandex.ru/debian

# LibreOffice — DOCX/DOC/RTF/XLSX в PDF; Tesseract (rus+eng), Ghostscript — OCR сканов через
# OCRmyPDF; bsdtar — RAR/7z/tar; шрифты с кириллицей и метрически совместимые с Calibri/Cambria,
# иначе страницы документа «поплывут» при конвертации; curl — проверка готовности.
RUN sed -i "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-calc-nogui \
        tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng ghostscript pngquant unpaper \
        libarchive-tools \
        fonts-dejavu-core fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv ставит зависимости ровно по uv.lock (формат lock-файла новый — берём свежий uv)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# сначала только зависимости: слой кэшируется, пока не меняются pyproject.toml и uv.lock
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# наборы правил и демо-пакет для первой загрузки (RULES_DIR, DEMO_PACKAGE)
COPY rules/ /app/rules/
COPY samples/ /app/samples/

# не root: LibreOffice и OCR разбирают чужие файлы — меньше прав у процесса, меньше вреда
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

EXPOSE 8000
CMD ["uvicorn", "svetofor.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
