"""Хранилище результатов: Google Sheets (основное) и CSV (для локальной проверки без ключей)."""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path

HEADER = ['Найдено', 'Опубликовано', 'Сайт', 'Город', 'Должность', 'Компания', 'Зарплата', 'Уровень', 'Вероятность',
          'Кратко', 'Решил', 'Ссылка']
# В Google Sheets служебные колонки классификатора не показываются — они остаются только в CSV.
SHEET_HEADER = [c for c in HEADER if c not in ('Уровень', 'Вероятность', 'Решил')]

# Google Sheets иногда не отвечает: без таймаута gspread ждёт бесконечно, один сбой не должен ронять запуск.
SHEETS_TIMEOUT = 60  # секунд на запрос
SHEETS_RETRIES = 3
SHEETS_RETRY_DELAY = 10.0  # секунд, растёт с каждой попыткой


def sheets_transient(exc: Exception) -> bool:
    """Временный сбой Google Sheets: таймаут, обрыв связи, 429/5xx — есть смысл повторить."""
    import requests  # приходит вместе с gspread

    if isinstance(exc, requests.RequestException):  # ReadTimeout, ConnectionError
        return True
    # gspread.APIError несёт ответ сервера; класс не импортируем — в тестах gspread подменён.
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    return status in (429, 500, 502, 503, 504)


def sheets_retry(call):
    """call(attempt) с повторами при временных сбоях Google Sheets; остальные ошибки — сразу наверх."""
    for attempt in range(SHEETS_RETRIES + 1):
        try:
            return call(attempt)
        except Exception as exc:
            if attempt == SHEETS_RETRIES or not sheets_transient(exc):
                raise
            wait = SHEETS_RETRY_DELAY * (attempt + 1)  # 10, 20, 30 секунд
            print(f'Google Sheets: {type(exc).__name__}, повтор через {wait:.0f} с', flush=True)
            time.sleep(wait)


# id вакансии из ссылки — в том же формате, что колонка id старых листов (work_N, robota_N).
ID_PATTERNS = [(re.compile(r'work\.ua/jobs/(\d+)'), 'work_'), (re.compile(r'robota\.ua/.*vacancy(\d+)'), 'robota_')]


def vacancy_id(url: str) -> str:
    """Ключ для дубликатов. В ссылке robota.ua есть id компании — он может смениться, id вакансии нет."""
    for pattern, prefix in ID_PATTERNS:
        if m := pattern.search(url):
            return prefix + m.group(1)
    return url  # незнакомый сайт — сравниваем по ссылке целиком


def make_row(now, vac, verdict, summary: str, decided_by: str) -> list[str]:
    """Строка CSV/таблицы. verdict — Verdict классификатора или None, если решал не он."""
    prob = f'{verdict.probability:.2f}' if verdict is not None and verdict.probability is not None else ''
    return [now.strftime('%Y-%m-%d %H:%M'), vac.posted.replace('T', ' ')[:16], vac.source, vac.city, vac.title,
            vac.company, vac.salary, verdict.level if verdict else '', prob, summary.strip(), decided_by, vac.url]


def safe(value: str) -> str:
    """Текст с сайтов не должен исполняться как формула в таблице (в т.ч. с ведущими пробелами/табами)."""
    if value[:1] in ('\t', '\r') or value.lstrip()[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value


def check_header(header: list[str], where: str, expected: list[str] = HEADER) -> None:
    """Дописывать строки под другой набор колонок нельзя — столбцы перепутаются, сломается дедупликация."""
    if header and header != expected:
        raise RuntimeError(f'{where}: старый набор колонок — переименуйте файл/лист, будет создан новый')


class GoogleSheet:
    def __init__(self, spreadsheet_id: str, credentials_file: str, worksheet: str = 'Вакансии',
                 archive: list[str] = ()):
        """archive — старые листы (только чтение): их вакансии тоже считаются уже сохранёнными."""
        import gspread  # импорт здесь, чтобы --dry-run работал без настроенного Google

        client = gspread.service_account(filename=credentials_file)
        client.set_timeout(SHEETS_TIMEOUT)
        book = client.open_by_key(spreadsheet_id)
        self.archive = [book.worksheet(name) for name in archive]  # нет листа — ошибка сразу
        try:
            self.ws = book.worksheet(worksheet)
        except gspread.WorksheetNotFound:
            self.ws = book.add_worksheet(worksheet, rows=1000, cols=len(SHEET_HEADER))
        header = self.ws.row_values(1)
        check_header(header, f'лист «{worksheet}»', SHEET_HEADER)
        if not header:
            self.ws.update([SHEET_HEADER], 'A1')
            self.ws.freeze(rows=1)

    def sheet_ids(self) -> set[str]:
        """id вакансий, уже записанных в рабочий лист."""
        return {vacancy_id(u) for u in self.ws.col_values(SHEET_HEADER.index('Ссылка') + 1)[1:] if u}

    def known_ids(self) -> set[str]:
        ids = sheets_retry(lambda _: self.sheet_ids())
        for ws in self.archive:
            ids |= archive_ids(sheets_retry(lambda _: ws.get_all_values()), ws.title)
        return ids

    def append(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        # Строки собраны под HEADER — оставляем только колонки листа.
        cols = [HEADER.index(c) for c in SHEET_HEADER]
        values = [[safe(r[i]) for i in cols] for r in rows]
        link = SHEET_HEADER.index('Ссылка')

        def write(attempt: int) -> None:
            pending = values
            if attempt:
                # Таймаут не значит, что строки не записались: уже попавшие в лист не дублируем.
                saved = self.sheet_ids()
                pending = [r for r in values if vacancy_id(r[link]) not in saved]
            if pending:
                # RAW — значения пишутся как есть, без интерпретации формул.
                self.ws.append_rows(pending, value_input_option='RAW')

        sheets_retry(write)


def archive_ids(rows: list[list[str]], title: str) -> set[str]:
    """id из старого листа: колонка «id», иначе — вычисленные из колонки «Ссылка»."""
    header = rows[0] if rows else []
    if 'id' in header:
        col, convert = header.index('id'), str.strip
    elif 'Ссылка' in header:
        col, convert = header.index('Ссылка'), vacancy_id
    else:
        raise RuntimeError(f'лист «{title}»: нет колонки «id» или «Ссылка» — не по чему искать дубликаты')
    return {convert(r[col]) for r in rows[1:] if len(r) > col and r[col].strip()}


class CsvStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def known_ids(self) -> set[str]:
        """id уже сохранённых вакансий. Вызывается в начале запуска — заодно проверяет заголовок,
        чтобы несовместимый файл обнаружился до запросов к API."""
        if not self.path.exists():
            return set()
        with self.path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
        if not rows:
            return set()
        check_header(rows[0], str(self.path))
        col = HEADER.index('Ссылка')
        return {vacancy_id(row[col]) for row in rows[1:] if len(row) > col}

    def append(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        new = not self.path.exists() or self.path.stat().st_size == 0
        if not new:
            with self.path.open(encoding='utf-8-sig', newline='') as f:
                check_header(next(csv.reader(f), []), str(self.path))
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
