"""Prepare a local encrypted store configuration; never print secrets."""
import os
from pathlib import Path
from cryptography.fernet import Fernet

root = Path(__file__).resolve().parent
env = root / ".env"
os.umask(0o077)
if not env.exists():
    with env.open("x") as out:
        out.write("TELEGRAM_BOT_TOKEN=\nENCRYPTION_KEY=" + Fernet.generate_key().decode() + "\nDAILY_LIMIT=30\nADMIN_IDS=\n")
    env.chmod(0o600)
    print("Создан conflict-bot/.env с ключом шифрования. Добавь TELEGRAM_BOT_TOKEN из BotFather.")
else:
    print("Файл .env уже существует; значения сохранены.")
