"""runtime.py — каркас для скриптов регулярных задач.

Ставится в `tools/` рабочего пространства. Только стандартная библиотека: файл
живёт в проекте пользователя отдельно от конструктора и не имеет права зависеть
от чего-либо, что нужно устанавливать.

Четыре вещи, которые он держит кодом, а не просьбой в промпте:

- **Стоп-кран.** `Budget` останавливает прогон по числу шагов, времени или
  деньгам и зовёт человека, вместо того чтобы полчаса перебирать варианты.
- **Дедупликация.** `Checkpoint` помнит обработанные идентификаторы, поэтому
  перезапуск после обрыва не плодит дубли и не платит второй раз.
- **Только свежие данные.** `QualityMetadata` помечает выгрузку как `degraded`
  при нуле строк, подозрении на обрезку пагинации или семплировании — и это
  печатается раньше, чем человек увидит результат.
- **Ретраи, которые не выжигают квоту.** `classify_error` не повторяет `4xx`,
  а `429` повторяет только по присланному `Retry-After`.

Ключи читает только `read_secret`. Она единственная возвращает значение наружу
и сама его нигде не печатает и не логирует; вызывающий код обязан соблюдать то
же правило — секрет уходит в заголовок запроса и никуда больше.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

# До любого вывода: на Windows консоль не UTF-8, и русский текст в print()
# превращается в мусор или роняет UnicodeEncodeError при перенаправлении.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

# ---------------------------------------------------------------------------
# Атомарная запись — скопирована буквально из library/tools/render_lib.py сборщика,
# не импортирована. Тот же приём: временный файл рядом с финальным
# именем плюс короткий os.replace().
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def _atomic_write_json(path: Path, data: object) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    _atomic_write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Классификация ошибок и ретраи
# ---------------------------------------------------------------------------


class RetryDecision(Enum):
    NO_RETRY = "no_retry"
    RETRY_AFTER = "retry_after"
    BACKOFF = "backoff"


def classify_error(
    status_code: int | None,
    retry_after: float | None,
    is_timeout: bool,
) -> RetryDecision:
    """Классифицирует уже случившийся ответ/исключение. Сама в сеть не ходит — тело задачи
    делает HTTP-запрос своей библиотекой, эта функция только решает, что делать дальше.

    Правила:
    - таймаут или `5xx` -> `BACKOFF` (экспоненциальный backoff с jitter);
    - `408` -> `BACKOFF` (таймаут запроса на стороне сервера — семантически ближе к 5xx);
    - `429`/`420` -> `RETRY_AFTER`, если сервис прислал `Retry-After`, иначе `NO_RETRY` —
      не гадаем интервал вслепую, это может выжечь суточную квоту за минуту;
    - любой другой `4xx` -> `NO_RETRY` (ошибка запроса, повторять бессмысленно);
    - `status_code is None` и не таймаут -> `NO_RETRY` (неизвестная ошибка — не ретраим вслепую).
    """
    if is_timeout:
        return RetryDecision.BACKOFF
    if status_code is None:
        return RetryDecision.NO_RETRY
    if status_code == 408:
        return RetryDecision.BACKOFF
    if 500 <= status_code <= 599:
        return RetryDecision.BACKOFF
    if status_code in (429, 420):
        return RetryDecision.RETRY_AFTER if retry_after is not None else RetryDecision.NO_RETRY
    return RetryDecision.NO_RETRY


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Экспоненциальный backoff с полным jitter: `random.uniform(0, min(cap, base * 2**attempt))`.

    Детерминированность здесь не требуется — это runtime-код, исполняемый в рабочем
    пространстве пользователя: разброс задержки здесь и нужен.
    """
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling)


# ---------------------------------------------------------------------------
# Бюджет прогона
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Бросается `Budget.check` при первом превышении лимита шагов, времени или стоимости.

    Верхнеуровневый обработчик в тело задачи ловит это исключение и печатает три строки по
    трём строкам: что делали, что не так, что сделать.
    """


@dataclass
class Budget:
    max_steps: int | None = None
    max_seconds: float | None = None
    max_cost: float | None = None

    def check(
        self,
        steps_done: int,
        seconds_elapsed: float,
        cost_so_far: float | None = None,
    ) -> None:
        """Бросает `BudgetExceeded` с человекочитаемой причиной при первом превышении любого
        из трёх лимитов. Лимит, равный `None`, не проверяется (без ограничения)."""
        if self.max_steps is not None and steps_done >= self.max_steps:
            raise BudgetExceeded(f"превышен лимит шагов: {steps_done} из {self.max_steps}")
        if self.max_seconds is not None and seconds_elapsed >= self.max_seconds:
            raise BudgetExceeded(
                f"превышен лимит времени: {seconds_elapsed:.0f}с из {self.max_seconds:.0f}с"
            )
        if (
            self.max_cost is not None
            and cost_so_far is not None
            and cost_so_far >= self.max_cost
        ):
            raise BudgetExceeded(
                f"превышен лимит стоимости: {cost_so_far:.2f} из {self.max_cost:.2f}"
            )


# ---------------------------------------------------------------------------
# Чекпоинт, единая папка выгрузки, manifest.json прогона, лог вызова
#
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """Список уже обработанных ID — `<output_dir>/.checkpoint.json`.

    Позволяет прервать длинный прогон и продолжить с того места, где остановились, не
    обрабатывая уже сделанное повторно и не платя за это второй раз."""

    done_ids: set[str] = field(default_factory=set)
    updated_at: str | None = None

    @classmethod
    def load(cls, path: Path) -> "Checkpoint":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            done_ids=set(data.get("done_ids", [])),
            updated_at=data.get("updated_at"),
        )

    def mark_done(self, item_id: str) -> None:
        self.done_ids.add(item_id)

    def is_done(self, item_id: str) -> bool:
        return item_id in self.done_ids

    def save(self, path: Path) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        data = {"done_ids": sorted(self.done_ids), "updated_at": self.updated_at}
        _atomic_write_json(path, data)


def output_dir(base: Path, name: str) -> Path:
    """`{name}-{datetime}` — единая папка сохранения.

    Формат времени сортируемый и однозначный: `%Y%m%dT%H%M%SZ`, всегда UTC
    (`datetime.now(timezone.utc)`, никогда naive-время). Вызывается один раз за прогон;
    создаёт директорию и возвращает путь до неё.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = base / f"{name}-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_manifest(output_dir: Path, entry: dict) -> None:
    """Дописывает `entry` в список `output_dir/manifest.json` (файлы-результаты прогона —
    путь, размер, sha256 и т.п., формируемые вызывающим кодом). Читает существующий список,
    добавляет запись, перезаписывает атомарно. Используется чистильщиком — здесь только запись, не удаление."""
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        existing = []
    existing.append(entry)
    _atomic_write_json(manifest_path, existing)


def record_result(
    output_dir: Path,
    name: str,
    rows: list[dict],
    fieldnames: list[str],
    meta: "QualityMetadata",
) -> Path:
    """Единая точка записи результата: файл + запись в `manifest.json` + метаданные рядом.

    Существует потому, что чистильщик (`cleanup.py`) удаляет всё, чего нет в
    `manifest.json`. Пока регистрация результата — отдельный шаг, который тело задачи
    может забыть, чистильщик опасен: забытый `append_manifest` означает удалённую
    выгрузку. Здесь забыть нечего — запись и регистрация неразделимы.
    """
    path = output_dir / name
    write_csv(path, rows, fieldnames)
    data = path.read_bytes()
    append_manifest(
        output_dir,
        {
            "path": name,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "rows": meta.rows,
            "degraded": meta.degraded,
        },
    )
    write_quality_metadata(output_dir, name, meta)

    # Статус печатается здесь, а не оставляется на прочтение `*.quality.json`: правило
    # «метаданные качества читаются до показа результата» до сих пор держалось только
    # текстом скилла, а текст переживает не всякую сессию. Сюда же приходит любой скрипт
    # каркаса — обойти эту строку можно только обойдя запись результата целиком.
    if meta.degraded:
        print(f"ВНИМАНИЕ: {name} — выгрузка degraded: {meta.degraded_reason}")
    else:
        print(f"ОК: {name} — строк {meta.rows}")
    return path


def manifest_entries(output_dir: Path) -> list[dict]:
    """Читает `manifest.json` прогона. Пустой список, если файла нет."""
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def call_log(
    output_dir: Path,
    *,
    requests_count: int,
    retries: int,
    duration_s: float,
    cost: float | None,
) -> None:
    """Пишет `output_dir/call_log.json` — сколько запросов сделано, сколько было ретраев,
    сколько времени заняло и во сколько это обошлось (если применимо). Время — ISO с явной
    таймзоной UTC. Ретраи логируются отдельно от данных: много ретраев — сигнал, что что-то
    не так, даже если данные в итоге собрались ."""
    data = {
        "requests_count": requests_count,
        "retries": retries,
        "duration_s": duration_s,
        "cost": cost,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(output_dir / "call_log.json", data)


# ---------------------------------------------------------------------------
# Метаданные качества и degraded
# ---------------------------------------------------------------------------


@dataclass
class QualityMetadata:
    period_start: str
    period_end: str
    rows: int
    sampled: bool
    sample_share: float | None
    complete: bool
    requests_count: int
    retries: int
    fetched_at: str
    degraded: bool
    degraded_reason: str | None


def build_quality_metadata(
    *,
    rows: int,
    sampled: bool = False,
    sample_share: float | None = None,
    complete: bool = True,
    requests_count: int,
    retries: int,
    period_start: str,
    period_end: str,
    page_limit: int | None = None,
) -> QualityMetadata:
    """Вычисляет `degraded` по разобранным граблям. Первое совпавшее условие определяет
    `degraded_reason` (порядок важен и фиксирован):

    1. `rows == 0` — ноль строк почти никогда не валидный ответ для регулярной задачи;
    2. `page_limit is not None and rows == page_limit` — число строк равно лимиту страницы,
       почти всегда означает обрезку пагинации, а не совпадение;
    3. `sampled is True` — включено семплирование (например, Метрика при >1 000 000 визитов).

    Ни одно условие не совпало -> `degraded=False`, `degraded_reason=None`.
    """
    degraded = False
    degraded_reason: str | None = None

    if rows == 0:
        degraded = True
        degraded_reason = "ноль строк в ответе"
    elif page_limit is not None and rows == page_limit:
        degraded = True
        degraded_reason = (
            "число строк равно лимиту страницы — подозрение на обрезку пагинации"
        )
    elif sampled is True:
        degraded = True
        degraded_reason = f"включено семплирование, sample_share={sample_share}"

    return QualityMetadata(
        period_start=period_start,
        period_end=period_end,
        rows=rows,
        sampled=sampled,
        sample_share=sample_share,
        complete=complete,
        requests_count=requests_count,
        retries=retries,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


def write_quality_metadata(output_dir: Path, name: str, meta: QualityMetadata) -> None:
    """Пишет `output_dir/{name}.quality.json` рядом с файлом результата `name`."""
    _atomic_write_json(output_dir / f"{name}.quality.json", asdict(meta))


# ---------------------------------------------------------------------------
# CSV и секреты
# ---------------------------------------------------------------------------


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Всегда `encoding="utf-8-sig"` (Excel на русской Windows не понимает CSV без BOM) и `newline=""` (требование модуля `csv`). Атомарно: временный файл + `os.replace`.
    Никакого прямого `csv.writer` в теле задачи — только через эту функцию."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def read_secret(name: str, *, env_path: Path = Path(".env")) -> str:
    """Простой построчный парсер `.env` (`KEY=VALUE`, пропуск пустых строк и
    `#`-комментариев, срез UTF-8 BOM если есть) — без внешней зависимости `python-dotenv`,
    чтобы не поднимать потолок 8 пакетов рабочего пространства раньше времени.

    Единственная функция в этом модуле, которая возвращает значение секрета наружу — сама
    она нигде не печатает и не логирует прочитанное. Вызывающий код (тело задачи) обязан
    соблюдать тот же инвариант: секрет попадает только в HTTP-заголовок/параметр запроса,
    никогда в `print`, `logging`, `manifest.json` или `call_log.json`.
    """
    if not env_path.exists():
        raise RuntimeError(
            f"файл {env_path} не найден — скопируйте .env.example в .env и заполните ключи"
        )

    raw = env_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8")

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()

    if name not in values:
        raise RuntimeError(
            f"ключ {name} не найден в .env — добавьте строку {name}=... (см. .env.example)"
        )
    return values[name]
