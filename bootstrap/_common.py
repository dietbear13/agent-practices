"""Общие примитивы всех скриптов bootstrap. Не CLI — только импортируется.

Здесь собрано то, что иначе пришлось бы повторять (и однажды забыть) в каждом
скрипте: настройка кодировок, атомарная запись, хеш, устойчивый к переводам
строк, подстановка плейсхолдеров и единый формат ответа.

Только стандартная библиотека. Целевая версия — Python 3.9.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# Кодировки. Вызывать ПЕРВЫМ делом, до любого вывода.
#
# На Windows консоль по умолчанию не UTF-8, и обычный print() с кириллицей
# либо падает с UnicodeEncodeError, либо печатает "?????". Это первое, обо что
# спотыкается человек на своей машине, поэтому лечится централизованно.
# --------------------------------------------------------------------------


def setup_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


# --------------------------------------------------------------------------
# Коды выхода. Двойка — не ошибка, а остановка по правилу: скрипт сделал всё,
# что мог без человека, и ждёт решения.
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USER = 1
EXIT_CONFIRM = 2
EXIT_INTERNAL = 3

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_INFO = "info"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def as_dict(self) -> Dict[str, str]:
        out = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.fix:
            out["fix"] = self.fix
        return out


@dataclass
class Result:
    """Единый контракт вывода. stdout — всегда валидный JSON и ничего кроме.

    Скилл, который вызывает скрипт, объясняет результат человеку словами;
    сырой JSON пользователю не показывается никогда.
    """

    action: str
    checks: List[Check] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    next: str = ""

    def add(self, name: str, status: str, detail: str, fix: str = "") -> Check:
        check = Check(name=name, status=status, detail=detail, fix=fix)
        self.checks.append(check)
        return check

    def ok(self, name: str, detail: str) -> Check:
        return self.add(name, STATUS_OK, detail)

    def fail(self, name: str, detail: str, fix: str = "") -> Check:
        return self.add(name, STATUS_FAIL, detail, fix)

    def warn(self, name: str, detail: str, fix: str = "") -> Check:
        return self.add(name, STATUS_WARN, detail, fix)

    def info(self, name: str, detail: str) -> Check:
        return self.add(name, STATUS_INFO, detail)

    @property
    def has_failures(self) -> bool:
        return any(c.status == STATUS_FAIL for c in self.checks)

    def exit_code(self, override: Optional[int] = None) -> int:
        if override is not None:
            return override
        return EXIT_USER if self.has_failures else EXIT_OK

    def emit(self, exit_code: Optional[int] = None) -> int:
        code = self.exit_code(exit_code)
        payload = {
            "ok": code == EXIT_OK,
            "action": self.action,
            "checks": [c.as_dict() for c in self.checks],
            "data": self.data,
            "next": self.next,
            "exit_code": code,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return code


def die(action: str, detail: str, fix: str = "", code: int = EXIT_USER) -> int:
    """Ошибка, понятная человеку, вместо traceback."""
    result = Result(action=action)
    result.fail("error", detail, fix)
    return result.emit(code)


# --------------------------------------------------------------------------
# Файлы. Ни одного open() без явного encoding.
# --------------------------------------------------------------------------


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_text_env(path: Path) -> str:
    """Для .env и всего, что человек мог сохранить Блокнотом.

    utf-8-sig снимает BOM. Без этого первая переменная файла читается вместе с
    невидимым префиксом и молча не находится, а человек уверен, что ключ вписал.
    """
    return path.read_text(encoding="utf-8-sig")


def write_text_atomic(path: Path, text: str, newline: str = "\n") -> None:
    """Пишет через временный файл рядом и os.replace.

    Прерванная запись не оставляет полуфайла на месте настоящего.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
        os.replace(str(tmp), str(path))
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------
# Хеш, устойчивый к переводам строк.
#
# git с core.autocrlf=true на Windows перепишет LF в CRLF при checkout. Хеш
# сырых байтов тогда не совпадёт с посчитанным на macOS, и система решит, что
# человек правил файл руками, хотя он его не открывал. Поэтому текст перед
# хешированием нормализуется, а двоичные файлы хешируются как есть.
# --------------------------------------------------------------------------


def normalize_newlines(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8000]


def sha256_bytes(data: bytes) -> str:
    if not looks_binary(data):
        data = normalize_newlines(data)
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


# --------------------------------------------------------------------------
# Плейсхолдеры вида <<<ИМЯ>>>.
#
# Формат выбран так, чтобы его нельзя было спутать ни с синтаксисом Python, ни
# с Jinja, ни с чем-то, что встречается в обычном тексте, и чтобы незаменённый
# плейсхолдер бросался в глаза при чтении файла.
# --------------------------------------------------------------------------

PLACEHOLDER_OPEN = "<<<"
PLACEHOLDER_CLOSE = ">>>"


def find_placeholders(text: str) -> Set[str]:
    found: Set[str] = set()
    start = 0
    while True:
        left = text.find(PLACEHOLDER_OPEN, start)
        if left < 0:
            return found
        right = text.find(PLACEHOLDER_CLOSE, left + len(PLACEHOLDER_OPEN))
        if right < 0:
            return found
        name = text[left + len(PLACEHOLDER_OPEN) : right]
        if name and "\n" not in name:
            found.add(name)
        start = right + len(PLACEHOLDER_CLOSE)


def substitute(text: str, values: Dict[str, str]) -> Tuple[str, Set[str]]:
    """Подставляет значения. Возвращает текст и множество незакрытых имён.

    Незакрытый плейсхолдер не заменяется на пустую строку намеренно: пустое
    место в файле выглядит как осознанный выбор автора, а видимый маркер — как
    незаконченная работа, и его находит проверка.
    """
    out = text
    for name, value in values.items():
        out = out.replace(PLACEHOLDER_OPEN + name + PLACEHOLDER_CLOSE, value)
    return out, find_placeholders(out)


# --------------------------------------------------------------------------
# Пути
# --------------------------------------------------------------------------


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def rel_posix(path: Path, base: Path) -> str:
    """Относительный путь всегда через '/' — чтобы JSON читался одинаково везде."""
    return path.resolve().relative_to(base.resolve()).as_posix()


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def iter_files(root: Path, skip_dirs: Iterable[str] = ()) -> Iterable[Path]:
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            yield Path(dirpath) / name
