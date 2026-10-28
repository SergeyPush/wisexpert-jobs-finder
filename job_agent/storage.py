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
# Лист/файл отсеянных: все колонки; «Решил» там — «Статус» (отброшена / спорная).
RENAMED = {'Статус': 'Решил'}  # колонка листа → колонка HEADER, откуда берётся значение
DROPPED_HEADER = ['Статус' if c == 'Решил' else c for c in HEADER]

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


def open_worksheet(book, name: str, header: list[str], not_found) -> object:
    """Лист name; нет — создаётся с заголовком header. Лист с другим набором колонок — ошибка."""
    try:
        ws = book.worksheet(name)
    except not_found:
        ws = book.add_worksheet(name, rows=1000, cols=len(header))
    current = ws.row_values(1)
    check_header(current, f'лист «{name}»', header)
    if not current:
        ws.update([header], 'A1')
        ws.freeze(rows=1)
    return ws


def worksheet_ids(ws, header: list[str]) -> set[str]:
    """id вакансий, уже записанных в лист."""
    return {vacancy_id(u) for u in ws.col_values(header.index('Ссылка') + 1)[1:] if u}


def append_to_worksheet(ws, header: list[str], rows: list[list[str]]) -> int | None:
    """Строки, собранные под HEADER, → лист с колонками header. С повторами при сбоях и без дублей.
    Возвращает номер первой записанной строки листа (None — ничего не записано или номер неизвестен)."""
    if not rows:
        return None
    # Оставляем только колонки листа, в его порядке.
    cols = [HEADER.index(RENAMED.get(c, c)) for c in header]
    values = [[safe(r[i]) for i in cols] for r in rows]
    link = header.index('Ссылка')

    def write(attempt: int):
        pending = values
        if attempt:
            # Таймаут не значит, что строки не записались: уже попавшие в лист не дублируем.
            saved = worksheet_ids(ws, header)
            pending = [r for r in values if vacancy_id(r[link]) not in saved]
        if pending:
            # RAW — значения пишутся как есть, без интерпретации формул.
            return ws.append_rows(pending, value_input_option='RAW')
        return None

    return first_row(sheets_retry(write))


def first_row(response) -> int | None:
    """Номер первой строки из ответа append_rows: {'updates': {'updatedRange': "'Лист'!A12:I15"}}."""
    if not isinstance(response, dict):
        return None
    m = re.search(r'![A-Z]+(\d+)', response.get('updates', {}).get('updatedRange', ''))
    return int(m.group(1)) if m else None


def mark_run_start(ws, row: int | None, header: list[str]) -> None:
    """Жирная линия над первой строкой запуска — видно, где начинается каждый поиск. Только оформление:
    лишних строк нет, сортировка и дедупликация не ломаются. Сбой оформления запуск не останавливает."""
    if row is None:
        return
    last_col = chr(ord('A') + len(header) - 1)  # колонок меньше 26
    try:
        sheets_retry(lambda _: ws.format(f'A{row}:{last_col}{row}', {'borders': {'top': {'style': 'SOLID_THICK'}}}))
    except Exception as exc:
        print(f'Google Sheets: линия-разделитель не поставлена ({type(exc).__name__}: {exc})', flush=True)


class GoogleSheet:
    def __init__(self, spreadsheet_id: str, credentials_file: str, worksheet: str = 'Вакансии',
                 archive: list[str] = (), dropped: str = ''):
        """archive — старые листы (только чтение): их вакансии тоже считаются уже сохранёнными.
        dropped — лист для отсеянных классификатором вакансий (с вероятностью и уровнем); '' — не писать."""
        import gspread  # импорт здесь, чтобы --dry-run работал без настроенного Google

        client = gspread.service_account(filename=credentials_file)
        client.set_timeout(SHEETS_TIMEOUT)
        book = client.open_by_key(spreadsheet_id)
        self.archive = [book.worksheet(name) for name in archive]  # нет листа — ошибка сразу
        self.ws = open_worksheet(book, worksheet, SHEET_HEADER, gspread.WorksheetNotFound)
        self.dropped_ws = open_worksheet(book, dropped, DROPPED_HEADER, gspread.WorksheetNotFound) if dropped else None
        self.marked = []  # листы, где первая строка этого запуска уже отделена линией

    def known_ids(self) -> set[str]:
        """Уже сохранённые и уже отсеянные вакансии: ни те, ни другие повторно не классифицируются."""
        ids = sheets_retry(lambda _: worksheet_ids(self.ws, SHEET_HEADER))
        if self.dropped_ws is not None:
            ids |= sheets_retry(lambda _: worksheet_ids(self.dropped_ws, DROPPED_HEADER))
        for ws in self.archive:
            ids |= archive_ids(sheets_retry(lambda _: ws.get_all_values()), ws.title)
        return ids

    def append(self, rows: list[list[str]]) -> None:
        self.write(self.ws, SHEET_HEADER, rows)

    def append_dropped(self, rows: list[list[str]]) -> None:
        if self.dropped_ws is not None:
            self.write(self.dropped_ws, DROPPED_HEADER, rows)

    def write(self, ws, header: list[str], rows: list[list[str]]) -> None:
        """Дописать строки; первая запись запуска в лист отделяется линией (append зовётся на каждый сайт × город)."""
        row = append_to_worksheet(ws, header, rows)
        if row is not None and ws not in self.marked:
            self.marked.append(ws)
            mark_run_start(ws, row, header)


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


def csv_ids(path: Path, header: list[str]) -> set[str]:
    """id вакансий из CSV. Заодно проверяет заголовок — несовместимый файл обнаружится до запросов к API."""
    if not path.exists():
        return set()
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    if not rows:
        return set()
    check_header(rows[0], str(path), header)
    col = header.index('Ссылка')
    return {vacancy_id(row[col]) for row in rows[1:] if len(row) > col}


def csv_append(path: Path, header: list[str], rows: list[list[str]]) -> None:
    """Дописать строки (собранные под HEADER, колонки header в том же порядке) в CSV."""
    if not rows:
        return
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(encoding='utf-8-sig', newline='') as f:
            check_header(next(csv.reader(f), []), str(path), header)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8-sig' if new else 'utf-8', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows([[safe(c) for c in r] for r in rows])


class CsvStore:
    def __init__(self, path: str, dropped: str = ''):
        """dropped — CSV для отсеянных классификатором вакансий; '' — не писать."""
        self.path = Path(path)
        self.dropped = Path(dropped) if dropped else None

    def known_ids(self) -> set[str]:
        """Уже сохранённые и уже отсеянные вакансии."""
        ids = csv_ids(self.path, HEADER)
        if self.dropped is not None:
            ids |= csv_ids(self.dropped, DROPPED_HEADER)
        return ids

    def append(self, rows: list[list[str]]) -> None:
        csv_append(self.path, HEADER, rows)

    def append_dropped(self, rows: list[list[str]]) -> None:
        if self.dropped is not None:
            csv_append(self.dropped, DROPPED_HEADER, rows)


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
