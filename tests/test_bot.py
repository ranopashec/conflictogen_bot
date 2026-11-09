import asyncio
import json

from cryptography.fernet import Fernet
import httpx
import pytest

from conflict_bot.bot import Bot, normalize_key
from conflict_bot.generator import Generator, load_bank, overlap, validate
from conflict_bot.providers import Provider, ProviderError, ModelUnavailable, model_id, parse_json
from conflict_bot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "bot.db"), Fernet.generate_key().decode())


@pytest.fixture
def case():
    return dict(
        title="Ключ от мастерской",
        situation="Две подруги вместе арендуют мастерскую и заранее договорились, что каждая пользуется ею "
        "только в свои дни. Одна из них без согласования пообещала знакомому провести там занятие "
        "в субботу, когда вторая готовит собственную выставку. Узнав об этом, вторая напомнила "
        "о расписании и отказалась освобождать помещение. Во время разговора наедине первая подруга "
        "продолжает настаивать на своём праве распоряжаться мастерской.",
        attack="Подруга, пообещавшая помещение знакомому", defense="Подруга, готовящая выставку",
        boundary="Право пользоваться мастерской в согласованное время",
        ethical_asymmetry="Атака распорядилась чужим согласованным временем и использует отказ как повод для давления.",
        mode="личный", mechanism="несогласованное обещание")


def configure(store, uid=1):
    store.configure(uid, "groq", "gsk_private_test_key_123456789", "test-model")
    store.setting(uid, "mode", "личный")


def test_credentials_encrypted_and_isolated(store):
    configure(store)
    configure(store, 2)
    assert store.key(1) == "gsk_private_test_key_123456789"
    assert b"gsk_private_test" not in store.db.execute("SELECT secret FROM users WHERE id=1").fetchone()[0]
    store.forget_key(1)
    assert store.key(1) is None
    assert store.key(2)


def test_ownership_delete_and_rate_limit(store, case):
    configure(store)
    cid = store.add_case(1, case)
    assert store.case(2, cid) is None
    store.rate(2, cid, -1)
    assert store.db.execute("SELECT rating FROM cases WHERE id=?", (cid,)).fetchone()[0] is None
    assert store.reserve(1, 1)
    assert not store.reserve(1, 1)
    assert store.reserve(2, 1)
    store.erase(1)
    assert store.recent(1) == []
    assert store.key(1) is None


def test_real_bank_and_duplicate_detector():
    bank = load_bank()
    assert len(bank) == 41
    assert all(x["situation"] and "Опубликовано" not in x["situation"] for x in bank)
    assert overlap(bank[0]["situation"], bank[0]["situation"]) == 1


def test_father_son_use_consistent_roles(case):
    case.update(attack="Отец", defense="Сын", situation=(
        "Взрослый сын решил самостоятельно перестелить пол на своей даче и закупил материалы. "
        "Отец приезжает, осматривает доски и требует выбрать другую древесину. Сын объясняет, "
        "что посоветовался со специалистом, и отказывается менять план. Отец недоволен отказом сына."))
    assert validate(case, "личный")["defense"] == "Сын"
    case["situation"] = case["situation"].replace("Взрослый сын", "Муж", 1)
    with pytest.raises(ValueError, match="муж без"):
        validate(case, "личный")


@pytest.mark.parametrize("ending", ["на что отец", "на что отец говорит:", "на что отец говорит.", "на что слышит:"])
def test_unfinished_situation_rejected(case, ending):
    case["situation"] = case["situation"].rstrip(".") + ", " + ending
    with pytest.raises(ValueError, match="ending"):
        validate(case, "личный")


async def test_case_output_is_only_situation(store, case):
    bot = LocalBot(store)
    await bot.show_case(1, 1, case)
    assert bot.sent[-1][1] == case["situation"]


class FakeProvider:
    def __init__(self, case, passes=True):
        self.case, self.passes, self.calls = case, passes, []

    async def complete(self, provider, key, model, system, user, schema=None):
        self.calls.append((system, user))
        if "Ты строгий редактор" in user:
            return {"pass": self.passes, "issues": [] if self.passes else ["Атака этически права"]}
        return dict(self.case)


async def test_generator_enforces_school_rules_and_saves(store, case):
    configure(store)
    provider = FakeProvider(case)
    cid, result = await Generator(provider, store).generate(1)
    assert store.case(1, cid)["situation"] == case["situation"]
    assert "этически неправа" in provider.calls[0][0]
    assert "игрок атаки выбирает сам" in provider.calls[0][0]
    assert len(provider.calls) == 2
    assert "leverage" not in result and "directions" not in result


async def test_failed_ethical_review_never_published(store, case):
    configure(store)
    provider = FakeProvider(case, False)
    with pytest.raises(ProviderError, match="не прошли"):
        await Generator(provider, store).generate(1)
    assert len(provider.calls) == 4
    assert store.recent(1) == []


async def test_copy_of_school_case_rejected(store, case):
    configure(store)
    case["situation"] = load_bank()[0]["situation"]
    provider = FakeProvider(case)
    with pytest.raises(ProviderError):
        await Generator(provider, store).generate(1)
    assert store.recent(1) == []


async def test_retired_model_is_replaced_once(store, case):
    configure(store)
    class RetiredProvider(FakeProvider):
        async def models(self, provider, key):
            return ["current-model"]
        async def complete(self, provider, key, model, system, user, schema=None):
            if model == "test-model":
                raise ModelUnavailable("retired")
            return await super().complete(provider, key, model, system, user, schema=schema)
    cid, result = await Generator(RetiredProvider(case), store).generate(1)
    assert result["model"] == "current-model"
    assert store.user(1)["model"] == "current-model"


async def test_provider_requests_use_correct_auth_without_secret_in_url():
    seen = []
    def transport(req):
        seen.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-test-flash",
                "supportedGenerationMethods": ["generateContent"]}]})
        if "googleapis" in str(req.url):
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"pass":true}'}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"pass":true}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p = Provider(client)
        assert await p.models("gemini", "secret-value") == ["gemini-test-flash"]
        assert (await p.complete("gemini", "secret-value", "gemini-test-flash", "s", "u"))["pass"]
        assert (await p.complete("groq", "secret-value", "test", "s", "u"))["pass"]
    assert all("secret-value" not in str(r.url) for r in seen)
    assert seen[0].headers["x-goog-api-key"] == "secret-value"
    assert seen[-1].headers["authorization"] == "Bearer secret-value"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_provider_errors_never_echo_credentials(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(status, text="secret-value"))) as client:
        with pytest.raises(ProviderError) as error:
            await Provider(client).models("groq", "secret-value")
        assert "secret-value" not in str(error.value)


def test_invalid_json_and_model_paths():
    assert parse_json('```json\n{"pass":true}\n```')["pass"]
    for text in ["not json", "[]"]:
        with pytest.raises(ProviderError):
            parse_json(text)
    for model in ["../other", "https://attacker/model", "", "a?key=secret"]:
        with pytest.raises(ProviderError):
            model_id(model)


class LocalBot(Bot):
    def __init__(self, store):
        super().__init__("123:unused", store, None)
        self.sent, self.deleted = [], []

    async def say(self, uid, text, markup=None):
        self.sent.append((uid, text))

    async def api(self, method, **payload):
        if method == "deleteMessage":
            self.deleted.append(payload)
        return True


def update(uid, text, private=True):
    return {"update_id": 1, "message": {"message_id": 3, "from": {"id": uid},
        "chat": {"id": uid if private else -123, "type": "private" if private else "group"}, "text": text}}


async def test_no_keys_in_groups_and_private_key_deleted(store):
    bot = LocalBot(store)
    class Models:
        async def models(self, provider, key):
            return ["test-model"]
    bot.provider = Models()
    await bot.handle(update(1, "/connect", False))
    assert not bot.sent and not bot.pending
    await bot.begin_key(1, "groq")
    key = "gsk_private_test_key_123456789"
    await bot.handle(update(1, key))
    assert bot.deleted == [{"chat_id": 1, "message_id": 3}]
    assert store.key(1) == key
    assert all(key not in msg for _, msg in bot.sent)


@pytest.mark.parametrize("wrapper", [
    '{key}', '"{key}"', '“{key}”', '`{key}`', 'GROQ_API_KEY="{key}"',
    'export GROQ_API_KEY=\'{key}\'', 'Ключ: {key}', 'Вот ключ:\n{key}',
    '\u200b{key}\u200e', '```\n{key}\n```',
])
def test_key_clipboard_wrappers(wrapper):
    key = "gsk_test1234567890_abcdef"
    assert normalize_key(wrapper.format(key=key)) == key


async def test_malformed_key_can_be_retried_without_connect(store):
    bot = LocalBot(store)
    class Models:
        async def models(self, provider, key):
            return ["test-model"]
    bot.provider = Models()
    await bot.begin_key(1, "groq")
    await bot.handle(update(1, "это не ключ"))
    assert 1 in bot.pending
    await bot.handle(update(1, 'GROQ_API_KEY="gsk_private_test_key_123456789"'))
    assert store.key(1) == "gsk_private_test_key_123456789"
    assert 1 not in bot.pending


async def test_delete_cancels_inflight_generation(store, case):
    configure(store)
    bot = LocalBot(store)
    entered = asyncio.Event()
    class SlowGenerator:
        async def generate(self, uid):
            entered.set()
            await asyncio.sleep(100)
            return store.add_case(uid, case), case
    bot.generator = SlowGenerator()
    await bot.start_generation(1)
    await entered.wait()
    await bot.handle(update(1, "/delete_me"))
    assert 1 not in bot.jobs
    assert not store.recent(1)
    assert store.key(1) is None


async def test_callback_cannot_access_another_users_case(store, case):
    cid = store.add_case(1, case)
    bot = LocalBot(store)
    await bot.handle({"callback_query": {"id": "abc", "from": {"id": 2},
        "message": {"chat": {"id": 2, "type": "private"}}, "data": f"explain:{cid}"}})
    assert "не найден" in bot.sent[-1][1]
    assert case["boundary"] not in bot.sent[-1][1]
