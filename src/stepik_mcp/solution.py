"""Локальный прогон эталонного решения задачи на код перед публикацией.

Автор кладёт решение в файл рядом с уроком и указывает его в YAML-блоке
ключом solution: solutions/zadacha.py. Файл на Stepik не отправляется.
Решение собирается так же, как на Stepik (header + решение + footer),
и запускается на каждом тесте. Секреты сервера в окружение не передаются.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .client import StepikError
from .steps import DEFAULT_CODE_CHECKER, parse_templates
from .workspace import _inside

# Расширение файла -> (префикс языка Stepik, как найти интерпретатор).
RUNTIMES = {
    ".py": ("python", lambda: sys.executable),
    ".jl": ("julia", lambda: shutil.which("julia")),
}
MAX_REPORTED = 3


def _clip(text, limit=200):
    return text if len(text) <= limit else text[:limit] + "…"


def _environment():
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_solution(folder, lesson_path, block, solution):
    """Прогоняет решение на тестах задачи. Возвращает сводку; провал — passed < total."""
    if not isinstance(solution, str) or not solution.strip():
        raise StepikError("solution: укажите путь к файлу решения", status=400)
    relative = os.path.normpath(str(lesson_path.parent.relative_to(folder) / solution))
    path = _inside(folder, relative)
    if not path.is_file():
        raise StepikError(f"Файл решения не найден: {solution}", status=404)
    runtime = RUNTIMES.get(path.suffix.lower())
    if runtime is None:
        raise StepikError(f"solution: поддерживаются {', '.join(sorted(RUNTIMES))}", status=400)
    prefix, find = runtime
    interpreter = find()
    if not interpreter:
        return {"status": "not_run", "note": f"Не найден интерпретатор для {path.suffix}; решение не проверено"}

    source = block.get("source") or {}
    templates = parse_templates(source.get("templates_data")) or {}
    language = next((name for name in templates if name.lower().startswith(prefix)), None)
    entry = templates.get(language, "")
    header = entry.get("header", "") if isinstance(entry, dict) else ""
    footer = entry.get("footer", "") if isinstance(entry, dict) else ""
    program = "\n".join(part for part in (header.rstrip("\n"), path.read_text(encoding="utf-8").rstrip("\n"),
                                          footer.rstrip("\n")) if part) + "\n"
    limit = max(int(source.get("execution_time_limit") or 5), 1) * 3

    tests = source.get("test_cases") or []
    failures = []
    with tempfile.TemporaryDirectory(prefix="stepik-solution-") as work:
        program_path = Path(work) / f"main{path.suffix}"
        program_path.write_text(program, encoding="utf-8")
        for number, (given, expected) in enumerate(tests, 1):
            try:
                result = subprocess.run([interpreter, str(program_path)], input=given, capture_output=True,
                                        text=True, encoding="utf-8", timeout=limit, cwd=work, env=_environment())
                got = result.stdout
                error = result.stderr.strip().splitlines()[-1:] if result.returncode else []
            except subprocess.TimeoutExpired:
                got, error = "", [f"превышено время {limit} с"]
            if error or got.strip() != expected.strip():
                failures.append({"test": number, "input": _clip(given), "expected": _clip(expected),
                                 "got": _clip(got), **({"error": _clip(error[0])} if error else {})})
    report = {"status": "passed" if not failures else "failed", "language": language or prefix,
              "passed": len(tests) - len(failures), "total": len(tests), "failures": failures[:MAX_REPORTED]}
    if source.get("code") != DEFAULT_CODE_CHECKER:
        report["note"] = "У задачи свой checker; локально вывод сравнивается с ожидаемым без пробелов по краям"
    return report
