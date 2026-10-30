"""Окружение внешних программ, которые открывают чужие документы (LibreOffice, OCRmyPDF).

Документ закупки — недоверенный вход. У LibreOffice были уязвимости, когда документ подставлял
переменные окружения в адрес и отправлял их наружу (CVE-2024-12426), а конвертер скачивал
внешние ресурсы из документа в обход фильтров приложения (CVE-2026-55229, Gotenberg). В
окружении воркера лежат ключ модели, пароль базы и секрет подписи входа — программе, которая
читает документ, они не нужны. Передаём только системные переменные из белого списка: даже
новая уязвимость того же класса не найдёт секретов.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

_ALLOWED = frozenset(
    {
        # поиск программ и системные пути
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        # временные файлы и домашняя папка (профиль LibreOffice задаётся отдельно)
        "TMP",
        "TEMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        # язык и кодировка — без них кириллица в именах и тексте ломается
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        # настройки самих программ
        "TESSDATA_PREFIX",
        "OMP_THREAD_LIMIT",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        "VIRTUAL_ENV",
    }
)


def tool_env() -> dict[str, str]:
    """Окружение для программы, читающей недоверенный документ: без секретов приложения."""
    return {key: value for key, value in os.environ.items() if key.upper() in _ALLOWED}


def run_untrusted(
    command: Sequence[str], *, timeout: float, capture: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """Запустить программу, которая читает чужой файл (LibreOffice, OCRmyPDF, bsdtar): без
    секретов в окружении, без ввода; вывод — только если нужен (`capture`), иначе отброшен.
    Исключения subprocess (TimeoutExpired, FileNotFoundError) — вызывающему."""
    output = subprocess.PIPE if capture else subprocess.DEVNULL
    return subprocess.run(
        list(command),
        env=tool_env(),
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=output,
        timeout=timeout,
        check=False,
    )
