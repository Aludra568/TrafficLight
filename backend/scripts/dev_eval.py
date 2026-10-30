"""Команды оценки (`python -m svetofor.eval ...`) без Docker — на базе сервера разработки.

Окружение то же, что у `dev_server.py`: встроенный PostgreSQL (если сервер уже запущен —
подключается к его базе), задачи в процессе, без Redis и эмбеддингов. Ключ модели — из `.env`
в корне проекта или из переменных окружения (они важнее). Word-пакеты нужен LibreOffice в PATH;
без него берите PDF-наборы (`generate --format pdf`) и `samples/demo_44fz_postavka_pdf.zip`.

    cd backend
    .venv\\Scripts\\python scripts\\dev_eval.py generate --out ..\\.cache\\eval_pdf --format pdf
    .venv\\Scripts\\python scripts\\dev_eval.py run --set ..\\.cache\\eval_pdf --limit 3
    .venv\\Scripts\\python scripts\\dev_eval.py cassette   # нужен LibreOffice: демо-пакет — Word
"""

from __future__ import annotations

import sys

from dev_server import prepare_environment


def main() -> int:
    prepare_environment()  # база остаётся работать: она постоянная, ей пользуется и сервер
    from svetofor.eval.__main__ import main as eval_main

    return eval_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
