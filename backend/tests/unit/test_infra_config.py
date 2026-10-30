"""Настройки контейнеров, от которых зависит безопасность: настоящий адрес клиента в API.

Лимит попыток входа считает неудачи по адресу клиента. Без доверия к заголовку от nginx все
пользователи для API — один адрес nginx, и 50 чужих ошибок закрывали вход всем. А дописанный,
а не перезаписанный заголовок клиент подделал бы и обходил лимит.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NGINX = ROOT / "infra" / "nginx" / "default.conf"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "backend" / "Dockerfile"

pytestmark = pytest.mark.skipif(not NGINX.is_file(), reason="нет файлов развёртывания (образ)")


def test_nginx_overwrites_forwarded_for() -> None:
    text = NGINX.read_text(encoding="utf-8")
    values = re.findall(r"proxy_set_header\s+X-Forwarded-For\s+(\S+);", text)
    assert values and set(values) == {"$remote_addr"}


def test_nginx_serves_mjs_as_javascript() -> None:
    """Обработчик PDF (pdf.worker.*.mjs) с nosniff запускается, только если nginx отдаёт его как
    JavaScript; в mime.types nginx 1.27 расширения .mjs нет (Docker, 20.10.2026)."""
    text = NGINX.read_text(encoding="utf-8")
    block = re.search(r"location ~\*? \\\.mjs\$ \{(.*?)\}\s*\}", text, re.S)
    assert block is not None
    assert re.search(r"default_type\s+application/javascript;", block.group(1))


def test_uvicorn_trusts_nginx_headers() -> None:
    assert "--forwarded-allow-ips '*'" in COMPOSE.read_text(encoding="utf-8")
    assert '"--forwarded-allow-ips", "*"' in DOCKERFILE.read_text(encoding="utf-8")


def test_api_port_is_not_published() -> None:
    """Доверие к заголовку безопасно, только пока к API ходит один nginx."""
    compose = COMPOSE.read_text(encoding="utf-8")
    api = compose.split("\n  api:\n", 1)[1].split("\n  worker-ingest:", 1)[0]
    assert "ports:" not in api


SNIPPETS = ROOT / "infra" / "nginx"
FRONT_DOCKERFILE = ROOT / "frontend" / "Dockerfile"


def _service(compose: str, name: str) -> str:
    """Блок сервиса docker-compose.yml: от «  name:» до следующего сервиса верхнего уровня."""
    block = re.search(rf"\n  {name}:\n(.*?)(?=\n  [a-z][\w-]*:\n|\nvolumes:)", compose, re.S)
    assert block is not None, name
    return block.group(1)


def test_nginx_pages_carry_security_headers() -> None:
    """Каждая отдача интерфейса — с CSP и запретом встраивания: add_header внутри location
    отменяет заголовки уровня server, поэтому заголовки подключаются в каждом location."""
    text = NGINX.read_text(encoding="utf-8")
    assert re.search(r"^\s*server_tokens off;", text, re.M)
    pages = (SNIPPETS / "security_headers.conf").read_text(encoding="utf-8")
    assert "script-src 'self';" in pages and "frame-ancestors 'none'" in pages
    assert "unsafe-eval" not in pages and "X-Frame-Options DENY" in pages
    api = (SNIPPETS / "api_headers.conf").read_text(encoding="utf-8")
    assert "default-src 'none'" in api and "Cache-Control no-store" in api
    # в каждом location с add_header — подключён один из наборов заголовков (кроме описания API)
    for block in re.findall(r"location [^{]+\{(.*?)\n    \}", text, re.S):
        if "add_header" in block and "Content-Security-Policy" not in block:
            assert "snippets/security_headers.conf" in block or "snippets/api_headers.conf" in block
    dockerfile = FRONT_DOCKERFILE.read_text(encoding="utf-8")
    assert "security_headers.conf" in dockerfile and "api_headers.conf" in dockerfile


def test_nginx_limits_api_request_rate() -> None:
    text = NGINX.read_text(encoding="utf-8")
    assert "limit_req_zone $binary_remote_addr zone=api" in text
    api = text.split("location /api/ {", 1)[1].split("}", 1)[0]
    assert "limit_req zone=api" in api


def test_database_and_redis_are_not_reachable_from_outside() -> None:
    """База и Redis без пароля по умолчанию — только во внутренней сети без выхода наружу."""
    compose = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"\nnetworks:\n  internal:\n    internal: true", compose)
    for name in ("postgres", "redis"):
        block = _service(compose, name)
        assert "networks: [internal]" in block and "ports:" not in block, name


def test_app_containers_drop_privileges() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    app = compose.split("x-app: &app", 1)[1].split("\nservices:", 1)[0]
    assert '"no-new-privileges:true"' in app and "cap_drop: [ALL]" in app
    assert "pids_limit:" in app
    nginx = _service(compose, "nginx")
    assert "read_only: true" in nginx and '"no-new-privileges:true"' in nginx
    assert "USER app" in DOCKERFILE.read_text(encoding="utf-8")
