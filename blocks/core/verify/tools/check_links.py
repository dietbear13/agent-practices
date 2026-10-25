#!/usr/bin/env python3
"""Проверка источников в артефакте: живые ли ссылки и у всего ли есть источник.

    python tools/check_links.py outputs/facts.md
    python tools/check_links.py outputs/facts.md --offline

Это первый шаг проверки: то, что можно посчитать кодом, не должна проверять
модель. Она дороже, медленнее и, в отличие от `urlopen`, иногда «видит» страницу,
которой нет.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

SRC_RE = re.compile(r"\[src:\s*(?P<url>[^\]\s]+)\s*\]")
UNSURE_RE = re.compile(r"\[\?\]")
DECISION_RE = re.compile(r"\[!\]")

# Строка, похожая на утверждение с числом: процент, цена, срок, количество.
#
# Границы слова в конце нет намеренно: после «%» и «₽» стоит пробел, а между двумя
# несловесными символами границы не возникает, и `40%` не находился.
CLAIM_RE = re.compile(
    r"\d[\d\s.,]*\s*(?:%|₽|\$|руб|год|лет|дн|мес|недел|шт|раз|человек|клиент)",
    re.IGNORECASE,
)

USER_AGENT = "agent-kit-link-check/1.0"


def check_url(url: str, timeout: float) -> dict:
    """Возвращает статус одной ссылки.

    Сетевая ошибка — не то же самое, что мёртвая ссылка: прокси, офлайн и
    закрытый корпоративный контур дают тот же обрыв. Такое помечается как
    «не проверено», а не как провал: проверка, красная по техническим причинам,
    приучает игнорировать красное.
    """
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"url": url, "status": "ok", "code": response.status}
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 405):
            # Сервер не любит HEAD или ботов — это не приговор ссылке.
            return {"url": url, "status": "unknown", "code": exc.code,
                    "detail": "сервер отказал в проверке, откройте глазами"}
        return {"url": url, "status": "fail", "code": exc.code, "detail": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"url": url, "status": "unknown", "detail": f"сеть недоступна: {exc.reason}"}
    except (ValueError, OSError) as exc:
        return {"url": url, "status": "fail", "detail": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка источников в артефакте")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--offline", action="store_true", help="не ходить в сеть")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    checks: list[dict] = []
    if not args.artifact.is_file():
        print(json.dumps({
            "ok": False, "action": "check_links",
            "checks": [{"name": "artifact_exists", "status": "fail",
                        "detail": f"файл не найден: {args.artifact}"}],
            "exit_code": 1,
        }, ensure_ascii=False, indent=2))
        return 1

    text = args.artifact.read_text(encoding="utf-8")
    lines = text.splitlines()

    urls = sorted({m.group("url") for m in SRC_RE.finditer(text)})
    unsure = sum(1 for _ in UNSURE_RE.finditer(text))
    decisions = sum(1 for _ in DECISION_RE.finditer(text))

    # Утверждения с числами, у которых в строке нет ни источника, ни маркера.
    unsourced = [
        {"line": i + 1, "text": line.strip()[:120]}
        for i, line in enumerate(lines)
        if CLAIM_RE.search(line)
        and not SRC_RE.search(line)
        and not UNSURE_RE.search(line)
        and not DECISION_RE.search(line)
    ]

    if unsourced:
        checks.append({
            "name": "claims_without_source",
            "status": "fail",
            "detail": f"утверждений с числами без источника и без маркера: {len(unsourced)}",
            "fix": "поставьте [src: URL] или пометьте [?], если источника нет",
        })
    else:
        checks.append({"name": "claims_without_source", "status": "ok",
                       "detail": "у числовых утверждений есть источник или маркер"})

    results = []
    if args.offline:
        checks.append({"name": "links_alive", "status": "info",
                       "detail": f"ссылок {len(urls)}, проверка сети отключена"})
    elif not urls:
        checks.append({"name": "links_alive", "status": "info", "detail": "ссылок нет"})
    else:
        results = [check_url(u, args.timeout) for u in urls]
        dead = [r for r in results if r["status"] == "fail"]
        unknown = [r for r in results if r["status"] == "unknown"]
        if dead:
            checks.append({
                "name": "links_alive", "status": "fail",
                "detail": "не открываются: " + ", ".join(f"{r['url']} ({r.get('detail')})" for r in dead),
                "fix": "замените источник или снимите факт",
            })
        elif unknown:
            checks.append({
                "name": "links_alive", "status": "info",
                "detail": f"проверено {len(results) - len(unknown)} из {len(results)}, "
                          f"остальные недоступны для проверки — откройте глазами",
            })
        else:
            checks.append({"name": "links_alive", "status": "ok",
                           "detail": f"все ссылки открываются: {len(results)}"})

    if decisions:
        checks.append({
            "name": "needs_human", "status": "info",
            "detail": f"пунктов с маркером [!]: {decisions} — их решает человек, не проверяющий",
        })
    if unsure:
        checks.append({
            "name": "unsure", "status": "info",
            "detail": f"пунктов с маркером [?]: {unsure} — источник не найден, за факт не выдавать",
        })

    failed = any(c["status"] == "fail" for c in checks)
    print(json.dumps({
        "ok": not failed,
        "action": "check_links",
        "checks": checks,
        "data": {"urls": results, "unsourced": unsourced[:20],
                 "counters": {"urls": len(urls), "unsure": unsure, "needs_human": decisions}},
        "next": "Дальше проверяет модель — только то, что кодом проверить нельзя.",
        "exit_code": 1 if failed else 0,
    }, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
