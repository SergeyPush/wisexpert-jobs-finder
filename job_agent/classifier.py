"""Предварительный отбор вакансий через OpenAI Decisions API: вероятность «подходит под criteria.md» и уровень."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .sources import Vacancy

LEVEL_QUESTION = {
    'type': 'choice',
    'name': 'level',
    'instructions': 'Уровень бухгалтерской должности в вакансии.',
    'choices': [
        {'value': 'chief', 'description': 'Главный бухгалтер или его заместитель'},
        {'value': 'accountant', 'description': 'Бухгалтер (любой участок), бухгалтер-кассир, бухгалтер-экономист'},
        {'value': 'assistant', 'description': 'Помощник бухгалтера, младший бухгалтер, стажёр'},
        {'value': 'not_accounting', 'description': 'Не бухгалтерская должность'},
    ],
}


def build_questions(criteria: str) -> list[dict]:
    """Главный вопрос — «вакансия подходит под все критерии из criteria.md»; уровень — справочно для CSV."""
    return [
        {'type': 'predicate', 'name': 'matches',
         'instructions': ('Вакансия (текст на украинском или русском) подходит под ВСЕ критерии ниже.\n\n'
                          f'{criteria}')},
        LEVEL_QUESTION,
    ]


LEVELS = {'chief': 'главбух/зам', 'accountant': 'бухгалтер', 'assistant': 'помощник', 'not_accounting': 'не бухгалтер'}


@dataclass
class Verdict:
    probability: float | None  # None — классификатор не ответил (ошибка или отказ)
    level: str = ''
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
        self.client = openai.OpenAI()
        self.questions = build_questions(criteria)
        self.model, self.keep, self.drop, self.workers = model, keep, drop, workers

    def classify(self, v: Vacancy) -> Verdict:
        try:
            resp = self.client.decisions.create(model=self.model, input=card_text(v), questions=self.questions)
        except (self.openai.AuthenticationError, self.openai.NotFoundError, self.openai.PermissionDeniedError):
            raise  # неверный ключ или модель — продолжать бессмысленно
        except self.openai.APIError as exc:
            return Verdict(None, error=f'{type(exc).__name__}: {exc}'[:200])
        answers = {a.name: a for a in resp.answers}
        pred, level = answers.get('matches'), answers.get('level')
        if pred is None or pred.type == 'refusal':
            return Verdict(None, error='классификатор отказался отвечать')
        return Verdict(pred.probability, LEVELS.get(level.choice, '') if level is not None and level.type == 'choice' else '')

    def classify_many(self, vacancies: list[Vacancy]) -> dict[str, Verdict]:
        """Один запрос на вакансию, параллельно в несколько потоков."""
        with ThreadPoolExecutor(self.workers) as pool:
            return dict(zip((v.key for v in vacancies), pool.map(self.classify, vacancies)))
