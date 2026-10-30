"""Распаковка: русские имена, защита от бомб и путей, пропуски с понятными причинами."""

from __future__ import annotations

import contextlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from svetofor.pipeline.unpack import (
    BROKEN_ENTRY,
    UnpackError,
    UnpackLimits,
    find_bsdtar,
    repair_filename,
    safe_relative_path,
    skip_hides_content,
    unpack_package,
)

PDF = b"%PDF-1.4 test"


class _LegacyNameInfo(zipfile.ZipInfo):
    """Запись с именем в заданной кодировке без флага UTF-8 — как делает Проводник Windows."""

    encoding = "cp866"

    def _encodeFilenameFlags(self) -> tuple[bytes, int]:  # noqa: N802 — переопределение API zipfile
        return self.filename.encode(self.encoding), self.flag_bits & ~0x800


def _set_encrypted_flag(path: Path, name: str) -> None:
    """Выставить бит шифрования (0x1) в локальном и центральном заголовках записи.

    zipfile при записи сбрасывает flag_bits, поэтому флаг ставим в байтах, как настоящий архиватор.
    """
    data = bytearray(path.read_bytes())
    encoded = name.encode()
    patched = 0
    # (сигнатура, смещение флагов, смещение длины имени, смещение имени) — по спецификации ZIP
    headers = ((b"PK\x03\x04", 6, 26, 30), (b"PK\x01\x02", 8, 28, 46))
    for signature, flag_offset, len_offset, name_offset in headers:
        pos = data.find(signature)
        while pos >= 0:
            name_len = int.from_bytes(data[pos + len_offset : pos + len_offset + 2], "little")
            if bytes(data[pos + name_offset : pos + name_offset + name_len]) == encoded:
                data[pos + flag_offset] |= 0x1
                patched += 1
            pos = data.find(signature, pos + 4)
    assert patched == 2, "не нашли оба заголовка записи"
    path.write_bytes(bytes(data))


def make_zip(
    path: Path,
    entries: list[tuple[str, bytes]],
    *,
    legacy: str | None = None,
    encrypted: set[str] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in entries:
            if legacy:
                info: zipfile.ZipInfo = _LegacyNameInfo(name)
                info.encoding = legacy  # type: ignore[attr-defined]
            else:
                info = zipfile.ZipInfo(name)
            info.compress_type = compression
            zf.writestr(info, data)
    for name in encrypted or ():
        _set_encrypted_flag(path, name)
    return path


def zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def unpack(tmp_path: Path, archive: Path, name: str = "package.zip", **limits: int):
    return unpack_package(archive, name, tmp_path / "out", UnpackLimits(**limits))


def accepted(files) -> list[str]:
    return [f.relative_path for f in files if not f.skipped]


def skipped(files) -> dict[str, str]:
    return {f.relative_path: f.skipped_reason for f in files if f.skipped}


@pytest.mark.parametrize("encoding", ["cp866", "cp1251"])
def test_russian_names_without_utf8_flag(tmp_path: Path, encoding: str) -> None:
    archive = make_zip(
        tmp_path / "a.zip",
        [("Проект контракта.pdf", PDF), ("ТЗ/Описание объекта.pdf", PDF + b"2")],
        legacy=encoding,
    )
    files = unpack(tmp_path, archive)
    assert accepted(files) == ["Проект контракта.pdf", "ТЗ/Описание объекта.pdf"]


def test_utf8_names(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", [("Извещение.pdf", PDF)])
    assert accepted(unpack(tmp_path, archive)) == ["Извещение.pdf"]


def test_files_written_with_hash_and_size(tmp_path: Path) -> None:
    files = unpack(tmp_path, make_zip(tmp_path / "a.zip", [("a.pdf", PDF)]))
    f = files[0]
    assert f.path is not None and f.path.read_bytes() == PDF
    assert f.size == len(PDF) and f.sha256 is not None and len(f.sha256) == 64


def test_unsafe_paths_skipped(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "a.zip", [("../evil.pdf", PDF), ("/abs.pdf", PDF + b"1"), ("ok.pdf", PDF + b"2")]
    )
    files = unpack(tmp_path, archive)
    assert accepted(files) == ["ok.pdf"]
    assert all(r == "небезопасный путь внутри архива" for r in skipped(files).values())
    assert not (tmp_path / "evil.pdf").exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a/b.pdf", "a/b.pdf"),
        ("a\\b.pdf", "a/b.pdf"),
        ("./a.pdf", "a.pdf"),
        ("../a.pdf", None),
        ("a/../../b.pdf", None),
        ("C:/x.pdf", None),
        ("/x.pdf", None),
        ("", None),
    ],
)
def test_safe_relative_path(name: str, expected: str | None) -> None:
    assert safe_relative_path(name) == expected


def test_junk_and_unsupported(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "a.zip",
        [
            ("__MACOSX/._a.pdf", b"x"),
            ("Thumbs.db", b"x"),
            ("doc.pdf", PDF),
            ("readme.exe", b"MZ"),
            ("old.rar", b"Rar!"),
        ],
    )
    files = unpack(tmp_path, archive)
    assert accepted(files) == ["doc.pdf"]
    reasons = skipped(files)
    assert reasons["readme.exe"] == "формат .exe не поддерживается"
    assert "перепакуйте в ZIP" in reasons["old.rar"]
    assert "Thumbs.db" not in reasons and "__MACOSX/._a.pdf" not in reasons


def test_encrypted_skipped(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "a.zip",
        [("secret.pdf", PDF), ("open.pdf", PDF + b"1")],
        encrypted={"secret.pdf"},
        compression=zipfile.ZIP_STORED,
    )
    files = unpack(tmp_path, archive)
    assert skipped(files) == {"secret.pdf": "файл защищён паролем"}


def test_empty_file_skipped(tmp_path: Path) -> None:
    files = unpack(tmp_path, make_zip(tmp_path / "a.zip", [("empty.docx", b""), ("a.pdf", PDF)]))
    assert accepted(files) == ["a.pdf"]
    assert skipped(files) == {"empty.docx": "пустой файл"}
    assert len(list((tmp_path / "out").iterdir())) == 1  # пустой файл не остался на диске


def test_duplicates_skipped(tmp_path: Path) -> None:
    files = unpack(tmp_path, make_zip(tmp_path / "a.zip", [("a.pdf", PDF), ("copy/a.pdf", PDF)]))
    assert accepted(files) == ["a.pdf"]
    assert skipped(files) == {"copy/a.pdf": "дубликат файла «a.pdf»"}


def test_nested_zip(tmp_path: Path) -> None:
    inner = zip_bytes([("Контракт.pdf", PDF)])
    files = unpack(
        tmp_path,
        make_zip(tmp_path / "a.zip", [("Документация.zip", inner), ("Извещение.pdf", PDF + b"1")]),
    )
    assert sorted(accepted(files)) == ["Документация.zip/Контракт.pdf", "Извещение.pdf"]


def test_nested_too_deep(tmp_path: Path) -> None:
    level2 = zip_bytes([("deep.pdf", PDF)])
    level1 = zip_bytes([("l2.zip", level2)])
    files = unpack(
        tmp_path,
        make_zip(tmp_path / "a.zip", [("l1.zip", level1), ("top.pdf", PDF + b"1")]),
        max_depth=1,
    )
    assert accepted(files) == ["top.pdf"]
    assert skipped(files) == {"l1.zip/l2.zip": "слишком глубокая вложенность архивов"}


def test_zip_bomb_by_ratio(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "bomb.zip", [("big.pdf", b"\0" * (5 * 1024 * 1024))])
    with pytest.raises(UnpackError, match="zip-бомбу"):
        unpack(tmp_path, archive, max_ratio=100)
    assert not (tmp_path / "out").exists()  # частично распакованное удалено


def test_total_size_limit_counts_real_bytes(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "a.zip",
        [("a.pdf", b"a" * 3000), ("b.pdf", b"b" * 3000)],
        compression=zipfile.ZIP_STORED,
    )
    with pytest.raises(UnpackError, match="слишком большой объём"):
        unpack(tmp_path, archive, max_unpacked_bytes=5000)


def test_file_count_limit(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", [(f"{i}.pdf", PDF + bytes([i])) for i in range(4)])
    with pytest.raises(UnpackError, match="больше 3 файлов"):
        unpack(tmp_path, archive, max_files=3)


def _patch_entry(
    path: Path, name: str, *, method: int | None = None, flip_data: bool = False
) -> None:
    """Испортить одну запись в байтах архива: метод сжатия в обоих заголовках или байт данных."""
    data = bytearray(path.read_bytes())
    encoded = name.encode()
    # (сигнатура, смещение метода, смещение длины имени, смещение имени)
    for signature, method_offset, len_offset, name_offset in (
        (b"PK\x03\x04", 8, 26, 30),
        (b"PK\x01\x02", 10, 28, 46),
    ):
        pos = data.find(signature)
        while pos >= 0:
            name_len = int.from_bytes(data[pos + len_offset : pos + len_offset + 2], "little")
            if bytes(data[pos + name_offset : pos + name_offset + name_len]) == encoded:
                if method is not None:
                    data[pos + method_offset : pos + method_offset + 2] = method.to_bytes(
                        2, "little"
                    )
                if flip_data and signature == b"PK\x03\x04":
                    data[pos + name_offset + name_len + 3] ^= 0xFF
            pos = data.find(signature, pos + 4)
    path.write_bytes(bytes(data))


@pytest.mark.parametrize(
    ("compression", "damage"),
    [
        (zipfile.ZIP_STORED, {"flip_data": True}),  # неверная контрольная сумма
        (zipfile.ZIP_DEFLATED, {"flip_data": True}),  # испорченный поток сжатия
        (zipfile.ZIP_STORED, {"method": 9}),  # Deflate64 из Проводника Windows
        (zipfile.ZIP_STORED, {"method": 98}),  # PPMd из 7-Zip
    ],
)
def test_broken_entry_skips_only_that_file(
    tmp_path: Path, compression: int, damage: dict[str, Any]
) -> None:
    archive = make_zip(
        tmp_path / "a.zip",
        [("Проект контракта.pdf", PDF + b"1" * 64), ("ТЗ.pdf", PDF + b"2" * 64)],
        compression=compression,
    )
    _patch_entry(archive, "ТЗ.pdf", **damage)
    files = unpack(tmp_path, archive)
    assert accepted(files) == ["Проект контракта.pdf"]
    assert skipped(files) == {"ТЗ.pdf": BROKEN_ENTRY}
    assert skip_hides_content("ТЗ.pdf", BROKEN_ENTRY)  # пакет с таким пропуском не «зелёный»


def test_too_many_entries_rejected_before_reading_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "a.zip", [(f"{i}.exe", b"x") for i in range(31)])
    opened: list[Path] = []
    monkeypatch.setattr(zipfile.ZipFile, "__init__", lambda self, *a, **k: opened.append(a[0]))
    with pytest.raises(UnpackError, match="больше 30 записей"):
        unpack(tmp_path, archive, max_files=3)
    assert opened == []  # оглавление на миллион записей не ложится в память


def test_many_skipped_entries_count_toward_limit(tmp_path: Path) -> None:
    """Пустые и чужие записи тоже считаются: иначе архив из них занимал воркер часами."""
    inner = zip_bytes([(f"e{i}.pdf", b"") for i in range(20)])
    archive = make_zip(tmp_path / "a.zip", [("real.pdf", PDF), ("inner.zip", inner)])
    assert accepted(unpack(tmp_path, archive, max_files=3)) == ["real.pdf"]  # 22 записи ≤ 30
    with pytest.raises(UnpackError, match="записей"):
        unpack(tmp_path, archive, max_files=2)  # 22 > 20


def test_control_characters_in_names_replaced(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", [("Проект\x01контракта\x1b.pdf", PDF)])
    assert accepted(unpack(tmp_path, archive)) == ["Проект_контракта_.pdf"]


def test_broken_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(UnpackError, match="повреждён"):
        unpack(tmp_path, bad)


def test_nothing_to_check(tmp_path: Path) -> None:
    with pytest.raises(UnpackError, match="нет документов"):
        unpack(tmp_path, make_zip(tmp_path / "a.zip", [("a.exe", b"MZ")]))


def test_single_pdf_upload(tmp_path: Path) -> None:
    src = tmp_path / "upload.bin"
    src.write_bytes(PDF)
    files = unpack(tmp_path, src, name="C:\\Users\\x\\Проект контракта.pdf")
    assert accepted(files) == ["Проект контракта.pdf"]


def test_single_unsupported_upload(tmp_path: Path) -> None:
    src = tmp_path / "upload.bin"
    src.write_bytes(b"x")
    with pytest.raises(UnpackError, match="нет документов"):
        unpack(tmp_path, src, name="file.exe")


# ─── RAR / 7z / tar через bsdtar (libarchive) ───

BSDTAR = find_bsdtar()
needs_bsdtar = pytest.mark.skipif(BSDTAR is None, reason="нет bsdtar (libarchive)")


def make_7z(path: Path, files: dict[str, bytes]) -> Path:
    import subprocess

    src = path.parent / f"{path.stem}_src"
    for name, data in files.items():
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert BSDTAR is not None
    subprocess.run(
        [BSDTAR, "--format", "7zip", "-cf", str(path), "-C", str(src), *sorted(files)],
        check=True,
        capture_output=True,
    )
    return path


@needs_bsdtar
def test_7z_with_russian_names_and_nested_zip(tmp_path: Path) -> None:
    nested = zip_bytes([("Описание объекта.pdf", PDF + b"spec")])
    archive = make_7z(
        tmp_path / "пакет.7z",
        {"Проект контракта.pdf": PDF, "Приложения/ТЗ.zip": nested, "Thumbs.db": b"x"},
    )
    files = unpack_package(archive, "пакет.7z", tmp_path / "out", UnpackLimits(), BSDTAR)
    assert accepted(files) == [
        "Приложения/ТЗ.zip/Описание объекта.pdf",
        "Проект контракта.pdf",
    ]
    assert not list((tmp_path / "out").glob("_x_*"))  # временная распаковка убрана


@needs_bsdtar
def test_7z_bomb_rejected_by_headers(tmp_path: Path) -> None:
    archive = make_7z(tmp_path / "bomb.7z", {"zeros.pdf": b"\0" * (5 * 1024 * 1024)})
    with pytest.raises(UnpackError, match="zip-бомбу"):
        unpack_package(archive, "bomb.7z", tmp_path / "out", UnpackLimits(), BSDTAR)
    small = UnpackLimits(max_unpacked_bytes=1024, max_ratio=10**9)
    with pytest.raises(UnpackError, match="слишком большой объём"):
        unpack_package(archive, "bomb.7z", tmp_path / "out2", small, BSDTAR)


@needs_bsdtar
def test_broken_rar_is_clear_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.rar"
    broken.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 64)
    with pytest.raises(UnpackError, match="повреждён или защищён паролем"):
        unpack_package(broken, "broken.rar", tmp_path / "out", UnpackLimits(), BSDTAR)


def test_rar_without_bsdtar_asks_to_repack(tmp_path: Path) -> None:
    src = tmp_path / "a.rar"
    src.write_bytes(b"Rar!")
    with pytest.raises(UnpackError, match="перепакуйте в ZIP"):
        unpack_package(src, "a.rar", tmp_path / "out", UnpackLimits(), None)


@pytest.mark.parametrize(
    ("received", "expected"),
    [
        ("Ïðîåêò êîíòðàêòà.pdf", "Проект контракта.pdf"),  # cp1251, прочитано как latin-1
        ("Проект.pdf".encode().decode("latin-1"), "Проект.pdf"),  # UTF-8, прочитано как latin-1
        ("Проект контракта.pdf", "Проект контракта.pdf"),
        ("Résumé.pdf", "Résumé.pdf"),  # настоящая латиница с диакритикой не трогается
        ("contract.pdf", "contract.pdf"),
    ],
)
def test_upload_filename_in_wrong_encoding_is_repaired(received: str, expected: str) -> None:
    assert repair_filename(received) == expected


def test_fuzzed_archives_fail_only_with_clear_reason(tmp_path: Path) -> None:
    """Перебор испорченных архивов: до исправления треть из 3000 падала
    UnicodeDecodeError, OSError, NotImplementedError — «внутренняя ошибка» вместо причины.
    Исход любого архива — распакован (битые файлы пропущены с причиной) или UnpackError."""
    import random
    import shutil

    inner = zip_bytes([("Извещение.pdf", PDF * 40)])
    archive = make_zip(
        tmp_path / "base.zip",
        [("Проект контракта.pdf", PDF + bytes(range(256)) * 8), ("Вложенный.zip", inner)],
    )
    data = archive.read_bytes()
    rng = random.Random(20261008)  # noqa: S311 — воспроизводимый перебор, не криптография
    for i in range(400):
        mutated = bytearray(data)
        for _ in range(rng.choice((1, 2, 4, 8, 32))):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        if rng.random() < 0.1:
            mutated = mutated[: rng.randrange(len(mutated))]
        source = tmp_path / f"{i}.zip"
        source.write_bytes(bytes(mutated))
        with contextlib.suppress(UnpackError):
            unpack_package(source, "p.zip", tmp_path / f"out{i}", UnpackLimits())
        shutil.rmtree(tmp_path / f"out{i}", ignore_errors=True)


def test_libarchive_entry_limit_checked_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Код-ревью 2026-10-08: у RAR/7z лимит записей проверялся только после распаковки —
    архив из миллиона пустых файлов распаковывался бы целиком."""
    from svetofor.pipeline import unpack as unpack_module

    monkeypatch.setattr(unpack_module, "declared_listing", lambda *_: (1000, 50_000))

    def must_not_extract(*_: object, **__: object) -> bool:
        raise AssertionError("распаковка не должна начаться")

    monkeypatch.setattr(unpack_module._Unpacker, "_run_bsdtar", must_not_extract)
    archive = tmp_path / "many.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
    with pytest.raises(UnpackError, match="записей"):
        unpack_package(archive, "many.7z", tmp_path / "out", UnpackLimits(), bsdtar="bsdtar")
