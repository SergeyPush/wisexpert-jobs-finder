from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import CITIES, KYIV, SOURCES, env, load_criteria, load_dotenv
from .sources import Fetcher
from .notify import notify, send_telegram, telegram_configured
from .storage import CsvStore, GoogleSheet, write_snapshot

MARKS = {'keep': '✔', 'drop': '✘', 'review': '?'}
DECISIONS = {'keep': 'подходит', 'drop': 'отброшена', 'review': 'проверить'}


def make_classifier():
    """Классификатор OpenAI Decisions API с критериями из criteria.md и порогами из .env."""
    if not env('OPENAI_API_KEY'):
        raise SystemExit('Нужен OPENAI_API_KEY в .env')
    from .classifier import DecisionsClassifier

    return DecisionsClassifier(load_criteria(), model=env('DECISIONS_MODEL', 'gpt-6-luna'),
                               keep=float(env('DECISION_KEEP', '0.8')), drop=float(env('DECISION_DROP', '0.2')))


def check_sources(args, now: datetime) -> int:
    """Проверка без сохранения: сколько вакансий видно в каждом источнике.
    С --classify ещё и показывает решения классификатора — для подбора критериев и порогов.
    С --save пишет все найденные вакансии (и решения, если --classify) в CSV — для сравнения «до и после»."""
    from .pipeline import search_all

    fetcher = Fetcher(delay=args.delay, proxy=env('PROXY_URL'))
    classifier = make_classifier() if args.classify else None
    snapshot = []  # (вакансия, Verdict или None, решение)
    failed = False
    for source, city, found, exc in search_all(fetcher, args.sources, args.cities, now, args.max_pages,
                                               args.retry_wait):
        if exc is not None:
            failed = True
            print(f'{source} · {city}: ОШИБКА {exc}')
            continue
        print(f'{source} · {city}: {len(found)} за 24ч')
        if classifier is None:
            snapshot += [(v, None, '') for v in found]
            for v in found[:3]:
                print(f'   {v.posted} {v.title} — {v.company}')
            continue
        try:
            verdicts = classifier.classify_many(found)
        except Exception as exc:  # неверный ключ, нет доступа к модели — дальше проверять бессмысленно
            print(f'Ошибка классификатора ({type(exc).__name__}): {exc}', file=sys.stderr)
            return 1
        for v in sorted(found, key=lambda v: -(verdicts[v.key].probability or 0)):
            vd = verdicts[v.key]
            decision = vd.decision(classifier.keep, classifier.drop)
            snapshot.append((v, vd, DECISIONS[decision]))
            prob = f'{vd.probability:.2f}' if vd.probability is not None else ' -- '
            print(f'   {MARKS[decision]} {prob} {vd.level or vd.error:<14} {v.title}')
    if args.save:
        write_snapshot(args.save, snapshot, with_verdicts=classifier is not None)
        print(f'Сохранено {len(snapshot)} вакансий → {Path(args.save).resolve()}')
    return 1 if failed else 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description='Поиск вакансий бухгалтера за 24 часа (work.ua, robota.ua) '
                                                 'с отбором по criteria.md через OpenAI Decisions API')
    parser.add_argument('--cities', nargs='+', choices=CITIES, default=list(CITIES))
    parser.add_argument('--sources', nargs='+', choices=SOURCES, default=list(SOURCES))
    parser.add_argument('--output', default=env('OUTPUT_CSV', 'jobs.csv'), help='CSV-файл с результатами')
    parser.add_argument('--sheets', action='store_true', help='Писать в Google Sheets вместо CSV')
    parser.add_argument('--max-pages', type=int, default=30, help='Лимит страниц выдачи на сайт и город')
    parser.add_argument('--delay', type=float, default=6, help='Пауза между запросами к сайтам, секунды')
    parser.add_argument('--retry-wait', type=float, default=150,
                        help='Через сколько секунд повторить источники с ошибкой (0 — без повтора)')
    parser.add_argument('--check', action='store_true', help='Только проверить сбор, без сохранения')
    parser.add_argument('--classify', action='store_true', help='С --check: показать решения классификатора')
    parser.add_argument('--save', metavar='CSV', help='С --check: сохранить все найденные вакансии (и решения) в CSV')
    parser.add_argument('--test-telegram', action='store_true', help='Отправить тестовое сообщение в Telegram')
    parser.add_argument('-v', '--verbose', action='store_true', help='Печатать решение по каждой вакансии')
    args = parser.parse_args()
    now = datetime.now(KYIV)

    if args.test_telegram:
        if not telegram_configured():
            parser.error('задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env')
        ok = send_telegram('✅ Поиск вакансий: уведомления настроены')
        print('Отправлено' if ok else 'Не отправлено — см. ошибку выше')
        return 0 if ok else 1
    if args.save and not args.check:
        parser.error('--save работает только с --check; результаты поиска пишутся в --output')
    if args.check:
        return check_sources(args, now)

    code, problems, summary = search_and_save(args, now, parser)
    notify(problems, summary)
    return code


def search_and_save(args, now: datetime, parser) -> tuple[int, list[str], str]:
    """Основной запуск. Возвращает (код выхода, проблемы для уведомления, итоговая строка)."""
    from .pipeline import REVIEW_ACTIONS, run_classifier  # импорт тут: --check не требует openai

    def fail(message: str) -> tuple[int, list[str], str]:
        print(message, file=sys.stderr)
        return 1, [message], ''

    review = env('REVIEW_ACTION', 'save').lower()
    if review not in REVIEW_ACTIONS:
        parser.error(f'REVIEW_ACTION должен быть одним из: {", ".join(REVIEW_ACTIONS)}')
    if args.sheets:
        sheet_id, creds = env('GOOGLE_SHEET_ID'), env('GOOGLE_CREDENTIALS', 'service-account.json')
        if not sheet_id:
            parser.error('для --sheets задайте GOOGLE_SHEET_ID в .env')
        try:
            # Старые листы через запятую — для поиска дубликатов, туда ничего не пишется.
            archive = [n.strip() for n in env('GOOGLE_ARCHIVE_WORKSHEETS').split(',') if n.strip()]
            store, target = GoogleSheet(sheet_id, creds, env('GOOGLE_WORKSHEET', 'Вакансии'), archive), 'Google Sheets'
        except Exception as exc:  # нет доступа, неверный ID, несовместимые колонки
            return fail(f'Google Sheets ({type(exc).__name__}): {exc}')
    else:
        store, target = CsvStore(args.output), str(Path(args.output).resolve())

    try:
        classifier = make_classifier()
    except SystemExit as exc:  # нет ключа — об этом тоже нужно узнать из уведомления
        return fail(str(exc))
    print(f'Классификатор: {classifier.model}, пороги {classifier.keep}/{classifier.drop}, '
          f'спорные: {"сохранять с пометкой «проверить»" if review == "save" else "отбрасывать"}. '
          f'Результаты: {target}', flush=True)
    try:
        store.known_ids()  # несовместимый CSV/лист — ошибка до первых запросов к сайтам и API
    except RuntimeError as exc:
        return fail(str(exc))
    try:
        _, total = run_classifier(store, args.cities, args.sources, now, classifier,
                                  fetcher=Fetcher(delay=args.delay, proxy=env('PROXY_URL')),
                                  max_pages=args.max_pages, review=review, verbose=args.verbose,
                                  retry_wait=args.retry_wait)
    except Exception as exc:  # неверный ключ, нет доступа к модели и т.п.
        return fail(f'Ошибка классификатора ({type(exc).__name__}): {exc}')
    summary = (f"[{now:%Y-%m-%d %H:%M}] Подходит: {total['saved']}, проверить: {total['review']}, "
               f"отброшено: {total['dropped']} из {total['found']} → {target}")
    print(summary)
    return (1 if total['errors'] else 0), total['problems'], summary


if __name__ == '__main__':
    sys.exit(main())
