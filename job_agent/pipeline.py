"""Конвейер: сбор → классификатор (Decisions API) → CSV. Решение «подходит / нет» по criteria.md."""
from __future__ import annotations

from datetime import datetime

from .sources import Fetcher, SourceError, search
from .storage import make_row, vacancy_id

# Что делать со спорными (вероятность между порогами): сохранить с пометкой «проверить» или отбросить.
REVIEW_ACTIONS = ('save', 'drop')

# Город, где ноль вакансий бухгалтера за сутки нереален: ноль здесь — признак сбоя источника.
BIG_CITY = 'Киев'


def suspicious_zeros(found_by: dict[tuple[str, str], int], sources: list[str], cities: list[str]) -> list[str]:
    """Предупреждения о «тихом нуле»: сайт ответил без ошибки, но вакансий нет там, где они должны быть.
    found_by: (сайт, город) → найдено; пары с ошибкой источника сюда не попадают."""
    warnings = []
    for source in sources:
        counts = {city: n for (s, city), n in found_by.items() if s == source}
        if len(counts) >= 2 and not any(counts.values()):
            warnings.append(f'{source}: 0 вакансий во всех городах ({", ".join(counts)}) — похоже на сбой источника')
        elif counts.get(BIG_CITY) == 0:
            warnings.append(f'{source}: 0 вакансий в городе {BIG_CITY} — похоже на сбой источника')
    return warnings


def run_classifier(store, cities: list[str], sources: list[str], now: datetime, classifier, *, fetcher: Fetcher,
                   max_pages: int = 30, review: str = 'save', verbose: bool = False) -> tuple[list[str], dict]:
    """Возвращает (строки отчёта по каждому сайту и городу, общие счётчики).
    total['problems'] — ошибки источников и предупреждения о подозрительном нуле, для уведомления."""
    known = store.known_ids()
    total = {'found': 0, 'saved': 0, 'review': 0, 'dropped': 0, 'errors': 0, 'problems': []}
    found_by: dict[tuple[str, str], int] = {}
    report = []
    for source in sources:
        for city in cities:
            try:
                found = search(fetcher, source, city, now, max_pages)
            except SourceError as exc:
                total['errors'] += 1
                line = f'{source} · {city}: ОШИБКА {exc}'
                total['problems'].append(line)
                report.append(line)
                print(line, flush=True)
                continue
            found_by[(source, city)] = len(found)
            fresh = [v for v in found if vacancy_id(v.url) not in known]
            verdicts = classifier.classify_many(fresh)
            rows, counts = [], {'keep': 0, 'review': 0, 'drop': 0}
            for v in fresh:
                vd = verdicts[v.key]
                decision = vd.decision(classifier.keep, classifier.drop)
                # Без ответа классификатора (сбой, отказ) вакансию не выбрасываем: завтра она уже вне 24 часов.
                if decision == 'review' and review == 'drop' and not vd.error:
                    decision = 'drop'
                known.add(vacancy_id(v.url))  # решена — в другом городе/повторно в этот день не классифицируем
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
                total['problems'].append(line)  # лимиты/сбои API: вакансии сохранены как «проверить»
            report.append(line)
            print(line, flush=True)
    for warning in suspicious_zeros(found_by, sources, cities):
        total['problems'].append(warning)
        print(f'ПРЕДУПРЕЖДЕНИЕ: {warning}', flush=True)
    return report, total
