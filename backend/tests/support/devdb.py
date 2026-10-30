"""Временный PostgreSQL (+pgvector) для тестов без Docker.

Приоритет: переменная TEST_DATABASE_URL (CI, Docker) → встроенный PostgreSQL из пакета pgserver
(только для локальной разработки). PostgreSQL под Windows не работает, если в пути к его файлам
есть кириллица, поэтому бинарники копируются в каталог с латинским путём.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


def _ascii_cache_dir() -> Path:
    """Каталог для бинарников и временных данных PostgreSQL. Путь обязан быть латиницей.

    SVETOFOR_DEV_DIR — явный выбор (например, D:\\svetofor-dev, чтобы не занимать системный диск).
    """
    explicit = os.environ.get("SVETOFOR_DEV_DIR")
    if explicit:
        path = Path(explicit)
        if not str(path).isascii():
            raise RuntimeError(f"SVETOFOR_DEV_DIR должен быть путём без кириллицы: {path}")
        return path
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = Path(base) / "svetofor-dev"
    if not str(path).isascii():
        path = Path(tempfile.gettempdir()) / "svetofor-dev"
    return path


def _pg_binaries() -> Path | None:
    try:
        import pgserver  # type: ignore[import-not-found]
    except ImportError:
        return None
    src = Path(pgserver.__file__).parent / "pginstall"
    if str(src).isascii():
        return src / "bin"
    dst = _ascii_cache_dir() / "pginstall"
    if not (dst / "bin").exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    return dst / "bin"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class DevPostgres:
    bin_dir: Path
    data_dir: Path
    port: int
    persistent: bool = False  # данные переживают остановку (локальный сервер разработки)

    @property
    def url(self) -> str:
        return f"postgresql+psycopg://postgres@127.0.0.1:{self.port}/postgres"

    def _run(self, exe: str, *args: str) -> None:
        # Никаких каналов (PIPE): запущенный сервер наследует дескрипторы pg_ctl, и чтение канала
        # зависло бы навсегда. Диагностика — в server.log и в коде возврата.
        result = subprocess.run(
            [str(self.bin_dir / exe), *args],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        if result.returncode != 0:
            log = self.data_dir / "server.log"
            tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
            raise RuntimeError(f"{exe} завершился с кодом {result.returncode}\n{tail}")

    def responds(self) -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def start(self) -> None:
        if not (self.data_dir / "PG_VERSION").exists():
            self._run(
                "initdb",
                "-D",
                str(self.data_dir),
                "--auth=trust",
                "--encoding=UTF8",
                "--locale=C",
                "-U",
                "postgres",
            )
        log = self.data_dir / "server.log"
        # pg_ctl -w ждёт готовности; вывод — в файл, иначе дочерний сервер держит наши дескрипторы.
        self._run(
            "pg_ctl",
            "-D",
            str(self.data_dir),
            "-o",
            f"-p {self.port} -c fsync=off",
            "-l",
            str(log),
            "-w",
            "start",
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.responds():
                return
            time.sleep(0.2)
        raise RuntimeError("PostgreSQL не запустился за 30 с")

    def stop(self) -> None:
        if self.persistent:
            self._run("pg_ctl", "-D", str(self.data_dir), "-m", "fast", "stop")
            return
        try:
            self._run("pg_ctl", "-D", str(self.data_dir), "-m", "immediate", "stop")
        finally:
            shutil.rmtree(self.data_dir, ignore_errors=True)


def start_dev_postgres() -> DevPostgres | None:
    """Поднять временный сервер или вернуть None, если pgserver не установлен."""
    bin_dir = _pg_binaries()
    if bin_dir is None:
        return None
    data_dir = Path(tempfile.mkdtemp(prefix="pgdata-", dir=_ascii_cache_dir()))
    shutil.rmtree(data_dir)  # initdb сам создаёт каталог
    server = DevPostgres(bin_dir=bin_dir, data_dir=data_dir, port=_free_port())
    server.start()
    return server


def start_persistent_postgres(name: str = "devdb") -> DevPostgres | None:
    """Постоянная база для локального сервера разработки: данные и вход переживают перезапуск.

    Если прошлый запуск оборвался (процесс убит, сервер базы остался работать), используется
    уже работающий сервер — порт берётся из postmaster.pid, новые копии не плодятся.
    """
    bin_dir = _pg_binaries()
    if bin_dir is None:
        return None
    data_dir = _ascii_cache_dir() / name
    pid_file = data_dir / "postmaster.pid"
    if pid_file.exists():
        lines = pid_file.read_text(errors="replace").splitlines()
        if len(lines) > 3 and lines[3].strip().isdigit():
            running = DevPostgres(bin_dir, data_dir, int(lines[3]), persistent=True)
            if running.responds():
                return running
    server = DevPostgres(bin_dir=bin_dir, data_dir=data_dir, port=_free_port(), persistent=True)
    server.start()
    return server
