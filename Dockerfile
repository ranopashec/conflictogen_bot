FROM python:3.13-slim AS dependencies
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATABASE_PATH=/app/data/bot.sqlite3
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m bot && mkdir /app/data && chown bot:bot /app/data
COPY conflict_bot ./conflict_bot
COPY examples.json ./

FROM dependencies AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY pyproject.toml ./
RUN python -m pytest -q && touch /tmp/tests-passed

FROM dependencies AS runtime
# This dependency makes a successful test stage mandatory for every production build.
COPY --from=test /tmp/tests-passed /app/tests-passed
USER bot
CMD ["python", "-m", "conflict_bot.bot"]
