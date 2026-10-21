"""Конвейер: сбор → классификатор (Decisions API) → CSV. Решение «подходит / нет» по criteria.md."""
from __future__ import annotations

from datetime import datetime

from .sources import Fetcher, SourceError, search
from .storage import make_row

# Что делать со спорными (вероятность между порогами): сохранить с пометкой «проверить» или отбросить.
REVIEW_ACTIONS = ('save', 'drop')


def run_classifier(store, cities: list[str], sources: list[str], now: datetime, classifier, *, fetcher: Fetcher,
                   max_pages: int = 30, review: str = 'save', verbose: bool = False) -> tuple[list[str], dict]:
    """Возвращает (строки отчёта по каждому сайту и городу, общие счётчики)."""
    known = store.known_urls()
    total = {'found': 0, 'saved': 0, 'review': 0, 'dropped': 0, 'errors': 0}
    report = []
    for source in sources:
        for city in cities:
            try:
                found = search(fetcher, source, city, now, max_pages)
            except SourceError as exc:
                total['errors'] += 1
                report.append(f'{source} · {city}: ОШИБКА {exc}')
                continue
            fresh = [v for v in found if v.url not in known]
            verdicts = classifier.classify_many(fresh)
            rows, counts = [], {'keep': 0, 'review': 0, 'drop': 0}
            for v in fresh:
                vd = verdicts[v.key]
                decision = vd.decision(classifier.keep, classifier.drop)
                # Без ответа классификатора (сбой, отказ) вакансию не выбрасываем: завтра она уже вне 24 часов.
                if decision == 'review' and review == 'drop' and not vd.error:
                    decision = 'drop'
                known.add(v.url)  # решена — в другом городе/повторно в этот день не классифицируем
                counts[decision] += 1
                if verbose:
                    prob = f'{vd.probability:.2f}' if vd.probability is not None else ' -- '
                    mark = {'keep': '✔', 'review': '?', 'drop': '✘'}[decision]
                    print(f'   {mark} {prob} {v.title} — {v.company}', flush=True)
                if decision == 'drop':
                    continue
                # «Кратко» — начало описания с сайта; спорные помечены для ручной проверки.
                rows.append(make_row(now, v, vd, v.snippet, 'классификатор' if decision == 'keep' else 'проверить'))
            store.append(rows)
            errors = sum(1 for v in fresh if verdicts[v.key].error)
            total['found'] += len(found)
            total['saved'] += counts['keep']
            total['review'] += counts['review']
            total['dropped'] += counts['drop']
            line = (f'{source} · {city}: найдено {len(found)}, новых {len(fresh)} → подходит {counts["keep"]}, '
                    f'проверить {counts["review"]}, отброшено {counts["drop"]}')
            if errors:
                line += f' (без ответа классификатора: {errors})'
            report.append(line)
            print(line, flush=True)
    return report, total
