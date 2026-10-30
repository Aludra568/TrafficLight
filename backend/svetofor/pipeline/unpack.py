"""Распаковка пакета документов.

Входные файлы недоверенные, поэтому:
- объём распакованного считается по факту, а не по заголовкам архива (заголовки могут врать);
- отсекаются zip-бомбы (коэффициент сжатия), выход за пределы каталога, лимит числа файлов;
- имена в архивах Windows без флага UTF-8 декодируются как cp866/cp1251 (русские имена).

RAR, 7z и tar распаковывает libarchive (`bsdtar`, лицензия BSD; в Windows — встроенный
`tar.exe`): в ЕИС заказчики часто прикладывают RAR. Перед распаковкой объём и сжатие
проверяются по заголовкам (libarchive не пишет больше заявленного размера записи), сама
распаковка — с ограничением времени и защитой bsdtar от «..», абсолютных путей и ссылок;
затем файлы проходят те же проверки, что и из ZIP.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import zipfile
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

from svetofor.core.tool_env import run_untrusted

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".rtf",
        ".odt",
        ".xls",
        ".xlsx",
        ".ods",
        ".txt",
        ".xml",
        ".jpg",
        ".jpeg",
        ".png",
        ".tif",
        ".tiff",
        ".bmp",
    }
)
LIBARCHIVE_EXTENSIONS = frozenset({".rar", ".7z", ".tar", ".tgz"})
ARCHIVE_EXTENSIONS = frozenset({".zip"}) | LIBARCHIVE_EXTENSIONS
UNSUPPORTED_ARCHIVES = frozenset({".gz", ".bz2", ".xz", ".arj", ".cab"})
ARCHIVE_TIMEOUT = 300  # с на распаковку одного архива RAR/7z
# строка `bsdtar -tv`: права, число ссылок, владелец, группа, размер, …
_LISTING = re.compile(r"^(?P<mode>[-dlbcps]\S{9})\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s")
_JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
# файлы без текста документа: открепленные подписи и сертификаты рядом с документами ЭТП
NON_CONTENT_EXTENSIONS = frozenset({".sig", ".sgn", ".p7s", ".cer", ".crt"})
EMPTY_FILE = "пустой файл"
DUPLICATE_OF = "дубликат файла"
_JUNK_DIRS = frozenset({"__macosx"})
_UTF8_FLAG = 0x800
_ENCRYPTED_FLAG = 0x1
_COPY_CHUNK = 1024 * 1024
_RATIO_MIN_BYTES = 1024 * 1024  # коэффициент сжатия проверяем только для файлов крупнее 1 МБ
_CYRILLIC_OR_NAME = re.compile(r"[А-Яа-яЁё0-9 ._\-()№,]")
# записей в архиве всего (с пропущенными) — на лимит файлов: архив из миллиона пустых или
# чужих записей занял бы воркер на часы и базу — миллионом строк «пропущено»
ENTRIES_PER_FILE_LIMIT = 10
# ZIP не читается: испорчены данные или имя в заголовке, неверная контрольная сумма или
# смещение, метод сжатия, которого нет в zipfile (Deflate64 из Проводника Windows, PPMd).
# Найдено перебором испорченных архивов: треть давала «внутреннюю ошибку» вместо причины
_BROKEN_READ = (
    zipfile.BadZipFile,
    zlib.error,
    NotImplementedError,
    EOFError,
    UnicodeDecodeError,
    OSError,
    struct.error,
)
BROKEN_ENTRY = "файл в архиве повреждён или сжат неподдерживаемым способом — перепакуйте архив"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class _BrokenReadError(Exception):
    """Не читается сам архив — в отличие от сбоя записи на наш диск (место кончилось)."""


class _Reading:
    """Поток записи ZIP, у которого любая ошибка чтения — `_BrokenReadError`."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def read(self, size: int) -> bytes:
        try:
            return cast(bytes, self._source.read(size))
        except _BROKEN_READ as exc:
            raise _BrokenReadError from exc


@dataclass(frozen=True)
class UnpackLimits:
    max_files: int = 200
    max_unpacked_bytes: int = 1024 * 1024 * 1024
    max_depth: int = 2
    max_ratio: int = 100


@dataclass(frozen=True)
class UnpackedFile:
    relative_path: str  # путь внутри пакета (для пользователя), через «/»
    path: Path | None  # где лежит на диске; None для пропущенных
    size: int
    sha256: str | None
    skipped_reason: str | None = None

    @property
    def filename(self) -> str:
        return PurePosixPath(self.relative_path).name

    @property
    def extension(self) -> str:
        return PurePosixPath(self.relative_path).suffix.lower()

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


class UnpackError(ValueError):
    """Пакет нельзя обработать целиком. Сообщение — для пользователя."""


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """Имя файла в архиве.

    Без флага UTF-8 zipfile декодирует имя как cp437 — восстанавливаем исходные байты.
    """
    if info.flag_bits & _UTF8_FLAG:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    try:
        return raw.decode("utf-8")  # некоторые архиваторы пишут UTF-8 без флага
    except UnicodeDecodeError:
        pass
    candidates = []
    for encoding in ("cp866", "cp1251"):
        text = raw.decode(encoding, errors="replace")
        score = sum(1 for ch in text if _CYRILLIC_OR_NAME.match(ch))
        candidates.append((score, encoding == "cp866", text))  # при равенстве — cp866 (Проводник)
    return max(candidates)[2]


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LATIN1_HIGH = re.compile(r"[À-ÿ]")


def repair_filename(name: str) -> str:
    """Имя файла из формы загрузки, прочитанное не в той кодировке: «Ïðîåêò.pdf» → «Проект.pdf».

    Браузер шлёт имя в UTF-8, а curl и скрипты под Windows — в кодировке системы (cp1251); сервер
    читает такие байты как latin-1. Исправляем, только если в имени нет кириллицы, а после
    перекодировки она появилась — обычные латинские имена с «é» не трогаем.
    """
    if _CYRILLIC.search(name) or not _LATIN1_HIGH.search(name):
        return name
    try:
        raw = name.encode("latin-1")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "cp1251"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        letters = [ch for ch in text if ch.isalpha()]
        # кириллица — хотя бы половина букв: «Résumé» в cp1251 даёт «Rйsumй» и остаётся как было
        if letters and 2 * len(_CYRILLIC.findall(text)) >= len(letters):
            return text
    return name


def safe_relative_path(name: str) -> str | None:
    """Нормализованный относительный путь или None, если путь опасный (абсолютный, с «..»)."""
    # управляющие символы в имени невидимы, а Word и журналы на них спотыкаются
    name = _CONTROL_CHARS.sub("_", name.replace("\\", "/"))
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _disk_name(index: int, relative_path: str) -> str:
    stem = PurePosixPath(relative_path).name
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "file"
    return f"{index:04d}_{cleaned[:120]}"


def declared_listing(bsdtar: str, archive: Path) -> tuple[int, int] | None:
    """(сумма размеров, число записей) по заголовкам архива. None — заголовки не прочитать.

    Число записей — до распаковки: архив RAR/7z из миллиона пустых файлов иначе распаковался бы
    целиком (минуты работы и миллион файлов на диске), прежде чем сработал бы лимит."""
    try:
        listing = run_untrusted([bsdtar, "-tvf", str(archive)], timeout=60, capture=True)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode != 0:
        return None
    total = entries = 0
    for line in listing.stdout.decode("utf-8", errors="replace").splitlines():
        match = _LISTING.match(line)
        if match and match.group("mode").startswith("-"):
            total += int(match.group("size"))
            entries += 1
    return total, entries


def declared_zip_entries(archive: Path) -> int | None:
    """Число записей по концевой записи ZIP (и ZIP64) — без разбора оглавления. None — не ZIP."""
    try:
        with archive.open("rb") as stream:
            record = zipfile._EndRecData(stream)  # type: ignore[attr-defined]
    except (OSError, ValueError, struct.error):
        return None
    return int(record[zipfile._ECD_ENTRIES_TOTAL]) if record else None  # type: ignore[attr-defined]


class _Unpacker:
    def __init__(self, dest: Path, limits: UnpackLimits, bsdtar: str | None) -> None:
        self.dest = dest
        self.limits = limits
        self.bsdtar = bsdtar
        self.files: list[UnpackedFile] = []
        self.accepted = 0  # файлов к проверке (без пропущенных) — счётчик, а не пересчёт
        self.entries = 0  # всех записей архивов, включая пропущенные
        self.total_bytes = 0
        self.seen_hashes: dict[str, str] = {}

    def _next_path(self, relative_path: str) -> Path:
        return self.dest / _disk_name(len(self.files), relative_path)

    @property
    def entry_limit(self) -> int:
        return self.limits.max_files * ENTRIES_PER_FILE_LIMIT

    def _too_many_entries(self) -> NoReturn:
        raise UnpackError(
            f"В архиве больше {self.entry_limit} записей. "
            "Проверьте, что это документация закупки, или разделите архив"
        )

    def _count_entry(self) -> None:
        self.entries += 1
        if self.entries > self.entry_limit:
            self._too_many_entries()

    def _count_file(self) -> None:
        if self.accepted >= self.limits.max_files:
            raise UnpackError(
                f"В пакете больше {self.limits.max_files} файлов. Разделите его на несколько частей"
            )

    def _skip(self, relative_path: str, reason: str, size: int = 0) -> None:
        self.files.append(UnpackedFile(relative_path, None, size, None, reason))

    def _accept(self, relative_path: str, path: Path, size: int, digest: str) -> None:
        if size == 0:
            path.unlink(missing_ok=True)
            self._skip(relative_path, EMPTY_FILE)
            return
        duplicate_of = self.seen_hashes.get(digest)
        if duplicate_of is not None:
            path.unlink(missing_ok=True)
            self._skip(relative_path, f"{DUPLICATE_OF} «{duplicate_of}»", size)
            return
        self.seen_hashes[digest] = relative_path
        self.accepted += 1
        self.files.append(UnpackedFile(relative_path, path, size, digest))

    def _copy_stream(self, source: object, target: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        written = 0
        with target.open("wb") as out:
            while True:
                block = source.read(_COPY_CHUNK)  # type: ignore[attr-defined]
                if not block:
                    break
                written += len(block)
                self.total_bytes += len(block)
                if self.total_bytes > self.limits.max_unpacked_bytes:
                    raise UnpackError(
                        "Архив распаковывается в слишком большой объём. Проверьте, что это "
                        "документация закупки, или разделите архив"
                    )
                digest.update(block)
                out.write(block)
        return written, digest.hexdigest()

    def add_plain_file(self, relative_path: str, source_path: Path) -> None:
        ext = PurePosixPath(relative_path).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            self._skip(relative_path, _unsupported_reason(ext), source_path.stat().st_size)
            return
        self._count_file()
        target = self._next_path(relative_path)
        with source_path.open("rb") as src:
            size, digest = self._copy_stream(src, target)
        self._accept(relative_path, target, size, digest)

    def add_zip(self, archive: Path, prefix: str, depth: int) -> None:
        declared = declared_zip_entries(archive)
        if declared is not None and self.entries + declared > self.entry_limit:
            self._too_many_entries()  # до разбора оглавления: оно целиком ложится в память
        try:
            zf = zipfile.ZipFile(archive)
        except (*_BROKEN_READ, ValueError) as exc:  # битое оглавление: имя, смещение, метод
            if depth == 0:
                raise UnpackError("Архив повреждён или это не ZIP") from exc
            self._skip(prefix.rstrip("/") or archive.name, "вложенный архив повреждён")
            return
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                self._count_entry()
                name = decode_zip_name(info)
                relative = safe_relative_path(name)
                shown = prefix + (relative or _CONTROL_CHARS.sub("_", name.replace("\\", "/")))
                if relative is None:
                    self._skip(shown, "небезопасный путь внутри архива")
                    continue
                parts = PurePosixPath(relative).parts
                if parts[-1].lower() in _JUNK_NAMES or any(p.lower() in _JUNK_DIRS for p in parts):
                    continue  # служебные файлы ОС — молча
                if info.flag_bits & _ENCRYPTED_FLAG:
                    self._skip(shown, "файл защищён паролем", info.file_size)
                    continue
                if (
                    info.file_size > _RATIO_MIN_BYTES
                    and info.file_size > self.limits.max_ratio * max(info.compress_size, 1)
                ):
                    raise UnpackError("Архив похож на «zip-бомбу» (подозрительно сильное сжатие)")
                ext = PurePosixPath(relative).suffix.lower()
                if ext in ARCHIVE_EXTENSIONS:
                    if depth + 1 > self.limits.max_depth:
                        self._skip(shown, "слишком глубокая вложенность архивов", info.file_size)
                        continue
                    nested = self.dest / f"_nested_{len(self.files):04d}{ext}"
                    if not self._extract(zf, info, nested, shown):
                        continue
                    self.add_archive(nested, ext, shown + "/", depth + 1)
                    nested.unlink(missing_ok=True)
                    continue
                if ext not in SUPPORTED_EXTENSIONS:
                    self._skip(shown, _unsupported_reason(ext), info.file_size)
                    continue
                self._count_file()
                target = self._next_path(shown)
                extracted = self._extract(zf, info, target, shown)
                if extracted is not None:
                    self._accept(shown, target, *extracted)

    def _extract(
        self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, shown: str
    ) -> tuple[int, str] | None:
        """Запись ZIP → файл. Не читается — пропуск с причиной (ошибка одного файла, а не
        пакета); лимиты объёма по-прежнему останавливают весь пакет."""
        try:
            src = zf.open(info)  # локальный заголовок: имя, смещение, метод сжатия
        except _BROKEN_READ:
            src = None
        if src is not None:
            try:
                with src:
                    return self._copy_stream(_Reading(src), target)
            except _BrokenReadError:
                pass
        target.unlink(missing_ok=True)
        self._skip(shown, BROKEN_ENTRY, info.file_size)
        return None

    def add_archive(self, archive: Path, ext: str, prefix: str, depth: int) -> None:
        if ext == ".zip":
            self.add_zip(archive, prefix, depth)
        else:
            self.add_libarchive(archive, prefix, depth)

    def add_libarchive(self, archive: Path, prefix: str, depth: int) -> None:
        """RAR / 7z / tar через bsdtar: проверка по заголовкам → распаковка → общие проверки."""
        shown_archive = prefix.rstrip("/") or archive.name
        if self.bsdtar is None:
            if depth == 0:
                raise UnpackError(
                    "Архивы RAR и 7z на этом сервере не распаковываются — перепакуйте в ZIP"
                )
            self._skip(shown_archive, "архивы RAR и 7z не поддерживаются — перепакуйте в ZIP")
            return
        listing = declared_listing(self.bsdtar, archive)
        if listing is None:
            if depth == 0:
                raise UnpackError("Архив повреждён или защищён паролем")
            self._skip(shown_archive, "вложенный архив повреждён или защищён паролем")
            return
        declared, entries = listing
        if self.entries + entries > self.entry_limit:
            self._too_many_entries()  # до распаковки, как у ZIP
        if self.total_bytes + declared > self.limits.max_unpacked_bytes:
            raise UnpackError(
                "Архив распаковывается в слишком большой объём. Проверьте, что это "
                "документация закупки, или разделите архив"
            )
        packed = archive.stat().st_size
        if declared > _RATIO_MIN_BYTES and declared > self.limits.max_ratio * max(packed, 1):
            raise UnpackError("Архив похож на «zip-бомбу» (подозрительно сильное сжатие)")
        workdir = self.dest / f"_x_{len(self.files):04d}_{depth}"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            complete = self._run_bsdtar(archive, workdir)
            self._add_tree(workdir, prefix, depth)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if not complete:
            self._skip(shown_archive, "часть файлов архива не распакована (пароль или повреждение)")

    def _run_bsdtar(self, archive: Path, workdir: Path) -> bool:
        assert self.bsdtar is not None
        try:
            result = run_untrusted(
                [self.bsdtar, "-xf", str(archive), "-C", str(workdir)], timeout=ARCHIVE_TIMEOUT
            )
        except subprocess.TimeoutExpired as exc:
            raise UnpackError("Архив распаковывается слишком долго") from exc
        except OSError as exc:
            raise UnpackError("Не удалось распаковать архив") from exc
        return result.returncode == 0

    def _add_tree(self, root: Path, prefix: str, depth: int) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue  # ссылки не открываем: они могут вести за пределы пакета
            self._count_entry()
            relative = _CONTROL_CHARS.sub("_", path.relative_to(root).as_posix())
            parts = PurePosixPath(relative).parts
            if parts[-1].lower() in _JUNK_NAMES or any(p.lower() in _JUNK_DIRS for p in parts):
                continue
            shown = prefix + relative
            ext = path.suffix.lower()
            if ext in ARCHIVE_EXTENSIONS:
                if depth + 1 > self.limits.max_depth:
                    self._skip(shown, "слишком глубокая вложенность архивов", path.stat().st_size)
                    continue
                self.add_archive(path, ext, shown + "/", depth + 1)
                continue
            self.add_plain_file(shown, path)


def skip_hides_content(relative_path: str, reason: str | None) -> bool:
    """Пропущенный файл мог содержать условия закупки: пароль, повреждение, RAR, незнакомый
    формат. Пустой файл, дубликат и подпись к документу — не могли."""
    if reason is None or reason == EMPTY_FILE or reason.startswith(DUPLICATE_OF):
        return False
    return Path(relative_path).suffix.lower() not in NON_CONTENT_EXTENSIONS


def _unsupported_reason(ext: str) -> str:
    if ext in UNSUPPORTED_ARCHIVES:
        return f"архивы {ext} не поддерживаются — перепакуйте в ZIP"
    return f"формат {ext or 'без расширения'} не поддерживается"


@lru_cache(maxsize=8)
def find_bsdtar(configured: str = "bsdtar") -> str | None:
    """Путь к bsdtar. В Windows подходит встроенный tar.exe — это и есть bsdtar."""
    system_tar = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "tar.exe"
    # первым в PATH может оказаться GNU tar (например, из Git для Windows) — он RAR не читает
    for candidate in (configured, "bsdtar", str(system_tar), "tar"):
        path = shutil.which(candidate)
        if path is None:
            continue
        try:
            version = subprocess.run(
                [path, "--version"], capture_output=True, timeout=10, check=False
            ).stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "bsdtar" in version or "libarchive" in version:  # GNU tar RAR и 7z не читает
            return path
    return None


def unpack_package(
    source: Path,
    original_name: str,
    dest: Path,
    limits: UnpackLimits,
    bsdtar: str | None = None,
) -> list[UnpackedFile]:
    """Распаковать загруженный файл в `dest`. Возвращает и принятые, и пропущенные файлы.

    UnpackError — пакет нельзя обработать вовсе (бомба, лимиты, битый архив).
    `bsdtar` — путь к bsdtar для RAR/7z/tar; None — такие архивы не поддерживаются.
    """
    dest.mkdir(parents=True, exist_ok=True)
    unpacker = _Unpacker(dest, limits, bsdtar)
    extension = PurePosixPath(original_name).suffix.lower()
    try:
        if extension in ARCHIVE_EXTENSIONS:
            unpacker.add_archive(source, extension, "", 0)
        else:
            unpacker.add_plain_file(PurePosixPath(original_name.replace("\\", "/")).name, source)
    except UnpackError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if not any(not f.skipped for f in unpacker.files):
        reasons = "; ".join(f"{f.relative_path}: {f.skipped_reason}" for f in unpacker.files[:5])
        raise UnpackError(
            "В пакете нет документов, которые можно проверить"
            + (f" ({reasons})" if reasons else "")
        )
    return unpacker.files
