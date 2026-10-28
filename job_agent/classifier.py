"""Предварительный отбор вакансий через OpenAI Decisions API: вероятность «подходит под criteria.md» и уровень."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .sources import Vacancy

# Повторы при временных сбоях API (503, обрыв связи, лимит запросов в минуту).
RETRIES = 2
RETRY_DELAY = 5.0  # секунд, растёт с каждой попыткой

def build_questions(criteria: str) -> list[dict]:
    """Один вопрос — «вакансия подходит под все критерии из criteria.md».
    Вопроса об уровне должности нет — экономия ~20% токенов на запрос."""
    return [
        {'type': 'predicate', 'name': 'matches',
         'instructions': ('Вакансия (текст на украинском или русском) подходит под ВСЕ критерии ниже.\n\n'
                          f'{criteria}')},
    ]


class ClassifierError(RuntimeError):
    """API классификатора не ответил — работу нужно остановить."""


@dataclass
class Verdict:
    probability: float | None  # None — классификатор не ответил (ошибка или отказ)
    error: str = ''

    def decision(self, keep: float, drop: float) -> str:
        """keep — сохранить сразу, drop — отбросить, review — спорная (сохранить с пометкой или отбросить)."""
        if self.probability is None:
            return 'review'
        if self.probability >= keep:
            return 'keep'
        if self.probability <= drop:
            return 'drop'
        return 'review'


def card_text(v: Vacancy) -> str:
    return f'Должность: {v.title}\nКомпания: {v.company}\nЗарплата: {v.salary or "не указана"}\nОписание: {v.snippet}'


class DecisionsClassifier:
    def __init__(self, criteria: str, model: str = 'gpt-6-luna', keep: float = 0.8, drop: float = 0.2,
                 workers: int = 8):
        import openai  # импорт тут: классификатор необязателен

        self.openai = openai
        # Повторы SDK выключены: он повторяет и 429 «нет кредитов». Временные сбои повторяем сами — см. transient().
        self.client = openai.OpenAI(max_retries=0)
        self.questions = build_questions(criteria)
        self.model, self.keep, self.drop, self.workers = model, keep, drop, workers

    def transient(self, exc: Exception) -> bool:
        """Временный сбой (5xx, сеть, таймаут, лимит запросов в минуту) — есть смысл повторить.
        Нет кредитов, неверный ключ/модель, ошибка запроса — повтор не поможет."""
        if isinstance(exc, self.openai.RateLimitError):
            text = f'{getattr(exc, "code", "")} {exc}'.lower()
            return not any(w in text for w in ('insufficient_quota', 'quota', 'credits'))
        return isinstance(exc, (self.openai.InternalServerError, self.openai.APIConnectionError))

    def classify(self, v: Vacancy) -> Verdict:
        for attempt in range(RETRIES + 1):
            try:
                resp = self.client.decisions.create(model=self.model, input=card_text(v), questions=self.questions)
                break
            except self.openai.APIError as exc:
                if attempt < RETRIES and self.transient(exc):
                    time.sleep(RETRY_DELAY * (attempt + 1))  # растущая пауза: 5, 10 секунд
                    continue
                # Сбой не временный или повторы кончились — остановка всей работы, а не ошибка на каждую вакансию.
                raise ClassifierError(f'{type(exc).__name__}: {exc}'[:300]) from exc
        answers = {a.name: a for a in resp.answers}
        pred = answers.get('matches')
        if pred is None or pred.type == 'refusal':
            return Verdict(None, error='классификатор отказался отвечать')
        return Verdict(pred.probability)

    def classify_many(self, vacancies: list[Vacancy]) -> dict[str, Verdict]:
        """Один запрос на вакансию, параллельно в несколько потоков.
        Первая вакансия — отдельным запросом: если API недоступен, падаем после одного вызова, а не после пачки."""
        if not vacancies:
            return {}
        first = self.classify(vacancies[0])
        pool = ThreadPoolExecutor(self.workers)
        try:
            rest = list(pool.map(self.classify, vacancies[1:]))
        finally:
            pool.shutdown(cancel_futures=True)  # при ошибке не отправлять оставшиеся запросы
        return dict(zip((v.key for v in vacancies), [first, *rest]))
