import json
import re
import httpx


class ProviderError(Exception):
    """Only safe user-facing text. Never include response bodies/URLs/keys."""


class ModelUnavailable(ProviderError):
    pass


BASES = {"groq": "https://api.groq.com/openai/v1"}
GEMINI = "https://generativelanguage.googleapis.com/v1beta"


def model_id(value):
    if not re.fullmatch(r"[a-zA-Z0-9._/-]{1,120}", value) or ".." in value:
        raise ProviderError("Некорректное имя модели. Используй /models.")
    return value.removeprefix("models/")


def parse_json(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
    except (ValueError, TypeError):
        raise ProviderError("Модель вернула неверный формат. Попробуй ещё раз или выбери другую модель.") from None
    if not isinstance(result, dict):
        raise ProviderError("Модель вернула неверный формат JSON.")
    return result


class Provider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def request(self, method, url, **kwargs):
        try:
            r = await self.client.request(method, url, timeout=75, **kwargs)
        except httpx.HTTPError:
            raise ProviderError("Провайдер не ответил. Попробуй позже.") from None
        if r.status_code in (401, 403):
            raise ProviderError("Провайдер отклонил доступ. Проверь ключ, регион и разрешения модели.")
        if r.status_code == 429:
            raise ProviderError("Лимит провайдера исчерпан. Подожди или проверь квоту своего ключа.")
        if r.status_code == 404:
            raise ModelUnavailable("Эта модель больше недоступна. Выбери актуальную через /models.")
        if r.status_code in (500, 502, 503, 504):
            raise ProviderError("Сервис модели временно недоступен. Попробуй позже или выбери другую модель через /models. "
                                "Переподключать ключ не требуется.")
        if not r.is_success:
            raise ProviderError(f"Провайдер вернул ошибку {r.status_code}. Проверь модель и доступ к API.")
        try:
            return r.json()
        except ValueError:
            raise ProviderError("Провайдер вернул нечитаемый ответ.") from None

    async def models(self, provider, key):
        if provider == "gemini":
            data = await self.request("GET", f"{GEMINI}/models", headers={"x-goog-api-key": key},
                                      params={"pageSize": 1000})
            names = [m["name"].removeprefix("models/") for m in data.get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])
                     and "gemini" in m.get("name", "")]
        elif provider in BASES:
            data = await self.request("GET", f"{BASES[provider]}/models", headers={"Authorization": f"Bearer {key}"})
            names = [m["id"] for m in data.get("data", [])]
        else:
            raise ProviderError("Выбери Gemini или Groq.")
        names = [n for n in names if not any(x in n.lower() for x in
                 ("audio", "image", "vision", "tts", "whisper", "embedding", "guard", "live", "robotics"))]
        # Prefer fast text models, while always discovering real model IDs from the user's API.
        def rank(n):
            low = n.lower()
            version = re.search(r"gemini-(\d+)(?:\.(\d+))?", low)
            return (0 if low == "gemini-3.5-flash-lite" else 1 if low == "gemini-flash-lite-latest" else 2,
                    any(x in low for x in ("preview", "experimental", "exp-")),
                    not any(x in low for x in ("flash", "70b", "fast", "120b")),
                    "lite" in low, -int(version[1]) if version else 0,
                    -int(version[2] or 0) if version else 0, n)
        return sorted(set(names), key=rank)

    async def complete(self, provider, key, model, system, user, schema=None):
        model = model_id(model)
        if provider == "gemini":
            config = {"responseMimeType": "application/json", "maxOutputTokens": 4096}
            if schema:
                config["responseJsonSchema"] = schema
            if model.startswith("gemini-3") or model in ("gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest"):
                config["thinkingConfig"] = {"thinkingLevel": "low"}
            data = await self.request("POST", f"{GEMINI}/models/{model}:generateContent",
                headers={"x-goog-api-key": key}, json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": config,
                })
            try:
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            except (KeyError, IndexError, TypeError):
                raise ProviderError("Модель не выдала текст. Попробуй другую тему или модель.") from None
        elif provider in BASES:
            data = await self.request("POST", f"{BASES[provider]}/chat/completions",
                headers={"Authorization": f"Bearer {key}"}, json={
                    "model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "response_format": {"type": "json_object"}, "max_completion_tokens": 4096,
                })
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise ProviderError("Модель не выдала текст.") from None
        else:
            raise ProviderError("Неизвестный провайдер.")
        return parse_json(text)
