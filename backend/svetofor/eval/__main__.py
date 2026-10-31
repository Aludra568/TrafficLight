"""Оценка качества из командной строки.

python -m svetofor.eval generate --out eval/synthetic [--format docx|pdf]
python -m svetofor.eval demo --out samples/demo_44fz_postavka.zip
python -m svetofor.eval run --set synthetic [--limit N] [--keep]
python -m svetofor.eval stress --out samples/stress
python -m svetofor.eval baseline --set synthetic [--limit N]   # «склеили текст — спросили модель»
python -m svetofor.eval boundary --out eval/boundary [--format docx|pdf]  # граница GREEN
python -m svetofor.eval ocr --source samples/demo_44fz_postavka_pdf.zip  # точность OCR (Tesseract)
python -m svetofor.eval cassette   # записать ответы модели для демо без ключа (нужен ключ)
python -m svetofor.eval retrieval --set real [--per-method 20] [--quota 3] [--top N] [--budget N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from svetofor.core.config import get_settings
from svetofor.core.logging import configure_logging
from svetofor.llm.naming import public_model_name


def _generate(args: argparse.Namespace) -> int:
    from svetofor.eval.synthetic import generate
    from svetofor.rules.loader import load_ruleset_file

    settings = get_settings()
    rules = load_ruleset_file(settings.rules_dir / f"{args.ruleset.replace('-', '_')}.yaml").rules
    folders = generate(Path(args.out), rules, args.format)
    print(f"Создано пакетов: {len(folders)} → {args.out}")
    return 0


def _demo(args: argparse.Namespace) -> int:
    from svetofor.eval.synthetic import generate_demo
    from svetofor.rules.loader import load_ruleset_file

    settings = get_settings()
    rules = load_ruleset_file(settings.rules_dir / f"{args.ruleset.replace('-', '_')}.yaml").rules
    print(f"Демо-пакет: {generate_demo(Path(args.out), rules)}")
    return 0


def _boundary(args: argparse.Namespace) -> int:
    from svetofor.eval.boundary import write_set

    folders = write_set(Path(args.out), args.format)
    print(f"Набор «граница GREEN»: {len(folders)} пакетов → {args.out}")
    return 0


def _stress(args: argparse.Namespace) -> int:
    from svetofor.eval.stress import generate_stress

    print(f"Стресс-пакет: {generate_stress(Path(args.out))}")
    return 0


def _baseline(args: argparse.Namespace) -> int:
    from svetofor.db.session import get_sessionmaker
    from svetofor.eval.baseline import baseline_markdown, run_baseline
    from svetofor.llm.cache import DbCache
    from svetofor.llm.gateway import build_gateway
    from svetofor.rules.loader import load_ruleset_file

    settings = get_settings()
    rules = load_ruleset_file(settings.rules_dir / f"{args.ruleset.replace('-', '_')}.yaml").rules
    set_dir = Path(args.set) if Path(args.set).is_dir() else settings.eval_dir / args.set
    gateway = build_gateway(settings, cache=DbCache(get_sessionmaker()))
    report = run_baseline(
        gateway,
        set_dir,
        rules,
        settings.eval_dir / "results",
        model=public_model_name(settings.llm_model_main),
        context_tokens=settings.llm_context_tokens,
        limit=args.limit,
    )
    print(baseline_markdown(report))
    return 0


def _run(args: argparse.Namespace) -> int:
    from svetofor.eval.run import run_set
    from svetofor.tasks.checks import services

    settings = get_settings()
    set_dir = Path(args.set) if Path(args.set).is_dir() else settings.eval_dir / args.set
    report = run_set(
        services(),
        set_dir,
        settings.eval_dir / "results",
        ruleset_code=args.ruleset,
        model=public_model_name(settings.llm_model_main),
        limit=args.limit,
        keep=args.keep,
    )
    for name, data in report["metrics"].items():
        print(f"{name:16} {data['value']}  95% {data['ci95']}")
    return 0


def _retrieval(args: argparse.Namespace) -> int:
    from svetofor.eval.retrieval import run_retrieval
    from svetofor.search.hybrid import QUOTA
    from svetofor.tasks.checks import services

    settings = get_settings()
    set_dir = Path(args.set) if Path(args.set).is_dir() else settings.eval_dir / args.set
    report = run_retrieval(
        services(),
        set_dir,
        settings.eval_dir / "results",
        ruleset_code=args.ruleset,
        per_method=args.per_method,
        quota=QUOTA if args.quota is None else args.quota,
        top=args.top,
        budget=args.budget,
        limit=args.limit,
    )
    print(f"recall {report.recall}  ({report.found}/{len(report.items)})  {report.reasons()}")
    return 0


def _ocr(args: argparse.Namespace) -> int:
    from svetofor.eval.ocr_accuracy import TARGET, measure, quality_mean, report

    scores = measure(Path(args.source), languages=args.languages)
    print(report(scores))
    return 0 if scores and quality_mean(scores) >= TARGET else 1


def _cassette(args: argparse.Namespace) -> int:
    from svetofor.eval.cassette import record

    settings = get_settings()
    base = Path(args.package) if args.package else settings.demo_package
    pdf = base.with_name(f"{base.stem}_pdf{base.suffix}")
    packages = [base, pdf] if pdf.is_file() else [base]
    target = Path(args.out) if args.out else settings.demo_cassette
    try:
        entries, light, failed = record(settings, packages, target)
    except (RuntimeError, LookupError) as exc:  # нет ключа модели или набора правил
        print(f"Кассета не записана: {exc}", file=sys.stderr)
        return 2
    print(f"Кассета: {target} — ответов модели: {entries}; итог демо-проверки: {light}")
    if failed:
        print(
            f"Кассета неполная: {failed} правил не проверено из-за сбоя модели — в демо без "
            "ключа они будут «не проверено». Запишите кассету заново.",
            file=sys.stderr,
        )
        return 3
    return 0 if entries else 1


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    parser = argparse.ArgumentParser(prog="python -m svetofor.eval", description="Оценка качества")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="Сгенерировать синтетические пакеты")
    gen.add_argument("--out", required=True)
    gen.add_argument("--format", choices=["docx", "pdf"], default="docx")
    gen.add_argument("--ruleset", default=settings.default_ruleset)
    demo = sub.add_parser("demo", help="Собрать демо-пакет для samples/")
    demo.add_argument("--out", required=True)
    demo.add_argument("--ruleset", default=settings.default_ruleset)
    run = sub.add_parser("run", help="Прогнать оценку на наборе")
    run.add_argument("--set", required=True, help="имя набора в EVAL_DIR или путь к папке")
    run.add_argument("--limit", type=int)
    run.add_argument("--keep", action="store_true", help="не удалять пакеты оценки из истории")
    run.add_argument("--ruleset", default=settings.default_ruleset)
    retr = sub.add_parser("retrieval", help="Замер поиска без модели: нужный фрагмент в контексте")
    retr.add_argument("--set", required=True, help="имя набора в EVAL_DIR или путь к папке")
    retr.add_argument("--limit", type=int)
    retr.add_argument("--ruleset", default=settings.default_ruleset)
    retr.add_argument("--per-method", type=int, default=20, help="длина списка каждого способа")
    retr.add_argument("--quota", type=int, help="первые места списков вне RRF; 0 — выкл.")
    retr.add_argument("--top", type=int, help="фрагментов после RRF; по умолчанию как в движке")
    retr.add_argument("--budget", type=int, help="бюджет токенов контекста; по умолчанию движка")
    edge = sub.add_parser("boundary", help="Собрать набор «граница GREEN» (eval/boundary)")
    edge.add_argument("--out", required=True)
    edge.add_argument("--format", choices=["docx", "pdf"], default="docx")
    stress = sub.add_parser("stress", help="Собрать стресс-пакет с ловушками для защиты")
    stress.add_argument("--out", required=True)
    base = sub.add_parser("baseline", help="Базовая линия: текст файлов + модель, для сравнения")
    base.add_argument("--set", required=True, help="имя набора в EVAL_DIR или путь к папке")
    base.add_argument("--limit", type=int)
    base.add_argument("--ruleset", default=settings.default_ruleset)
    ocr = sub.add_parser("ocr", help="Точность OCR на сканах из PDF с текстом (критерий 4)")
    ocr.add_argument("--source", required=True, help="PDF, папка с PDF или ZIP-архив")
    ocr.add_argument("--languages", default=settings.ocr_languages)
    tape = sub.add_parser("cassette", help="Записать ответы модели для демо без ключа")
    tape.add_argument("--package", help="пакет; по умолчанию DEMO_PACKAGE")
    tape.add_argument("--out", help="файл кассеты; по умолчанию DEMO_CASSETTE")
    args = parser.parse_args(argv)
    commands = {
        "generate": _generate,
        "demo": _demo,
        "run": _run,
        "retrieval": _retrieval,
        "stress": _stress,
        "baseline": _baseline,
        "boundary": _boundary,
        "ocr": _ocr,
        "cassette": _cassette,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
