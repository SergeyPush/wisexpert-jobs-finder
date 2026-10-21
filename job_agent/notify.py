"""Уведомления в Telegram о проблемах запуска (и, по желанию, ежедневная сводка)."""
from __future__ import annotations

import sys

from curl_cffi import requests

from .config import env

LIMIT = 4000  # Telegram принимает до 4096 символов в сообщении


def telegram_configured() -> bool:
    return bool(env('TELEGRAM_BOT_TOKEN') and env('TELEGRAM_CHAT_ID'))


def send_telegram(text: str) -> bool:
    """Отправляет сообщение; любая ошибка только печатается — уведомление не должно ронять запуск."""
    token, chat_id = env('TELEGRAM_BOT_TOKEN'), env('TELEGRAM_CHAT_ID')
    if not (token and chat_id):
        return False
    if len(text) > LIMIT:
        text = text[:LIMIT] + '\n…'
    try:
        resp = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}, timeout=20)
        if resp.status_code != 200:
            # Тело ответа Telegram содержит причину (неверный chat_id и т.п.), токена в нём нет.
            print(f'Telegram: HTTP {resp.status_code}: {resp.text[:200]}', file=sys.stderr)
            return False
        return True
    except requests.RequestsError as exc:
        print(f'Telegram: {type(exc).__name__}', file=sys.stderr)  # без текста: в нём может быть URL с токеном
        return False


def notify(problems: list[str], summary: str) -> None:
    """Шлёт сообщение, если есть проблемы, либо всегда при TELEGRAM_NOTIFY=always."""
    mode = env('TELEGRAM_NOTIFY', 'problems').lower()
    if not problems and mode != 'always':
        return
    head = '⚠️ Поиск вакансий: есть проблемы' if problems else '✅ Поиск вакансий'
    body = '\n'.join(f'• {p}' for p in problems)
    send_telegram('\n\n'.join(part for part in (head, body, summary) if part))
