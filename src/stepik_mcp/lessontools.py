"""Работа с локальным файлом урока: оглавление, запуск примеров кода, предпросмотр.

Без запросов к Stepik. Предпросмотр пишет только свой файл <урок>.preview.html,
который каждый раз создаётся заново; файл урока не меняется.
"""

import html
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from . import coursemap
from . import workspace as sw
from .client import StepikError
from .publish import STEP_MARKER, _fence_after, _manifest_item, file_format
from .render import md_to_html
from .solution import _clip, _environment
from .steps import load_yaml, split_body

FENCE_OPEN = re.compile(r"^(\s*)(`{3,})\s*([\w+#.-]*)(.*)$")
PYTHON = {"python", "py", "python3"}
OUTPUT = {"", "text", "output", "console", "plaintext", "txt", "out"}
MAX_GAP = 100
MAX_RUNS = 100
RUN_TIMEOUT = 5
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def lesson_file(course_id, lesson_position):
    folder = coursemap.course_folder(course_id)
    item, path = _manifest_item(folder, course_id, lesson_position)
    return folder, item, path


def step_segments(text):
    """Шаги файла урока со строками: [{"step_id", "delete", "start", "end"}], start/end — индексы строк."""
    lines = text.replace("\r\n", "\n").split("\n")
    track = file_format(text) >= 2
    segments, fence = [], None
    for index, line in enumerate(lines):
        fence = _fence_after(fence, line) if track else None
        match = None if fence else STEP_MARKER.match(line.rstrip())
        if match:
            if segments:
                segments[-1]["end"] = index
            number = match[1] or match[3]
            segments.append({"step_id": int(number) if number else None, "delete": bool(match[3]),
                             "start": index, "end": len(lines)})
    return lines, segments


def code_blocks(markdown):
    """Блоки кода: [{"lang", "code", "gap"}], gap — текст между этим и предыдущим блоком."""
    blocks, lines, index, last_end = [], markdown.split("\n"), 0, 0
    while index < len(lines):
        match = FENCE_OPEN.match(lines[index])
        if not match:
            index += 1
            continue
        indent, fence, lang = match[1], match[2], match[3].lower()
        body, close = [], index + 1
        while close < len(lines):
            stripped = lines[close].strip()
            if stripped.startswith(fence) and not stripped.strip("`"):
                break
            body.append(lines[close][len(indent):] if lines[close].startswith(indent) else lines[close])
            close += 1
        gap = "\n".join(lines[last_end:index]).strip()
        blocks.append({"lang": lang, "extra": match[4].strip(), "code": textwrap.dedent("\n".join(body)), "gap": gap})
        index = last_end = close + 1
    return blocks


def _yaml(body):
    condition, yaml_text = split_body(body)
    data = {}
    if yaml_text is not None:
        try:
            data = load_yaml(yaml_text)
        except StepikError:
            data = {"type": "unknown"}
    return condition, data


def lesson_outline(course_id, lesson_position):
    """Оглавление урока, чтобы читать только нужные строки файла."""
    _, item, path = lesson_file(course_id, lesson_position)
    lines, segments = step_segments(path.read_text(encoding="utf-8"))
    steps = []
    for segment in segments:
        if segment["delete"]:
            continue
        chunk = lines[segment["start"] + 1:segment["end"]]
        heading = next((line[3:].strip() for line in chunk if line.startswith("## ")), "")
        condition, data = _yaml("\n".join(line for line in chunk if not line.startswith("## ")))
        blocks = code_blocks(condition)
        prose = re.sub(r"(?ms)^\s*`{3,}.*?^\s*`{3,}\s*$", " ", condition)
        subheadings = [line.lstrip("# ").strip() for line in prose.split("\n") if re.match(r"\s*#{3,} ", line)]
        steps.append({
            "step": len(steps) + 1, "step_id": segment["step_id"], "type": data.get("type", "text"),
            "title": heading, "lines": f"{segment['start'] + 1}-{segment['end']}",
            "words": len(re.sub(r"<[^>]+>", " ", prose).split()), "code_blocks": len(blocks),
            "images": len(IMAGE.findall(condition)), **({"sections": subheadings[:10]} if subheadings else {}),
        })
    return {"status": "ok", "lesson": lesson_position, "title": item.get("title"), "path": str(path),
            "total_lines": len(lines), "steps": steps,
            "note": "lines — строки файла урока: читай только нужный шаг (offset/limit)."}


def _run_python(code):
    with tempfile.TemporaryDirectory(prefix="stepik-example-") as work:
        program = Path(work) / "example.py"
        program.write_text(code, encoding="utf-8")
        try:
            # -I не читает PYTHONIOENCODING, поэтому кодировку задаёт -X utf8.
            result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(program)], input="",
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=RUN_TIMEOUT, cwd=work, env=_environment())
        except subprocess.TimeoutExpired:
            return None, f"превышено время {RUN_TIMEOUT} с"
    stderr = (result.stderr or "").strip()
    error = stderr.splitlines()[-1] if result.returncode and stderr else ""
    return result.stdout or "", error or (f"код завершения {result.returncode}" if result.returncode else "")


FRAGMENT_START = ("elif ", "else:", "except", "finally:", "return", "await ", "@", "yield", ".")
FRAGMENT_ERRORS = ("outside function", "outside loop", "expected an indented block", "unexpected indent",
                   "'await' outside", "'return' outside", "'yield' outside")


def _fragment(code, error):
    """Пример — кусок кода из урока, а не программа: опирается на код выше или вырван из функции."""
    if error.startswith(("NameError", "UnboundLocalError")):
        return True
    if not error.startswith(("SyntaxError", "IndentationError")):
        return False
    first = next((line.strip() for line in code.split("\n") if line.strip()), "")
    return first.startswith(FRAGMENT_START) or any(part in error for part in FRAGMENT_ERRORS)


def _same(got, expected):
    def norm(text):
        return "\n".join(line.rstrip() for line in text.strip().split("\n"))
    return norm(got) == norm(expected)


def run_examples(course_id, lesson_position, step=None):
    """Запускает примеры Python из урока и сравнивает вывод с показанным в тексте."""
    _, _, path = lesson_file(course_id, lesson_position)
    lines, segments = step_segments(path.read_text(encoding="utf-8"))
    results, runs, counts = [], 0, {}
    number = 0
    for segment in segments:
        if segment["delete"]:
            continue
        number += 1
        if step is not None and number != step:
            continue
        condition, _ = _yaml("\n".join(line for line in lines[segment["start"] + 1:segment["end"]]
                                       if not line.startswith("## ")))
        blocks = code_blocks(condition)
        for index, block in enumerate(blocks):
            if block["lang"] not in PYTHON:
                continue
            following = blocks[index + 1] if index + 1 < len(blocks) else None
            expected = None
            if following and following["lang"] in OUTPUT and len(following["gap"]) <= MAX_GAP:
                expected = following["code"]
            entry = {"step": number, "example": index + 1, "first_line": _clip(block["code"].strip().split("\n")[0], 80)}
            if re.search(r"(?m)^>>> ", block["code"]):
                entry["status"] = "repl"
            elif runs >= MAX_RUNS:
                entry["status"] = "not_run"
            else:
                runs += 1
                got, error = _run_python(block["code"])
                if got is None:
                    entry["status"] = "timeout"
                elif "EOFError" in error:
                    entry["status"] = "needs_input"
                elif "ModuleNotFoundError" in error or "ImportError" in error:
                    entry.update(status="missing_module", error=_clip(error, 160))
                elif _fragment(block["code"], error):
                    entry.update(status="fragment", error=_clip(error, 160))
                elif error:
                    entry.update(status="error", error=_clip(error, 160))
                elif expected is None:
                    entry["status"] = "no_expected_output"
                elif not got.strip():
                    entry["status"] = "no_output"
                elif _same(got, expected):
                    entry["status"] = "ok"
                else:
                    entry.update(status="differs", expected=_clip(expected.strip()), got=_clip(got.strip()))
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
            results.append(entry)
    problems = [entry for entry in results if entry["status"] in ("error", "differs", "timeout")]
    return {
        "status": "ok", "lesson": lesson_position, "examples": len(results), "counts": counts,
        "problems": problems[:20],
        "note": "Запускаются только блоки ```python без запросов к Stepik: отдельный процесс, временная папка, "
                f"без секретов сервера, пустой ввод, до {RUN_TIMEOUT} с. Ожидаемый вывод — следующий блок "
                "```text (или без языка) сразу после примера. fragment — кусок кода, который опирается на код выше "
                "или вырван из функции (NameError, return вне функции): сам по себе не ошибка; no_output — пример "
                "ничего не выводит, блок после него не сравнивается; needs_input — пример ждёт ввода; missing_module — нужна "
                "библиотека; repl — пример в стиле >>>, не запускается.",
    }


CSS = """body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:860px;margin:0 auto;padding:24px 16px;
background:#f5f6f7;color:#222;line-height:1.55}section{background:#fff;border-radius:8px;padding:20px 24px;
margin:0 0 20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}.n{color:#888;font-size:13px;margin-bottom:8px}
pre{background:#2d3436;color:#eee;padding:12px;border-radius:6px;overflow:auto}code{font-family:Consolas,monospace}
img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}
.task{border-top:1px dashed #ccc;margin-top:16px;padding-top:12px}.task label{display:block;margin:6px 0}
.hint{color:#888;font-size:13px}.box{display:inline-block;border:1px solid #bbb;border-radius:4px;padding:4px 10px;
margin:3px}"""


def _task_html(data):
    kind = data.get("type", "unknown")
    parts = [f'<div class="task"><div class="hint">Задание: {html.escape(str(kind))} (ответы скрыты)</div>']
    if kind == "choice":
        control = "checkbox" if data.get("multiple") else "radio"
        for option in data.get("options") or []:
            text = option.get("text", "") if isinstance(option, dict) else str(option)
            parts.append(f'<label><input type="{control}" disabled> {text}</label>')
    elif kind in ("sorting",):
        parts += [f'<div class="box">{(o.get("text", "") if isinstance(o, dict) else o)}</div>'
                  for o in data.get("options") or []]
    elif kind == "matching":
        for pair in data.get("pairs") or []:
            if isinstance(pair, dict):
                parts.append(f'<div><span class="box">{pair.get("first", "")}</span> → '
                             f'<span class="box">{pair.get("second", "")}</span></div>')
    elif kind == "code":
        tests = data.get("tests") or []
        for given, expected in [tuple(test) for test in tests if isinstance(test, (list, tuple)) and len(test) == 2][
                :int(data.get("samples_count") or 0)]:
            parts.append(f"<p>Пример ввода:</p><pre>{html.escape(str(given))}</pre>"
                         f"<p>Пример вывода:</p><pre>{html.escape(str(expected))}</pre>")
        parts.append('<textarea rows="6" style="width:100%" disabled>Поле для кода</textarea>')
    elif kind in ("string", "number", "free-answer"):
        parts.append('<input style="width:60%" disabled placeholder="Ответ ученика">')
    parts.append("</div>")
    return "\n".join(parts)


def preview_lesson(course_id, lesson_position):
    """HTML-предпросмотр урока рядом с файлом урока: <урок>.preview.html."""
    _, item, path = lesson_file(course_id, lesson_position)
    lines, segments = step_segments(path.read_text(encoding="utf-8"))
    title = item.get("title") or lesson_position
    sections, number = [], 0
    for segment in segments:
        if segment["delete"]:
            continue
        number += 1
        condition, data = _yaml("\n".join(line for line in lines[segment["start"] + 1:segment["end"]]
                                          if not line.startswith("## ")))
        body = md_to_html(condition) if condition else ""
        task = _task_html(data) if data else ""
        sections.append(f'<section><div class="n">Шаг {number} из {{total}}</div>\n{body}\n{task}</section>')
    total = len(sections)
    page = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{html.escape(f"{lesson_position} {title}")}</title>'
            f"<style>{CSS}</style></head><body><h1>{html.escape(str(title))}</h1>\n"
            + "\n".join(section.replace("{total}", str(total)) for section in sections) + "</body></html>\n")
    target = path.with_name(path.name[:-3] + ".preview.html")
    sw._atomic_write(target, page, replace=True)
    return {"status": "ok", "lesson": lesson_position, "path": str(target), "steps": total,
            "note": "Предпросмотр приблизительный: оформление Stepik и проверка ответов не воспроизводятся. "
                    "Файл создаётся заново при каждом вызове; файл урока не меняется."}
