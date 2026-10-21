"""Хранилище результатов: Google Sheets (основное) и CSV (для локальной проверки без ключей)."""
from __future__ import annotations

import csv
from pathlib import Path

HEADER = ['Найдено', 'Опубликовано', 'Сайт', 'Город', 'Должность', 'Компания', 'Зарплата', 'Уровень', 'Вероятность',
          'Кратко', 'Решил', 'Ссылка']


def url_column(header: list[str]) -> int:
    """Колонку ссылки ищем по заголовку: старые файлы могли иметь другой набор колонок."""
    return header.index('Ссылка') if 'Ссылка' in header else len(header) - 1


def make_row(now, vac, verdict, summary: str, decided_by: str) -> list[str]:
    """Строка CSV/таблицы. verdict — Verdict классификатора или None, если решал не он."""
    prob = f'{verdict.probability:.2f}' if verdict is not None and verdict.probability is not None else ''
    return [now.strftime('%Y-%m-%d %H:%M'), vac.posted.replace('T', ' ')[:16], vac.source, vac.city, vac.title,
            vac.company, vac.salary, verdict.level if verdict else '', prob, summary.strip(), decided_by, vac.url]


def safe(value: str) -> str:
    """Текст с сайтов не должен исполняться как формула в таблице."""
    return "'" + value if value[:1] in ('=', '+', '-', '@') else value


class GoogleSheet:
    def __init__(self, spreadsheet_id: str, credentials_file: str, worksheet: str = 'Вакансии'):
        import gspread  # импорт здесь, чтобы --dry-run работал без настроенного Google

        book = gspread.service_account(filename=credentials_file).open_by_key(spreadsheet_id)
        try:
            self.ws = book.worksheet(worksheet)
        except gspread.WorksheetNotFound:
            self.ws = book.add_worksheet(worksheet, rows=1000, cols=len(HEADER))
        if self.ws.row_values(1) != HEADER:
            self.ws.update([HEADER], 'A1')
            self.ws.freeze(rows=1)

    def known_urls(self) -> set[str]:
        header = self.ws.row_values(1)
        return set(self.ws.col_values(url_column(header) + 1)[1:])

    def append(self, rows: list[list[str]]) -> None:
        if rows:
            # RAW — значения пишутся как есть, без интерпретации формул.
            self.ws.append_rows([[safe(c) for c in r] for r in rows], value_input_option='RAW')


class CsvStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def known_urls(self) -> set[str]:
        if not self.path.exists():
            return set()
        with self.path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
        if not rows:
            return set()
        col = url_column(rows[0])
        return {row[col] for row in rows[1:] if len(row) > col}

    def append(self, rows: list[list[str]]) -> None:
        new = not self.path.exists()
        if not new:
            with self.path.open(encoding='utf-8-sig', newline='') as f:
                header = next(csv.reader(f), [])
            if header and header != HEADER:
                raise RuntimeError(f'{self.path}: старый набор колонок — переименуйте файл, будет создан новый')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8-sig' if new else 'utf-8', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(HEADER)
            w.writerows([[safe(c) for c in r] for r in rows])


SNAPSHOT_HEADER = ['Сайт', 'Город', 'Опубликовано', 'Должность', 'Компания', 'Зарплата', 'Кратко', 'Ссылка']
VERDICT_HEADER = ['Вероятность', 'Уровень', 'Решение', 'Ошибка']


def write_snapshot(path: str, items: list, with_verdicts: bool) -> None:
    """Снимок проверки (--check --save): все найденные вакансии, без отбора. Файл перезаписывается.
    items: (вакансия, Verdict или None, решение); с with_verdicts добавляются колонки классификатора."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(SNAPSHOT_HEADER + (VERDICT_HEADER if with_verdicts else []))
        for vac, verdict, decision in items:
            row = [vac.source, vac.city, vac.posted.replace('T', ' ')[:16], vac.title, vac.company, vac.salary,
                   vac.snippet, vac.url]
            if with_verdicts:
                prob = f'{verdict.probability:.2f}' if verdict.probability is not None else ''
                row += [prob, verdict.level, decision, verdict.error]
            w.writerow([safe(c) for c in row])
