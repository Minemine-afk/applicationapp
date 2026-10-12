"""Minimal Telegram Bot API client (https://core.telegram.org/bots/api)."""

from __future__ import annotations

import json
from typing import Any

import httpx

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, http: httpx.Client | None = None) -> None:
        self._base = f"{API}/bot{token}"
        self._http = http or httpx.Client(timeout=70)

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        return self._post(method, json=params)

    def _post(self, method: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.post(f"{self._base}/{method}", **kwargs)
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None   # keep token out of logs
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or resp.status_code}")
        return data["result"]

    def send_photo(self, chat_id: int, png: bytes, caption: str,
                   links: list[tuple[str, str]] | None = None) -> dict:
        """A PNG with an HTML caption and optional link buttons [(label, https-url)]."""
        data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        if links:
            data["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": label, "url": url}] for label, url in links]})
        return self._post("sendPhoto", data=data, files={"photo": ("qr.png", png, "image/png")})

    def send_document(self, chat_id: int, name: str, content: bytes, caption: str = "",
                      mime: str = "text/html") -> dict:
        """A file (e.g. a saved page) with an HTML caption."""
        data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        return self._post("sendDocument", data=data, files={"document": (name, content, mime)})

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None,
             ) -> dict:
        markup = None
        if buttons:
            markup = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row] for row in buttons
            ]}
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=markup)

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace a message's text and drop its buttons."""
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                  parse_mode="HTML", disable_web_page_preview=True)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:
            # A tap handled after a busy spell is past Telegram's 15 s window to answer it.
            # The tap itself was still processed, so there's nothing to report.
            if "query is too old" not in str(e):
                raise

    def updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])
