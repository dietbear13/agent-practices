#!/usr/bin/env python3
"""Аудит чужого MCP-сервера до того, как вы его себе поставите.

    python tools/mcp_audit.py --server "npx -y some-mcp-server" --i-trust-this-server
    python tools/mcp_audit.py --server "python server.py" --tasks TASKS.md --i-trust-this-server
    python tools/mcp_audit.py --from-json tools_list.json

Отвечает на три вопроса, которые обычно выясняются уже после установки:

1. **Во что он обходится постоянно.** Определения инструментов попадают в контекст
   при каждом запросе, а не при вызове. Пятнадцать ручек ради двух нужных вы
   оплачиваете в каждом сообщении.
2. **Покрывает ли он вашу задачу.** Бывает, что нет нужного отчёта или нужной
   атрибуции — и выясняется это на результате, а не на установке.
3. **Что он умеет ломать.** Инструменты, которые пишут, удаляют и меняют. Чужой
   сервер подключается только на чтение.

Скрипт запускает сервер и делает два вызова протокола: `initialize` и `tools/list`.
Ни один инструмент не вызывается. Но запуск чужого кода — всё равно запуск чужого
кода, поэтому нужен явный флаг `--i-trust-this-server`.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

PROTOCOL_VERSION = "2024-11-05"

# Слова, по которым инструмент опознаётся как меняющий состояние.
WRITE_HINTS = (
    "create", "update", "delete", "remove", "write", "set", "post", "put",
    "patch", "send", "publish", "upload", "insert", "modify", "execute", "run",
)


def estimate_tokens(text: str) -> int:
    """Грубая оценка: примерно четыре символа на токен.

    Оценка намеренно грубая и помечена как оценка. Точное число зависит от
    токенизатора модели, но для решения «пятнадцать ручек или две» хватает
    порядка величины.
    """
    return max(1, round(len(text) / 4))


def rpc(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def fetch_tools(command: str, timeout: float) -> list[dict]:
    """Поднимает сервер по stdio и забирает список инструментов."""
    args = shlex.split(command, posix=False) if sys.platform == "win32" else shlex.split(command)
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-kit-audit", "version": "1.0"},
            },
        }
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(rpc(init))
        process.stdin.write(rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        process.stdin.write(rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        process.stdin.flush()

        tools: list[dict] = []
        for _ in range(200):
            line = process.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # сервер что-то пишет в stdout мимо протокола
            if message.get("id") == 2:
                tools = message.get("result", {}).get("tools", [])
                break
        return tools
    finally:
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            process.kill()


def analyse(tools: list[dict], task_words: set[str]) -> dict:
    rows = []
    total_chars = 0
    for tool in tools:
        name = tool.get("name", "")
        description = tool.get("description", "") or ""
        schema = json.dumps(tool.get("inputSchema", {}), ensure_ascii=False)
        chars = len(name) + len(description) + len(schema)
        total_chars += chars

        lowered = (name + " " + description).lower()
        writes = any(hint in lowered for hint in WRITE_HINTS)
        relevant = bool(task_words & set(lowered.replace("_", " ").split())) if task_words else None

        rows.append({
            "name": name,
            "chars": chars,
            "tokens_estimate": estimate_tokens(name + description + schema),
            "writes": writes,
            "relevant_to_tasks": relevant,
            "description": description[:160],
        })
    return {
        "tools": rows,
        "count": len(rows),
        "total_chars": total_chars,
        "total_tokens_estimate": estimate_tokens("x" * total_chars),
    }


def read_task_words(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8").lower()
    words = {w.strip(".,:;()[]«»\"'") for w in text.split()}
    return {w for w in words if len(w) > 4}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Аудит чужого MCP-сервера")
    parser.add_argument("--server", help="команда запуска сервера")
    parser.add_argument("--from-json", help="готовый ответ tools/list, если сервер не поднять")
    parser.add_argument("--tasks", help="TASKS.md — чтобы сверить инструменты с задачами")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--i-trust-this-server", action="store_true",
                        help="подтверждение запуска чужого кода")
    args = parser.parse_args(argv)

    checks: list[dict] = []

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        tools = data.get("tools", data) if isinstance(data, dict) else data
        checks.append({"name": "source", "status": "info",
                       "detail": "список инструментов взят из файла, сервер не запускался"})
    elif not args.server:
        print(json.dumps({"ok": False, "action": "mcp_audit", "checks": [
            {"name": "no_input", "status": "fail",
             "detail": "нужен --server или --from-json"}], "exit_code": 1},
            ensure_ascii=False, indent=2))
        return 1
    elif not args.i_trust_this_server:
        print(json.dumps({"ok": False, "action": "mcp_audit", "checks": [
            {"name": "confirmation_required", "status": "fail",
             "detail": "аудит запускает чужой код на вашей машине",
             "fix": "если сервер вам знаком, повторите с флагом --i-trust-this-server; "
                    "иначе получите список инструментов вручную и передайте --from-json"}],
            "exit_code": 2}, ensure_ascii=False, indent=2))
        return 2
    else:
        try:
            tools = fetch_tools(args.server, args.timeout)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "action": "mcp_audit", "checks": [
                {"name": "server_start", "status": "fail",
                 "detail": f"сервер не запустился: {exc}",
                 "fix": "проверьте команду; либо возьмите список инструментов из документации "
                        "и передайте --from-json"}], "exit_code": 1},
                ensure_ascii=False, indent=2))
            return 1
        if not tools:
            checks.append({"name": "tools_list", "status": "fail",
                           "detail": "сервер не вернул список инструментов",
                           "fix": "возможно, нужны ключи в окружении; тогда разбирайте документацию руками"})
            print(json.dumps({"ok": False, "action": "mcp_audit", "checks": checks,
                              "exit_code": 1}, ensure_ascii=False, indent=2))
            return 1

    report = analyse(tools, read_task_words(Path(args.tasks) if args.tasks else None))

    checks.append({
        "name": "context_cost",
        "status": "info",
        "detail": f"инструментов {report['count']}, их описания занимают "
                  f"{report['total_chars']} символов — это примерно "
                  f"{report['total_tokens_estimate']} токенов в КАЖДОМ запросе, "
                  f"независимо от того, вызываете вы их или нет",
    })

    writers = [t["name"] for t in report["tools"] if t["writes"]]
    if writers:
        checks.append({
            "name": "write_access",
            "status": "warn",
            "detail": "умеют менять состояние: " + ", ".join(writers),
            "fix": "чужой сервер подключается только на чтение — уточните, что делают эти ручки",
        })
    else:
        checks.append({"name": "write_access", "status": "ok",
                       "detail": "инструментов, меняющих состояние, не видно"})

    if args.tasks:
        irrelevant = [t["name"] for t in report["tools"] if t["relevant_to_tasks"] is False]
        if irrelevant:
            checks.append({
                "name": "coverage",
                "status": "warn",
                "detail": f"не пересекаются с вашими задачами: {len(irrelevant)} из {report['count']} "
                          f"({', '.join(irrelevant[:8])}{'…' if len(irrelevant) > 8 else ''})",
                "fix": "если нужны две ручки из пятнадцати — сузьте: свой тонкий слой или скрипт",
            })

    print(json.dumps({
        "ok": True,
        "action": "mcp_audit",
        "checks": checks,
        "data": report,
        "next": "Дальше — бриф адаптации: какие ручки нужны, какая атрибуция и период "
                "постоянны, и к чему сводим: оставить, сузить или заменить скриптом.",
        "exit_code": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
