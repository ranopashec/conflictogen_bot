"""Small private-chat Telegram bot, using long polling (no public ports)."""
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time
import unicodedata

import httpx

from .generator import Generator, MODES, TOPICS, ROOT
from .providers import Provider, ProviderError, model_id
from .store import Store
from .bank import BankMixin

log = logging.getLogger("conflict_bot")
PROVIDERS = {"gemini": "Gemini", "groq": "Groq"}
KEY_LINKS = {"gemini": "https://aistudio.google.com/apikey", "groq": "https://console.groq.com/keys"}


def normalize_key(text):
    """Accept common clipboard wrappers; the provider decides whether a key is valid."""
    text = "".join(c for c in text if unicodedata.category(c) != "Cf").strip()
    # API consoles/clipboard snippets often include quotes, an env-var assignment,
    # or a short label. Extract only one recognizable Gemini/Groq key.
    found = set(re.findall(r"(?<![A-Za-z0-9_-])(?:gsk_[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{10,})", text))
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        return None
    text = text.strip("`\"'“”‘’ \n\r\t")
    text = re.sub(r"^(?:export\s+)?(?:GROQ_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|API[_ -]?KEY|Ключ)\s*[:=]\s*",
                  "", text, flags=re.I).strip("`\"'“”‘’ \n\r\t")
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    # Do not assume a particular vendor key length/alphabet. Never send arbitrary
    # multiline prose to providers as an Authorization header.
    return text if 10 <= len(text) <= 2048 and all(33 <= ord(c) <= 126 for c in text) else None


def keyboard(rows):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


MENU = keyboard([[('🎲 Новый конфликт', 'new')], [('Тема', 'topics'), ('Формат', 'modes')],
                 [('Мои конфликты', 'bank:list:mine:0'), ('Общий банк', 'bank:list:public:0')],
                 [('Мой ключ и модель', 'settings')]])


class TelegramError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Telegram error {code}")


class Bot(BankMixin):
    def __init__(self, token, store, client, daily_limit=30, admins=()):
        self.base = f"https://api.telegram.org/bot{token}"
        self.store, self.client = store, client
        self.provider = Provider(client)
        self.generator = Generator(self.provider, store)
        self.pending = {}
        self.jobs = {}
        self.sem = asyncio.Semaphore(4)
        self.daily_limit = daily_limit
        self.init_bank(admins)

    async def api(self, method, **payload):
        try:
            response = await self.client.post(f"{self.base}/{method}", json=payload, timeout=40)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise TelegramError(0) from None
        if not data.get("ok"):
            # Do not propagate descriptions: they may contain tokens/user-supplied text.
            raise TelegramError(data.get("error_code", response.status_code))
        return data["result"]

    async def say(self, uid, text, markup=None):
        return await self.api("sendMessage", chat_id=uid, text=text[:4000],
                              reply_markup=markup or MENU, link_preview_options={"is_disabled": True})

    async def connect(self, uid):
        self.drafts.discard(uid)
        self.pending.pop(uid, None)
        await self.say(uid, "Выбери провайдера. Будет использоваться только твой API-ключ. "
                       "Доступность и стоимость запросов зависят от твоего аккаунта у провайдера. "
                       "Одна генерация делает до четырёх запросов к модели.",
                       keyboard([[(label, f"provider:{name}") for name, label in PROVIDERS.items()]]))

    async def settings(self, uid):
        u = self.store.user(uid)
        await self.say(uid,
            f"Провайдер: {PROVIDERS.get(u['provider'], 'не подключён')}\n"
            f"Модель: {u['model'] or 'не выбрана'}\nТема: {u['topic']}\nФормат: {u['mode']}\n\n"
            "/connect — подключить или заменить ключ\n/models — доступные модели\n/model ИМЯ — выбрать модель\n"
            "/forget — удалить ключ\n/delete_me — удалить ключ и всю свою историю\n"
                       "/history — последние пять кейсов\n/bank — мой банк\n/public — общий банк\n"
                       "/add — добавить свой конфликт\n/cancel — отменить ввод или генерацию" +
                       ("\n/review — предложения участников" if uid in self.admins else ""),
            keyboard([[('Подключить ключ', 'connect')], [('Удалить ключ', 'forget')]]))

    async def begin_key(self, uid, provider):
        if provider not in PROVIDERS:
            return
        self.drafts.discard(uid)
        self.pending[uid] = (provider, time.monotonic())
        await self.say(uid,
            f"Ключ {PROVIDERS[provider]} можно создать здесь: {KEY_LINKS[provider]}\n\n"
            "Пришли только ключ следующим сообщением в течение 10 минут. "
            "Он пройдёт через Telegram и сервер бота. Сообщение попробую сразу удалить; "
            "в базе ключ будет зашифрован. Администратор сервера технически может его расшифровать. "
            "Ключ используется только для твоих генераций. Удаление: /forget. Отмена: /cancel.")

    async def accept_key(self, uid, message):
        provider, since = self.pending[uid]
        # Delete even malformed/expired messages, before making provider calls.
        deleted = True
        try:
            await self.api("deleteMessage", chat_id=uid, message_id=message["message_id"])
        except TelegramError:
            deleted = False
        key = normalize_key(message.get("text", ""))
        if time.monotonic() - since > 600:
            self.pending.pop(uid, None)
            await self.say(uid, "Время ввода истекло. Начни заново: /connect")
            return
        if not key:
            await self.say(uid, "Не получилось выделить один API-ключ из сообщения. Пришли его ещё раз "
                           "текстом; кавычки и подпись API_KEY= допустимы. Отмена: /cancel." +
                           ("" if deleted else "\nУдали сообщение с ключом вручную."))
            return
        try:
            models = await self.provider.models(provider, key)
            if not models:
                raise ProviderError("Не нашлось текстовых моделей для этого ключа.")
            self.store.configure(uid, provider, key, models[0])
            self.pending.pop(uid, None)
            note = "" if deleted else "\nНе удалось удалить сообщение с ключом — удали его вручную."
            await self.say(uid, f"Ключ сохранён. Модель: {models[0]}. "
                           "Успешная проверка списка моделей ещё не гарантирует доступную квоту генерации. "
                           "Выбрать другую: /models." + note)
        except ProviderError as exc:
            await self.say(uid, str(exc) + "\nКлюч не сохранён. Можно прислать исправленный ключ следующим сообщением. "
                           "Другой провайдер: /connect. Отмена: /cancel." +
                           ("" if deleted else "\nУдали сообщение с ключом вручную."))

    async def list_models(self, uid):
        u = self.store.user(uid)
        key = self.store.key(uid)
        if not key:
            await self.connect(uid)
            return
        models = await self.provider.models(u["provider"], key)
        await self.say(uid, "Доступные текстовые модели (первые 30):\n" + "\n".join(models[:30]) +
                       "\n\nВыбрать: /model имя_модели. Конкретная модель может требовать отдельную квоту.")

    async def generate(self, uid):
        try:
            async with self.sem:
                await self.say(uid, "Придумываю ситуацию и проверяю её по критериям школы…")
                case_id, case = await self.generator.generate(uid)
                await self.show_case(uid, case_id, case)
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            await self.say(uid, str(exc))
        except Exception:
            # Logging exception objects/tracebacks can leak provider credentials in URLs.
            log.error("Generation failed; details suppressed")
            await self.say(uid, "Не удалось подготовить кейс. Попробуй ещё раз; при повторении сообщи администратору.")
        finally:
            self.jobs.pop(uid, None)

    async def show_case(self, uid, case_id, case):
        await self.say(uid, case['situation'],
                       keyboard([[('🎲 Ещё конфликт', 'new')],
                                 [('Сохранить в мой банк', f"bank:save:{case_id}")],
                                 [('👍 Подходит', f"rate:{case_id}:1"), ('👎 Не подходит', f"rate:{case_id}:-1")]]))

    async def start_generation(self, uid):
        if uid in self.pending:
            await self.say(uid, "Сначала закончи ввод ключа или отмени его: /cancel")
            return
        if not self.store.key(uid):
            await self.connect(uid)
            return
        if uid in self.jobs:
            await self.say(uid, "Предыдущий кейс ещё готовится. Отменить: /cancel")
            return
        if not self.store.reserve(uid, self.daily_limit):
            await self.say(uid, f"Лимит бота — {self.daily_limit} попыток в сутки — исчерпан. Обновление в 00:00 UTC.")
            return
        self.jobs[uid] = asyncio.create_task(self.generate(uid))

    async def cancel(self, uid):
        self.drafts.discard(uid)
        self.pending.pop(uid, None)
        job = self.jobs.pop(uid, None)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message") if callback else update.get("message")
        if not message:
            return
        sender = callback.get("from", {}) if callback else message.get("from", {})
        uid = sender.get("id")
        # Keys and generation are strictly private; no group key entry.
        if message.get("chat", {}).get("type") != "private" or message["chat"]["id"] != uid:
            if callback:
                await self.api("answerCallbackQuery", callback_query_id=callback["id"], text="Открой личный чат с ботом.")
            return
        if callback:
            await self.api("answerCallbackQuery", callback_query_id=callback["id"])
            data = callback.get("data", "")
            if data.startswith("bank:"):
                await self.bank_callback(uid, data)
            elif data == "new":
                await self.start_generation(uid)
            elif data in ("connect", "settings"):
                await (self.connect(uid) if data == "connect" else self.settings(uid))
            elif data == "forget":
                await self.cancel(uid)
                self.store.forget_key(uid)
                await self.say(uid, "Ключ удалён.")
            elif data.startswith("provider:"):
                await self.begin_key(uid, data.split(":", 1)[1])
            elif data == "topics":
                await self.say(uid, "Выбери тему:", keyboard([[(t.capitalize(), f"topic:{t}")] for t in TOPICS]))
            elif data == "modes":
                await self.say(uid, "Выбери формат:", keyboard([[(t.capitalize(), f"mode:{t}")] for t in MODES]))
            elif data.startswith(("topic:", "mode:")):
                field, value = data.split(":", 1)
                if value in (TOPICS if field == "topic" else MODES):
                    self.store.setting(uid, field, value)
                    await self.say(uid, f"Сохранено: {value}.")
            elif data.startswith("explain:"):
                cid = int(data.split(":")[1])
                case = self.store.case(uid, cid)
                if case:
                    await self.show_case(uid, cid, case)
                else:
                    await self.say(uid, "Кейс не найден в твоей истории.")
            elif data.startswith("rate:"):
                _, cid, rating = data.split(":")
                if rating in ("1", "-1"):
                    self.store.rate(uid, int(cid), int(rating))
                    await self.say(uid, "Оценка сохранена. Она поможет проверить качество генератора.")
            return
        text = message.get("text", "").strip()
        if uid in self.pending and not text.startswith("/"):
            await self.accept_key(uid, message)
            return
        if uid in self.drafts and not text.startswith("/"):
            await self.bank_accept(uid, text)
            return
        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()
        if command in ("/start", "/help"):
            await self.say(uid, "Генератор конфликтов SSL\n\n"
                "Короткая вводная для игры: этически неправомерная атака и защита своей границы или статуса. "
                "Первую реплику и цель манипуляции выбирает игрок.\n\n"
                "1. /connect — подключи свой ключ Gemini или Groq.\n"
                "2. Выбери тему и формат кнопками.\n3. Нажми «Новый конфликт».\n\n"
                "В основе — 41 пример школы и её правила. Готовые кейсы сохраняются в /history. "
                "Свои ситуации: /add и /bank. Общий банк: /public. "
                "Банки доступны без API-ключа. Настройки и удаление данных: /settings.")
        elif command in ("/bank", "/public", "/review"):
            await self.bank_page(uid, {"/bank": "mine", "/public": "public", "/review": "review"}[command])
        elif command == "/add":
            await self.bank_add(uid)
        elif command == "/connect":
            await self.connect(uid)
        elif command == "/new":
            await self.start_generation(uid)
        elif command == "/settings":
            await self.settings(uid)
        elif command == "/models":
            await self.list_models(uid)
        elif command == "/model":
            chosen = model_id(arg.strip())
            if not self.store.key(uid):
                await self.connect(uid)
                return
            self.store.setting(uid, "model", chosen)
            await self.say(uid, f"Выбрана модель {chosen}. Доступ проверится при генерации.")
        elif command == "/forget":
            await self.cancel(uid)
            self.store.forget_key(uid)
            await self.say(uid, "Ключ удалён. Сохранённые кейсы остались в /history.")
        elif command == "/delete_me":
            await self.cancel(uid)
            self.store.erase(uid)
            await self.say(uid, "Твой ключ, настройки, личный банк, предложения, история и оценки удалены. "
                           "Ранее опубликованные копии остаются в общем банке без привязки к автору.")
        elif command == "/cancel":
            await self.cancel(uid)
            await self.say(uid, "Ввод ключа и текущая генерация отменены. Уже отправленный запрос провайдер может тарифицировать.")
        elif command == "/history":
            cases = self.store.recent(uid, 5)
            if not cases:
                await self.say(uid, "Пока нет сохранённых кейсов. Нажми «Новый конфликт».")
            for case in reversed(cases):
                await self.show_case(uid, case["id"], case)
        else:
            await self.say(uid, "Нажми «Новый конфликт» или /connect, чтобы подключить ключ. Помощь: /help")

    async def run(self):
        me = await self.api("getMe")
        webhook = await self.api("getWebhookInfo")
        if webhook.get("url"):
            raise RuntimeError("У бота настроен webhook. Используй отдельного бота или отключи webhook самостоятельно.")
        await self.api("setMyCommands", commands=[
            {"command": cmd, "description": description} for cmd, description in [
                ("new", "Новый конфликт"), ("connect", "Подключить свой API-ключ"),
                ("settings", "Настройки"), ("history", "Последние кейсы"),
                ("bank", "Мои конфликты"), ("add", "Добавить свой конфликт"),
                ("public", "Общий банк"), ("review", "Модерация: для администраторов"),
                ("forget", "Удалить API-ключ"), ("delete_me", "Удалить все свои данные"),
                ("cancel", "Отменить ввод или генерацию"), ("help", "Помощь")]])
        log.info("Polling started for @%s", me["username"])
        Path("/tmp/bot.ready").touch()
        try:
            while True:
                try:
                    updates = await self.api("getUpdates", offset=self.store.cursor(), timeout=25,
                                             allowed_updates=["message", "callback_query"])
                    Path("/tmp/bot.ready").touch()
                    for update in updates:
                        try:
                            await self.handle(update)
                        except ProviderError as exc:
                            uid = (update.get("callback_query", {}).get("from") or update.get("message", {}).get("from", {})).get("id")
                            if uid:
                                await self.say(uid, str(exc))
                        except Exception:
                            log.error("Update failed; details suppressed")
                        self.store.set_cursor(update["update_id"] + 1)
                except TelegramError as exc:
                    if exc.code in (401, 409):
                        raise RuntimeError("Telegram: неверный токен или уже запущен другой экземпляр бота.") from None
                    log.warning("Polling unavailable; retrying")
                    await asyncio.sleep(3)
        finally:
            for job in list(self.jobs.values()):
                job.cancel()
            await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)


def load_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


async def main():
    load_env(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    encryption_key = os.environ.get("ENCRYPTION_KEY", "")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token) or not encryption_key:
        raise SystemExit("Нужны TELEGRAM_BOT_TOKEN и ENCRYPTION_KEY в conflict-bot/.env. См. README.md.")
    store = Store(os.environ.get("DATABASE_PATH", str(ROOT / "data/bot.sqlite3")), encryption_key)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        admins = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
        await Bot(token, store, client, int(os.environ.get("DAILY_LIMIT", "30")), admins).run()


if __name__ == "__main__":
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        # Never print exceptions from HTTP clients: Bot API URLs contain the bot token.
        log.error("Bot stopped. Check configuration, connection and whether another instance is running.")
        raise SystemExit(1) from None
