"""Кассета демо: ответы настоящей модели для демо-пакета — «Попробовать на примере» без ключа.

Демо-пакет проверяется с моделью, каждый её ответ записывается в `samples/demo_llm_cassette.jsonl`
под ключом без имени модели (`LLMGateway._to_cassette`). Без ключа шлюз работает в режиме replay
и находит там ответы на те же промпты. Ключ включает текст промпта, версию шаблона и схему
ответа, поэтому после правки правил, промптов или демо-пакета кассету нужно записать заново —
устаревшие ответы просто не найдутся, выдуманных не появится.

    docker compose run --rm -v ./samples:/app/samples api python -m svetofor.eval cassette
    docker compose up -d --build   # кассета входит в образ (make demo-cassette — обе команды)
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from svetofor.core.config import Settings
from svetofor.db.models import Check, Package
from svetofor.eval.run import inline_tasks
from svetofor.pipeline.ingest import create_check
from svetofor.pipeline.storage import PackagePaths
from svetofor.rules.store import latest_ruleset_version
from svetofor.tasks import checks


def record(settings: Settings, package: Path, target: Path) -> tuple[int, str, int]:
    """Проверить пакет с моделью, записывая ответы в кассету.

    → (число записей, итог проверки, правил не проверено из-за сбоя модели). Сбойный вызов в
    кассету не попадает, и демо без ключа покажет эти правила «не проверено» — такую запись
    надо повторить (20.10.2026: модель один раз вернула негодный ответ на группу из 3 правил).
    """
    if not settings.llm_configured:
        raise RuntimeError("Нужна модель: задайте LLM_BASE_URL, LLM_API_KEY и LLM_MODEL_MAIN")
    target.unlink(missing_ok=True)  # заново: старые ответы к новым промптам не подходят
    services = checks.build_services(settings, record_cassette=target)
    with services.session_factory() as db:
        version = latest_ruleset_version(db, settings.default_ruleset)
        if version is None:
            raise LookupError(f"Набор правил «{settings.default_ruleset}» не загружен")
        version_id = version.id
    services.ingest.packages_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=services.ingest.packages_dir) as tmp:
        upload = Path(tmp) / package.name
        shutil.copyfile(package, upload)
        with services.session_factory() as db:
            check = create_check(
                db,
                upload=upload,
                original_name=package.name,
                config=services.ingest,
                ruleset_version_id=version_id,
            )
            db.commit()
            check_id = check.id
    with inline_tasks(services):
        checks.run_check.delay(str(check_id))
    with services.session_factory() as db:
        done = db.get(Check, check_id)
        light = done.light.value if done and done.light else "—"
        failed = int(done.counters.get("rules_failed", 0)) if done else 0
        package_row = db.get(Package, done.package_id) if done else None
        if package_row is not None:  # служебная проверка не засоряет историю
            PackagePaths(Path(package_row.storage_path)).remove()
            db.delete(package_row)
            db.commit()
    entries = sum(1 for line in target.read_text(encoding="utf-8").splitlines() if line.strip())
    return entries, light, failed
