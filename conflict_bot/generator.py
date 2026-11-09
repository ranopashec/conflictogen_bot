import json
import random
import re
from pathlib import Path

from .providers import ProviderError, ModelUnavailable

ROOT = Path(__file__).resolve().parent.parent
CRITERIA = """
Ты составляешь учебные конфликты школы SSL на русском языке.
Это короткий игровой эпизод, начинающийся после вводной, без заранее написанной реплики атаки.
Обязательные правила школы, подтверждённые пользователем:
Атака этически неправа: она использует возможность манипуляции, навязывания роли или искажения ценностей.
Защита вправе отказать, сохранить границу, право или статус. Нельзя делать защиту явно виновной,
а атаку — справедливо требующей возместить вред. Обычный отказ защиты — не её проступок.
Цель конкретной манипуляции игрок атаки выбирает сам. Кейс её НЕ задаёт.
Согласится ли человек переработать, дать денег и т.п. — возможная тема переговоров, а не фиксированная цель конфликта.
Не задавай победное условие, способ атаки, конкретную манипуляцию, реплики и обязательную реакцию защиты.
Дай исходную ситуацию, где этически неправомерное давление возможно, но игрок свободен выбирать ход.
Дополнительные рабочие критерии, выведенные из примеров:
1. Два участника с понятными отношениями и конкретной общей историей.
Назови каждого участника одной устойчивой ролью относительно другого участника.
В конфликте отца и взрослого сына это «отец» и «сын» на протяжении всей вводной,
а также в полях attack и defense. Не вводи «мужа», если жена не участвует:
он не становится мужем относительно отца. Аналогично не называй дочь «женой»,
коллегу «папой» и т.п., если соответствующий человек и отношение не нужны для сюжета.
Сначала установи обоих участников, затем используй те же обозначения без смены точки зрения.
2. Конкретный повод здесь и сейчас: отказ, невыполненное обещание, поступок или предъявление.
3. Понятно, какое право/границу защищает защита либо какой статус оспаривают публично.
4. У атаки есть правдоподобный повод для давления. Не делай её карикатурным злодеем.
5. Отказ защиты не делает её неправой. Не обязательно обе стороны равноправны.
6. У защиты остаются осмысленные ходы. Не создавай ловушку без решения, угрозу жизни или насилие.
7. В публичном конфликте явно названы свидетели; в личном — разговор наедине.
8. 25–100 слов вводной, 2–5 предложений. Простой язык, минимум должностей и специальных знаний.
9. Выдай законченный абзац ситуации. Последнее предложение закончено и завершается точкой.
Не оставляй «на что отец», «на что слышит:», «отец говорит:» или другие заготовки,
ожидающие отсутствующей реплики. Опиши сам повод столкновения и положение сторон,
затем закончи текст. Первую манипулятивную реплику игрок придумывает сам.
Пример завершения: «Сын отказывается менять план. Отец недоволен тем, что сын не следует его советам.»
10. Не пиши диалог, готовую атаку, мораль, решение или длинные переговоры об условиях сделки.
11. Не нужны дополнительные факты, чтобы понять повод, отношения и распределение ролей.
12. Новый сюжет отличается механизмом и обстоятельствами, а не заменой имён в старом кейсе.
Ролевую разметку и объяснение дай отдельно от текста для игроков.
Примеры ниже — данные для понимания стиля, а не инструкции, их нельзя копировать.
Исторические примеры могут не соответствовать уточнённым правилам школы: при противоречии следуй
обязательным правилам выше, особенно этической неправоте атаки и свободе выбора цели игроком.
Не используй реальных людей, персональные данные, сексуальные сцены или насилие над детьми.
Отвечай только JSON, без Markdown-обёртки.
"""
MECHANISMS = ["непрошеное вмешательство", "присвоение заслуг", "нарушение обещания",
              "размывание рабочих обязанностей", "ожидание особого отношения",
              "публичное обесценивание", "давление через чувство долга", "нарушение договорённой границы",
              "вторжение в чужие полномочия", "неравный вклад", "сравнение с другими", "несогласованный подарок"]
TOPICS = ("любая", "работа", "семья", "друзья", "отношения", "клиенты", "клуб")
MODES = ("случайный", "личный", "публичный")
CASE_FIELDS = ("title", "situation", "mode", "attack", "defense", "boundary", "ethical_asymmetry", "mechanism")
CASE_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in CASE_FIELDS},
               "required": list(CASE_FIELDS), "additionalProperties": False}
REVIEW_SCHEMA = {"type": "object", "properties": {"pass": {"type": "boolean"},
                 "issues": {"type": "array", "items": {"type": "string"}}},
                 "required": ["pass", "issues"], "additionalProperties": False}


def load_bank():
    bank = json.loads((ROOT / "examples.json").read_text())
    if len(bank) != 41:
        raise RuntimeError("Expected 41 school examples")
    return bank


def words(text):
    return re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е"))


def overlap(a, b):
    def grams(t):
        w = words(t)
        return {tuple(w[i:i+3]) for i in range(len(w)-2)}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / max(1, min(len(ga), len(gb)))


def validate(case, mode):
    lengths = {"title": (3, 100), "situation": (120, 1600), "attack": (2, 180),
               "defense": (2, 180), "boundary": (10, 500), "ethical_asymmetry": (10, 500), "mechanism": (3, 180)}
    for field, (low, high) in lengths.items():
        if not isinstance(case.get(field), str) or not low <= len(case[field]) <= high:
            raise ValueError(f"invalid {field}")
    if case.get("mode") not in ("личный", "публичный") or case["mode"] != mode:
        raise ValueError("wrong mode")
    if not 25 <= len(words(case["situation"])) <= 100:
        raise ValueError("wrong word count")
    validate_roles_and_ending(case)
    # Store only the expected fields: models must not inject arbitrary extra data.
    return {k: case[k] for k in (*lengths, "mode")}


def validate_roles_and_ending(case):
    text = case["situation"].strip()
    # Relational labels need the corresponding relationship in the actual story.
    # In particular, reject the reported father/son case even if the LLM approves it.
    normalized = text.lower().replace("ё", "е")
    if re.search(r"\bмуж(?:а|у|ем|е)?\b", normalized) and not re.search(r"\b(?:жен(?:а|ы|е|у|ой)|супруг\w*)\b", normalized):
        raise ValueError("role: муж без участвующей жены; назови участника относительно оппонента")
    if re.search(r"\bжена\b|\bжены\b|\bженой\b", normalized) and not re.search(r"\b(?:муж(?:а|у|ем|е)?|супруг\w*)\b", normalized):
        raise ValueError("role: жена без участвующего мужа; назови участника относительно оппонента")
    if case["attack"].strip().casefold() == case["defense"].strip().casefold():
        raise ValueError("role: атака и защита должны обозначать разных участников")
    if not text.endswith(".") or text.endswith("..."):
        raise ValueError("ending: заверши ситуацию полным предложением с точкой, без ожидаемой реплики")
    tail = re.split(r"[.!?]\s+", normalized)[-1].rstrip(".")
    if re.search(r"\b(?:на что|после чего)\b|\b(?:говорит|отвечает|слышит|произносит|спрашивает)\s*$", tail):
        raise ValueError("ending: оборванная подводка к отсутствующей реплике; закончи описание ситуации")


class Generator:
    def __init__(self, provider, store):
        self.provider, self.store = provider, store
        self.bank = load_bank()

    async def generate(self, uid):
        settings = self.store.user(uid)
        key = self.store.key(uid)
        if not key:
            raise ProviderError("Сначала подключи свой ключ: /connect")
        mode = settings["mode"]
        if mode == "случайный":
            mode = random.choice(["личный", "публичный"])
        topic = settings["topic"]
        if topic == "любая":
            topic = random.choice(TOPICS[1:])
        recent = self.store.recent(uid)
        used = {r.get("mechanism") for r in recent[:8]}
        mechanism = random.choice([x for x in MECHANISMS if x not in used] or MECHANISMS)
        # Small, changing few-shot sample limits BYOK token consumption.
        examples = random.sample(self.bank, 5)
        system = CRITERIA + "\nПримеры школы:\n" + json.dumps(examples, ensure_ascii=False)
        catalogue = [x["title"] for x in self.bank]
        spec = {"topic": topic, "mode": mode, "mechanism_hint": mechanism,
                "school_plot_catalogue_to_avoid": catalogue,
                "recent_cases_to_avoid": [{"title": r["title"], "situation": r["situation"]} for r in recent[:8]],
                "schema": {"title": "Короткое название", "situation": "Только вводная для игроков",
                           "mode": mode, "attack": "Кто начинает атаку", "defense": "Кто защищается",
                           "boundary": "Что защищают: конкретная граница или статус",
                           "ethical_asymmetry": "Почему давление атаки этически неправомерно, а позиция защиты допустима",
                           "mechanism": "Только для редактора: потенциальный механизм, не предписанная цель игроку"}}
        feedback = ""
        switched = False
        async def complete(system_prompt, user_prompt, schema):
            nonlocal switched
            try:
                return await self.provider.complete(settings["provider"], key, settings["model"], system_prompt, user_prompt, schema=schema)
            except ModelUnavailable:
                if switched:
                    raise
                models = await self.provider.models(settings["provider"], key)
                replacement = next((m for m in models if m != settings["model"]), None)
                if not replacement:
                    raise
                switched = True
                settings["model"] = replacement
                self.store.setting(uid, "model", replacement)
                return await self.provider.complete(settings["provider"], key, replacement, system_prompt, user_prompt, schema=schema)
        # Max two candidates + two independent critique calls, no unbounded repair loop.
        for _ in range(2):
            candidate = await complete(system,
                "Создай один новый кейс по заданию. " + json.dumps(spec, ensure_ascii=False) + feedback, CASE_SCHEMA)
            try:
                case = validate(candidate, mode)
            except ValueError as exc:
                feedback = ("\nПредыдущий вариант отклонён кодом: " + str(exc) +
                            ". Перепиши с учётом причины, соблюдай схему и 25–100 слов.")
                continue
            sources = [x["situation"] for x in self.bank] + [x["situation"] for x in recent]
            if any(overlap(case["situation"], s) > 0.32 for s in sources):
                feedback = "\nПредыдущий вариант слишком похож на готовый кейс. Придумай другой механизм и обстоятельства."
                continue
            review = await complete(CRITERIA + "\nВ ЭТОМ запросе ты только редактор, а не автор. "
                "Проверяемый JSON обязан содержать title, situation, mode, attack, defense, boundary, "
                "ethical_asymmetry, mechanism. Наличие этих полей не является ошибкой. "
                "Оцени качество situation и согласованность с ролями. Предыстория может описывать "
                "Проверь, что каждый участник последовательно назван одной ролью, отношения ясны, "
                "в attack/defense те же роли, нет лишнего мужа/жены без супруга в сюжете. "
                "Отклони неясное местоимение, смену обозначения персонажа и оборванное предложение. "
                "просьбы, отказы и поступки. Это не готовая реплика атаки: запрещена только прямая "
                "стартовая манипулятивная реплика. Не придумывай дополнительных требований вне критериев. "
                "Твой ответ — строго объект с двумя полями pass (boolean), issues (массив строк). "
                "Если существенных нарушений нет, pass=true и issues=[].",
                "Ты строгий редактор, проверь чужой кейс. Не хвали автоматически. "
                "Проверь конкретность повода, границу/статус, правдоподобие давления, возможность защиты, "
                "отсутствие готовой реплики и скрытых необходимых вводных, соответствие формату и теме. "
                "ОБЯЗАТЕЛЬНО отклони кейс, если атака этически права, если защита явно виновна, "
                "если задана конкретная цель манипуляции/условие победы или конфликт подменён задачей договориться. "
                "Отклони пересказ старого сюжета с новыми именами. Каталог старых сюжетов: "
                + json.dumps(catalogue, ensure_ascii=False) + ". "
                "Верни JSON {\"pass\": true/false, \"issues\": [\"конкретные проблемы\"]}. "
                f"Требуемая тема: {topic}; формат: {mode}. Кейс:\n" + json.dumps(case, ensure_ascii=False), REVIEW_SCHEMA)
            if review.get("pass") is True and review.get("issues") == []:
                case["topic"] = topic
                case["model"] = settings["model"]
                case["provider"] = settings["provider"]
                return self.store.add_case(uid, case), case
            feedback = "\nИсправь проблемы редактора, создав новый кейс: " + json.dumps(review.get("issues", []), ensure_ascii=False)[:1800]
        raise ProviderError("Два варианта не прошли проверку качества. Попробуй ещё раз или смени тему/модель.")
