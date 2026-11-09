from cryptography.fernet import Fernet
import pytest

from conflict_bot.store import Store
from test_bot import LocalBot, update


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "bank.db"), Fernet.generate_key().decode())


TITLE = "Своя мастерская"
TEXT = "Два друга делят мастерскую. Один без согласования занял место второго и отказывается освобождать его."


def callback(uid, data):
    return {"callback_query": {"id": "cb", "from": {"id": uid},
            "message": {"chat": {"id": uid, "type": "private"}}, "data": data}}


def test_private_entries_are_isolated_and_submit_is_owned(store):
    cid = store.library_add(1, TITLE, TEXT)
    assert store.library_entry(2, cid) is None
    assert store.library_page(2) == []
    assert not store.library_delete(2, cid)
    assert not store.submit(2, cid)
    assert store.moderation_page() == []
    assert store.submit(1, cid)
    assert not store.submit(1, cid)


def test_approval_publishes_snapshot_once_and_author_deletion_keeps_public(store):
    cid = store.library_add(1, TITLE, TEXT)
    assert store.submit(1, cid)
    sid = store.moderation_page()[0]["id"]
    assert store.public_page() == []
    assert store.moderate(sid, 99, True)
    assert not store.moderate(sid, 99, True)
    assert len(store.public_page()) == 1
    assert set(store.public_page()[0]) == {"id", "title", "situation"}
    store.erase(1)
    assert store.library_page(1) == []
    assert len(store.public_page()) == 1
    assert store.unpublish(store.public_page()[0]["id"])
    assert store.public_page() == []


def test_deleting_private_entry_withdraws_pending_proposal(store):
    cid = store.library_add(1, TITLE, TEXT)
    store.submit(1, cid)
    assert store.library_delete(1, cid)
    assert store.moderation_page() == []


def test_publications_survive_submission_id_reuse_after_account_deletion(store):
    cid = store.library_add(1, TITLE, TEXT)
    store.submit(1, cid)
    store.moderate(store.moderation_page()[0]["id"], 99, True)
    store.erase(1)
    cid = store.library_add(2, "Другой конфликт", TEXT + " Теперь новая ситуация.")
    store.submit(2, cid)
    store.moderate(store.moderation_page()[0]["id"], 99, True)
    assert len(store.public_page()) == 2


async def test_non_admin_cannot_read_queue_or_approve_forged_callback(store):
    bot = LocalBot(store)
    cid = store.library_add(1, TITLE, TEXT)
    store.submit(1, cid)
    sid = store.moderation_page()[0]["id"]
    await bot.handle(callback(2, f"bank:open:review:{sid}"))
    assert "администраторам" in bot.sent[-1][1]
    assert TEXT not in bot.sent[-1][1]
    await bot.handle(callback(2, f"bank:approve:{sid}"))
    assert store.submission(sid)
    await bot.handle(callback(2, f"bank:unpublish:{store.public_page()[0]['id']}"))
    assert len(store.public_page()) == 6


async def test_admin_can_approve_only_submitted_entries(store):
    bot = LocalBot(store)
    bot.admins = {99}
    cid = store.library_add(1, TITLE, TEXT)
    await bot.handle(callback(99, f"bank:open:mine:{cid}"))
    assert "недоступна" in bot.sent[-1][1]
    assert TEXT not in bot.sent[-1][1]
    store.submit(1, cid)
    sid = store.moderation_page()[0]["id"]
    await bot.handle(callback(99, f"bank:approve:{sid}"))
    assert store.public_page()[0]["situation"] == TEXT


async def test_manual_entry_without_api_key_and_seed_idempotent(store):
    bot = LocalBot(store)
    await bot.handle(update(1, "/add"))
    await bot.handle(update(1, TITLE + "\n" + TEXT))
    assert store.library_page(1)[0]["situation"] == TEXT
    assert store.key(1) is None
    assert not store.moderation_page()
    LocalBot(store)
    assert store.db.execute("SELECT count(*) FROM public_cases").fetchone()[0] == 41


async def test_private_entry_callback_cannot_leak_other_authors_text(store):
    bot = LocalBot(store)
    cid = store.library_add(1, TITLE, TEXT)
    await bot.handle(callback(2, f"bank:open:mine:{cid}"))
    assert TEXT not in bot.sent[-1][1]
