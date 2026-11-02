"""Шаги Stepik в локальном Markdown и обратно (формат урока 2).

Тело шага в файле урока: условие в Markdown, а у заданий ещё YAML-блок
с настройками в конце:

    Условие задачи.

    ```yaml stepik
    type: choice
    options:
    - text: верный вариант
      correct: true
    ```

to_markdown(source) строит тело из step-source. build_block(base, body)
собирает блок для Stepik обратно: берёт исходный блок base и меняет в нём
только то, что изменилось в теле. Неизменённое условие сохраняет исходный
HTML, а поля, которых нет в YAML, переносятся из base как есть. Поэтому
build_block(base, to_markdown(base)) == base для любого шага.
"""

import copy
import math
import re

import yaml

from .client import StepikError
from .render import code_fence, html_to_md, md_to_html

# 2 — YAML-блоки и исправленные списки; 3 — исправленные цитаты (врезки);
# 4 — таблицы как HTML и спойлеры <details> с Markdown внутри.
FORMAT = 4
MISSING = object()

YAML_FENCE = re.compile(r"^(`{3,})yaml stepik\s*$")


# --------------------------------------------------------------------------
# Поля
# --------------------------------------------------------------------------

def _get(obj, path):
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return MISSING
        obj = obj[key]
    return obj


def _set(obj, path, value):
    for key in path[:-1]:
        obj = obj.setdefault(key, {})
    obj[path[-1]] = value


class Field:
    """Скалярное поле блока. default=MISSING — поле пишется в YAML всегда."""

    def __init__(self, name, path, default=MISSING, mirrors=()):
        self.name, self.path, self.default, self.mirrors = name, path, default, mirrors

    def read(self, block):
        value = _get(block, self.path)
        return value if value is MISSING else copy.deepcopy(value)

    def write(self, block, value):
        _set(block, self.path, value)
        # Stepik дублирует часть настроек в block.options для учеников.
        for mirror in self.mirrors:
            if _get(block, mirror) is not MISSING:
                _set(block, mirror, value)


class Items(Field):
    """Список объектов: варианты теста, пары, ответы. Лишние ключи элементов сохраняются."""

    def __init__(self, name, path, fields):
        super().__init__(name, path)
        self.fields = fields

    def read(self, block):
        rows = _get(block, self.path)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return MISSING
        out = []
        for row in rows:
            item = {}
            for field in self.fields:
                value = field.read(row)
                if value is not MISSING and value != field.default:
                    item[field.name] = value
            out.append(item)
        return out

    def write(self, block, value):
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise StepikError(f"{self.name}: ожидается список объектов", status=400)
        base = _get(block, self.path)
        base = base if isinstance(base, list) else []
        rows = []
        for number, item in enumerate(value):
            unknown = set(item) - {field.name for field in self.fields}
            if unknown:
                raise StepikError(f"{self.name}[{number + 1}]: неизвестные поля {sorted(unknown)}", status=400)
            row = copy.deepcopy(base[number]) if number < len(base) and isinstance(base[number], dict) else {}
            for field in self.fields:
                current = item.get(field.name, field.default)
                if current is MISSING:
                    raise StepikError(f"{self.name}[{number + 1}]: нет поля {field.name}", status=400)
                field.write(row, current)
            rows.append(row)
        _set(block, self.path, rows)


class Tests(Field):
    """source.test_cases: [[ввод, вывод], ...] -> [{input, output}, ...]."""

    def read(self, block):
        rows = _get(block, self.path)
        if not isinstance(rows, list) or not all(isinstance(row, list) and len(row) == 2 for row in rows):
            return MISSING
        return [{"input": row[0], "output": row[1]} for row in rows]

    def write(self, block, value):
        if not isinstance(value, list) or not all(
                isinstance(item, dict) and set(item) == {"input", "output"} for item in value):
            raise StepikError("tests: каждый тест — объект с полями input и output", status=400)
        _set(block, self.path, [[item["input"], item["output"]] for item in value])


TEMPLATE_MARK = re.compile(r"(?m)^::([\w.+# -]+)\n")
SECTIONS = ("header", "code", "footer")


def parse_templates(data):
    """source.templates_data -> {язык: код} или {язык: {header, code, footer}}; None, если формат иной."""
    if not isinstance(data, str) or not data.startswith("::"):
        return None
    parts = TEMPLATE_MARK.split(data)
    if parts[0] != "":
        return None
    out = {}
    language = None
    for name, content in zip(parts[1::2], parts[2::2]):
        if name in SECTIONS and language is not None:
            entry = out[language]
            if not isinstance(entry, dict):
                if entry != "":
                    return None
                entry = out[language] = {}
            if name in entry:
                return None
            entry[name] = content
        else:
            if name in out or name in SECTIONS:
                return None
            language = name
            out[language] = content
    return out


def serialize_templates(templates):
    out = []
    for language, entry in templates.items():
        out.append(f"::{language}\n")
        if isinstance(entry, dict):
            for name, content in entry.items():
                if name not in SECTIONS:
                    raise StepikError(f"templates.{language}: допустимы разделы {', '.join(SECTIONS)}", status=400)
                out.append(f"::{name}\n{content}")
        else:
            out.append(entry)
    return "".join(out)


class Templates(Field):
    """source.templates_data в виде словаря по языкам. Если формат не разобран, поле не показывается."""

    def read(self, block):
        data = _get(block, self.path)
        templates = parse_templates(data)
        if templates is None or serialize_templates(templates) != data:
            return MISSING
        return templates

    def write(self, block, value):
        if not isinstance(value, dict) or not all(isinstance(entry, (str, dict)) for entry in value.values()):
            raise StepikError("templates: ожидается словарь «язык: код»", status=400)
        _set(block, self.path, serialize_templates(value))


FEEDBACK = [Field("feedback_correct", ("feedback_correct",), ""), Field("feedback_wrong", ("feedback_wrong",), "")]
HTML = Field("html", ("source", "is_html_enabled"), True)

SPECS = {
    "choice": [
        Field("multiple", ("source", "is_multiple_choice"), False, mirrors=(("options", "is_multiple_choice"),)),
        Field("show", ("source", "sample_size")),
        Field("keep_order", ("source", "preserve_order"), False),
        Field("always_correct", ("source", "is_always_correct"), False),
        HTML,
        Field("options_feedback", ("source", "is_options_feedback"), False),
        Items("options", ("source", "options"), [
            Field("text", ("text",)), Field("correct", ("is_correct",), False), Field("feedback", ("feedback",), ""),
        ]),
    ],
    "matching": [
        Field("keep_order", ("source", "preserve_firsts_order"), True),
        HTML,
        Items("pairs", ("source", "pairs"), [Field("first", ("first",)), Field("second", ("second",))]),
    ],
    "sorting": [HTML, Items("options", ("source", "options"), [Field("text", ("text",))])],
    "string": [
        Field("answer", ("source", "pattern")),
        Field("regex", ("source", "use_re"), False),
        Field("substring", ("source", "match_substring"), False),
        Field("case_sensitive", ("source", "case_sensitive"), False),
        Field("text_disabled", ("source", "is_text_disabled"), False),
        Field("file_disabled", ("source", "is_file_disabled"), True),
        Field("checker", ("source", "code")),
    ],
    "number": [Items("answers", ("source", "options"), [Field("answer", ("answer",)), Field("max_error", ("max_error",), "0")])],
    "free-answer": [
        Field("attachments", ("source", "is_attachments_enabled"), False),
        HTML,
        Field("manual_scoring", ("source", "manual_scoring"), False),
    ],
    "code": [
        Field("time_limit", ("source", "execution_time_limit"), mirrors=(("options", "execution_time_limit"),)),
        Field("memory_limit", ("source", "execution_memory_limit"), mirrors=(("options", "execution_memory_limit"),)),
        Field("run_user_code", ("source", "is_run_user_code_allowed"), True,
              mirrors=(("options", "is_run_user_code_allowed"),)),
        Templates("templates", ("source", "templates_data")),
        Field("samples_count", ("source", "samples_count"), 0),
        Tests("tests", ("source", "test_cases")),
        Field("checker", ("source", "code")),
    ],
}
# Типы без удобной схемы (table, parsons, fill-blanks и новые) показывают source целиком.
GENERIC = [Field("source", ("source",), {})]


def spec(kind):
    if kind == "text":
        return []
    return SPECS.get(kind, GENERIC) + FEEDBACK


def has_schema(kind):
    return kind in SPECS


# Ключи YAML, которые нужны только локально и на Stepik не отправляются.
LOCAL_KEYS = ("solution",)

# Флажок Stepik по умолчанию: checker сравнивает ответ с выводом теста без пробелов по краям.
DEFAULT_CODE_CHECKER = (
    "# This is a sample Code Challenge\n# Learn more: https://stepik.org/lesson/9172\n"
    "# Ask your questions via help@stepik.org\n\ndef generate():\n    return []\n\n"
    "def check(reply, clue):\n    return reply.strip() == clue.strip()\n\n"
    "# def solve(dataset):\n#     a, b = dataset.split()\n#     return str(int(a) + int(b))"
)
DEFAULT_STRING_CHECKER = (
    "# def check(reply):\n#     \"\"\"Evaluate the learner's reply.\n#\n"
    "#     It should return 1 or True for the correct reply and 0 or False\n#     for the incorrect one.\n#\n"
    "#     A partial solution may be scored using a float number from the\n"
    "#     interval (0, 1). In such a case the learner total score for the\n"
    "#     problem will be 'step cost' * 'score'.\n#\n"
    "#     :param reply: a string that is the learner's reply to the problem\n"
    "#     :return: a score number (int or float) in range [0, 1]\n#\n#     \"\"\"\n"
    "#     return reply == \"Hello\"\n\n# def solve():\n"
    "#     \"\"\"Return a correct reply. This function is *optional*.\n#\n"
    "#     It is used to test the correctness of the 'check' function.\n#\n"
    "#     :return: a string that is a correct reply to the problem\n#\n#     \"\"\"\n#     return \"Hello\""
)

# Блоки новых шагов: значения по умолчанию, как у шагов, созданных в редакторе Stepik.
DEFAULT_SOURCES = {
    "choice": {"is_multiple_choice": False, "is_always_correct": False, "sample_size": 0, "preserve_order": False,
               "is_html_enabled": True, "is_options_feedback": False, "options": []},
    "matching": {"preserve_firsts_order": True, "is_html_enabled": True, "pairs": []},
    "sorting": {"is_html_enabled": True, "options": []},
    "string": {"pattern": "", "use_re": False, "match_substring": False, "case_sensitive": False,
               "code": DEFAULT_STRING_CHECKER, "is_text_disabled": False, "is_file_disabled": True},
    "number": {"options": []},
    "free-answer": {"is_attachments_enabled": False, "is_html_enabled": True, "manual_scoring": False},
    "code": {"code": DEFAULT_CODE_CHECKER, "execution_time_limit": 5, "execution_memory_limit": 256,
             "samples_count": 0, "templates_data": "::python3\n", "is_time_limit_scaled": True,
             "is_memory_limit_scaled": True, "is_run_user_code_allowed": True, "manual_time_limits": [],
             "manual_memory_limits": [], "test_archive": [], "test_cases": []},
}
PUBLISHABLE = set(DEFAULT_SOURCES)


def default_block(kind):
    if kind not in DEFAULT_SOURCES:
        raise StepikError(f"Создание шагов типа {kind} не поддерживается", status=400)
    block = {"name": kind, "text": "", "source": copy.deepcopy(DEFAULT_SOURCES[kind]),
             "feedback_correct": "", "feedback_wrong": ""}
    if kind == "choice":
        block["options"] = {"is_multiple_choice": False}
    return block


# --------------------------------------------------------------------------
# Проверка до отправки
# --------------------------------------------------------------------------

def _number(value, name):
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        raise StepikError(f"{name}: «{value}» — не число", status=400) from None
    if not math.isfinite(number):
        raise StepikError(f"{name}: «{value}» — не число", status=400)
    return number


def validate(block):
    """Явные ошибки задания, которые Stepik принял бы, а ученик увидел бы сломанную задачу."""
    kind = block.get("name")
    source = block.get("source") or {}
    problems = []
    if kind == "choice":
        options = source.get("options") or []
        correct = sum(1 for option in options if option.get("is_correct"))
        if len(options) < 2:
            problems.append("нужно хотя бы два варианта")
        if any(not str(option.get("text", "")).strip() for option in options):
            problems.append("у варианта пустой текст")
        if not source.get("is_always_correct"):
            if source.get("is_multiple_choice") and correct < 1:
                problems.append("нет ни одного правильного варианта")
            if not source.get("is_multiple_choice") and correct != 1:
                problems.append(f"в тесте с одним ответом правильных вариантов {correct}, а нужен ровно один")
        show = source.get("sample_size")
        if not isinstance(show, int) or not 1 <= show <= len(options):
            problems.append(f"show должно быть от 1 до {len(options)} (число вариантов)")
        elif not source.get("is_multiple_choice") and correct and show < 2:
            problems.append("show меньше 2: ученик увидит один вариант")
    elif kind == "string":
        if not str(source.get("pattern", "")).strip():
            problems.append("answer пустой")
        elif source.get("use_re"):
            try:
                re.compile(source["pattern"])
            except re.error as exc:
                problems.append(f"answer — неверное регулярное выражение: {exc}")
    elif kind == "number":
        answers = source.get("options") or []
        if not answers:
            problems.append("нет ни одного ответа")
        for number, answer in enumerate(answers, 1):
            try:
                _number(answer.get("answer"), f"answers[{number}].answer")
                if _number(answer.get("max_error", "0"), f"answers[{number}].max_error") < 0:
                    problems.append(f"answers[{number}].max_error меньше нуля")
            except StepikError as exc:
                problems.append(str(exc))
    elif kind == "matching":
        pairs = source.get("pairs") or []
        if len(pairs) < 2:
            problems.append("нужно хотя бы две пары")
        firsts = [str(pair.get("first", "")).strip() for pair in pairs]
        if any(not first for first in firsts) or any(not str(pair.get("second", "")).strip() for pair in pairs):
            problems.append("у пары пустая сторона")
        if len(set(firsts)) != len(firsts):
            problems.append("левые части пар повторяются")
    elif kind == "sorting":
        options = source.get("options") or []
        if len(options) < 2:
            problems.append("нужно хотя бы два элемента")
        if any(not str(option.get("text", "")).strip() for option in options):
            problems.append("у элемента пустой текст")
    elif kind == "code":
        tests = source.get("test_cases") or []
        if not tests:
            problems.append("нет ни одного теста")
        # samples_count бывает больше числа тестов у рабочих задач Stepik, поэтому только >= 0.
        samples = source.get("samples_count", 0)
        if not isinstance(samples, int) or samples < 0:
            problems.append("samples_count: ожидается целое число не меньше 0")
        for name, low, high in (("execution_time_limit", 1, 60), ("execution_memory_limit", 16, 2048)):
            value = source.get(name)
            if not isinstance(value, int) or not low <= value <= high:
                problems.append(f"{name}: ожидается целое от {low} до {high}")
        templates = parse_templates(source.get("templates_data"))
        if templates is not None and not templates:
            problems.append("нет ни одного языка в templates")
    if problems:
        raise StepikError(f"Задание {kind} не прошло проверку: " + "; ".join(problems), status=400)


def settings(block):
    """Настройки задания для YAML; поля со значением по умолчанию опускаются."""
    out = {}
    for field in spec(block.get("name")):
        value = field.read(block)
        if value is not MISSING and value != field.default:
            out[field.name] = value
    return out


# --------------------------------------------------------------------------
# YAML
# --------------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper, value):
    # Многострочный код и тесты читаются как есть, без экранирования \n.
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dump_yaml(data):
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=10 ** 6)


def load_yaml(text):
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StepikError(f"Ошибка в YAML-блоке: {exc}", status=400) from exc
    if not isinstance(data, dict):
        raise StepikError("YAML-блок должен быть словарём «поле: значение»", status=400)
    return data




# --------------------------------------------------------------------------
# Тело шага
# --------------------------------------------------------------------------

def to_markdown(source, fmt=FORMAT):
    """Тело шага для файла урока формата fmt (2 и выше)."""
    block = source.get("block") or {}
    condition = html_to_md(block.get("text"), old_quotes=fmt < 3, old_blocks=fmt < 4)
    kind = block.get("name", "unknown")
    if kind == "text":
        return condition
    text = dump_yaml({"type": kind, **settings(block)})
    fence = code_fence(text)
    yaml_block = f"{fence}yaml stepik\n{text}{fence}"
    return f"{condition}\n\n{yaml_block}" if condition else yaml_block


def split_body(body):
    """Тело шага -> (условие в Markdown, текст YAML или None). YAML-блок стоит последним."""
    lines = body.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "", None
    closing = lines[-1].strip()
    if not re.fullmatch(r"`{3,}", closing):
        return body.strip(), None
    for start in range(len(lines) - 2, -1, -1):
        match = YAML_FENCE.match(lines[start].strip())
        if match and match[1] == closing:
            return "\n".join(lines[:start]).strip(), "\n".join(lines[start + 1:-1]) + "\n"
    return body.strip(), None


def build_block(base_block, body):
    """Собирает блок Stepik из тела шага, начиная с исходного блока base_block."""
    kind = base_block.get("name")
    condition, yaml_text = split_body(body)
    block = copy.deepcopy(base_block)
    if condition != html_to_md(base_block.get("text")):
        block["text"] = md_to_html(condition)
    if kind == "text":
        if yaml_text is not None:
            raise StepikError("У текстового шага не бывает YAML-блока", status=400)
        return block
    if yaml_text is None:
        raise StepikError(f"У шага {kind} нет YAML-блока ```yaml stepik", status=400)
    data = load_yaml(yaml_text)
    for key in LOCAL_KEYS:
        data.pop(key, None)
    if data.pop("type", None) != kind:
        raise StepikError(f"Тип шага в YAML должен быть {kind}: менять тип нельзя", status=400)
    fields = spec(kind)
    unknown = set(data) - {field.name for field in fields}
    if unknown:
        raise StepikError(f"Неизвестные поля YAML для {kind}: {sorted(unknown)}", status=400)
    base = settings(base_block)
    for field in fields:
        new = data.get(field.name, field.default)
        # Поле без значения по умолчанию, которого нет в YAML (например, автор
        # не стал выписывать checker), остаётся как в исходнике.
        if new is not MISSING and new != base.get(field.name, field.default):
            field.write(block, new)
    return block


def local_settings(body):
    """Локальные ключи YAML-блока (например, solution), которые на Stepik не уходят."""
    yaml_text = split_body(body)[1]
    if yaml_text is None:
        return {}
    data = load_yaml(yaml_text)
    return {key: data[key] for key in LOCAL_KEYS if key in data}


def new_block(body):
    """Блок для нового задания: значения по умолчанию плюс поля из YAML."""
    condition, yaml_text = split_body(body)
    if yaml_text is None:
        raise StepikError("У нового задания нет YAML-блока ```yaml stepik", status=400)
    data = load_yaml(yaml_text)
    kind = data.get("type")
    base = default_block(kind)
    merged = {**settings(base), **{key: value for key, value in data.items() if key not in LOCAL_KEYS}}
    if kind == "choice" and "show" not in data:
        merged["show"] = len(merged.get("options") or [])
    text = dump_yaml({"type": kind, **{key: value for key, value in merged.items() if key != "type"}})
    fence = code_fence(text)
    block = build_block(base, f"{condition}\n\n{fence}yaml stepik\n{text}{fence}")
    if not condition:
        raise StepikError("У нового задания нет условия", status=400)
    return block


def changed_paths(old, new, prefix=""):
    """Пути отличающихся полей двух блоков: для диагностики и diff."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new), key=str):
            out += changed_paths(old.get(key, MISSING), new.get(key, MISSING), f"{prefix}.{key}" if prefix else str(key))
        return out
    return [] if old == new else [prefix or "(весь блок)"]
