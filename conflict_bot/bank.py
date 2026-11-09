"""Private libraries and an opt-in, administrator-curated public catalogue."""
from .generator import load_bank


class BankMixin:
    def init_bank(self, admins):
        self.admins = set(admins)
        self.drafts = set()
        self.store.seed_public(load_bank())

    async def bank_page(self, uid, kind, offset=0):
        from .bot import keyboard
        offset = max(0, offset)
        if kind == "review" and uid not in self.admins:
            await self.say(uid, "Этот раздел доступен только администраторам.")
            return
        methods = {"mine": lambda: self.store.library_page(uid, offset),
                   "public": lambda: self.store.public_page(offset),
                   "review": lambda: self.store.moderation_page(offset)}
        rows = methods[kind]()
        labels = {"mine": "Мои конфликты — видны только тебе", "public": "Общий банк конфликтов",
                  "review": "Предложения участников — очередь модерации"}
        buttons = [[(f"{offset+i+1}. {r['title'][:65]}", f"bank:open:{kind}:{r['id']}")] for i, r in enumerate(rows[:5])]
        nav = []
        if offset:
            nav.append(("← Назад", f"bank:list:{kind}:{max(0,offset-5)}"))
        if len(rows) > 5:
            nav.append(("Далее →", f"bank:list:{kind}:{offset+5}"))
        if nav:
            buttons.append(nav)
        if kind == "mine":
            buttons.append([("＋ Добавить свой конфликт", "bank:add")])
        buttons.append([("Мои конфликты", "bank:list:mine:0"), ("Общий банк", "bank:list:public:0")])
        if uid in self.admins:
            buttons.append([("Предложения на публикацию", "bank:list:review:0")])
        await self.say(uid, labels[kind] + ("\n\nПока пусто." if not rows else "\n\nВыбери конфликт:"), keyboard(buttons))

    async def bank_open(self, uid, kind, cid):
        from .bot import keyboard
        if kind == "review" and uid not in self.admins:
            await self.say(uid, "Этот раздел доступен только администраторам.")
            return
        if kind == "mine":
            entry = self.store.library_entry(uid, cid)
            buttons = [[("Предложить в общий банк", f"bank:consent:{cid}")],
                       [("Удалить из моих", f"bank:remove:{cid}")]]
        elif kind == "public":
            entry = self.store.public_entry(cid)
            buttons = [[("Сохранить себе", f"bank:copy:{cid}")]]
            if uid in self.admins:
                buttons.append([("Снять с публикации", f"bank:unpublish:{cid}")])
        else:
            entry = self.store.submission(cid)
            buttons = [[("Опубликовать", f"bank:approve:{cid}"), ("Отклонить", f"bank:reject:{cid}")]]
        if not entry:
            await self.say(uid, "Запись уже удалена, рассмотрена или недоступна.")
            return
        buttons.append([("К списку", f"bank:list:{kind}:0")])
        await self.say(uid, entry['situation'], keyboard(buttons))

    async def bank_add(self, uid):
        self.pending.pop(uid, None)
        self.drafts.add(uid)
        await self.say(uid, "Пришли текст своего конфликта одним сообщением: первая строка — название, "
                       "далее — сама ситуация. От 30 до 2800 символов для ситуации.\n\n"
                       "Он сохранится только в твоём банке. Для публикации нужна отдельная отправка "
                       "на рассмотрение администратору. Отмена: /cancel.")

    async def bank_accept(self, uid, text):
        title, _, situation = text.partition("\n")
        if not situation.strip():
            situation = text
            title = text.split(".", 1)[0][:80]
        title, situation = title.strip(), situation.strip()
        # Avoid accidentally saving a key while a user is switching between flows.
        import re
        if re.search(r"(?:gsk_[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{10,})", text):
            await self.say(uid, "Это похоже на API-ключ. Он не добавлен в банк. Для подключения ключа используй /connect.")
            return
        try:
            cid = self.store.library_add(uid, title, situation)
        except ValueError:
            await self.say(uid, "Не удалось сохранить: название должно содержать 3–100 символов, "
                           "сама ситуация — 30–2800. Исправь текст и пришли повторно. Отмена: /cancel.")
            return
        self.drafts.discard(uid)
        await self.bank_open(uid, "mine", cid)

    async def bank_callback(self, uid, data):
        from .bot import keyboard
        parts = data.split(":")
        action = parts[1]
        if action == "add":
            await self.bank_add(uid)
            return
        if action in ("list", "open") and len(parts) == 4 and parts[2] in ("mine", "public", "review"):
            if action == "list":
                await self.bank_page(uid, parts[2], int(parts[3]))
            else:
                await self.bank_open(uid, parts[2], int(parts[3]))
            return
        if len(parts) != 3 or not parts[2].isdigit():
            return
        cid = int(parts[2])
        if action == "save":
            case = self.store.case(uid, cid)
            if case:
                entry_id = self.store.library_add(uid, case["title"], case["situation"], cid)
                await self.bank_open(uid, "mine", entry_id)
            return
        if action == "copy":
            entry = self.store.public_entry(cid)
            if entry:
                entry_id = self.store.library_add(uid, entry["title"], entry["situation"])
                await self.bank_open(uid, "mine", entry_id)
            return
        if action == "consent" and self.store.library_entry(uid, cid):
            await self.say(uid, "Администраторы увидят только этот предложенный текст. После одобрения "
                           "его копия будет доступна всем участникам без имени автора. Удали из текста "
                           "имена и другие личные сведения до отправки. Опубликованная копия остаётся "
                           "в общем банке при удалении твоей личной записи или аккаунта.",
                           keyboard([[("Отправить на рассмотрение", f"bank:submit:{cid}")],
                                     [("Вернуться", f"bank:open:mine:{cid}")]]))
        elif action == "submit":
            ok = self.store.submit(uid, cid)
            await self.say(uid, "Предложение отправлено. Администратор найдёт его в очереди модерации." if ok
                           else "Запись недоступна, уже предложена или опубликована.")
        elif action == "remove":
            ok = self.store.library_delete(uid, cid)
            await self.say(uid, "Личная запись и её необработанное предложение удалены. "
                           "Опубликованная копия в общем банке сохранена." if ok else "Запись не найдена.")
        elif action in ("approve", "reject", "unpublish"):
            if uid not in self.admins:
                await self.say(uid, "Это действие доступно только администраторам.")
                return
            if action == "unpublish":
                ok = self.store.unpublish(cid)
            else:
                ok = self.store.moderate(cid, uid, action == "approve")
            await self.say(uid, ("Опубликовано в общем банке." if action == "approve" else
                           "Снято с публикации." if action == "unpublish" else "Предложение отклонено.")
                           if ok else "Запись уже рассмотрена или недоступна.")
