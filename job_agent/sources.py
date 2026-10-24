"""Сбор вакансий с work.ua (HTML) и robota.ua (JSON API) обычными HTTP-запросами, без браузера."""
from __future__ import annotations

import html
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from curl_cffi import requests

from .config import CITIES, KYIV, QUERY


class SourceError(RuntimeError):
    """Сайт не отдал данные: блокировка, HTTP-ошибка, смена разметки."""


@dataclass
class Vacancy:
    key: str  # уникальный ключ вакансии: "work:123" / "robota:456"
    source: str
    city: str
    title: str
    company: str
    salary: str
    posted: str  # ISO-время публикации по Киеву
    url: str
    snippet: str



def text(fragment: str) -> str:
    """HTML → плоский текст."""
    fragment = re.sub(r'<br\s*/?>|</p>|</li>', '\n', fragment, flags=re.I)
    fragment = re.sub(r'<[^>]+>', ' ', fragment)
    fragment = html.unescape(fragment).replace(' ', ' ').replace('\xa0', ' ')
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n\s*\n+', '\n', fragment)).strip()


class Fetcher:
    """HTTP-клиент с TLS-отпечатком Chrome (нужно для Cloudflare на work.ua), паузами и повторами."""

    def __init__(self, delay: float = 6, proxy: str = ''):
        self.delay = delay
        self.proxy = proxy
        self.reset()
        self.last = 0.0

    def reset(self) -> None:
        """Новая сессия без cookies — после блокировки Cloudflare старая сессия часто остаётся «подозрительной»."""
        proxies = {'https': self.proxy, 'http': self.proxy} if self.proxy else None
        self.session = requests.Session(impersonate='chrome', proxies=proxies)

    def get(self, url: str, retries: int = 3) -> requests.Response:
        for attempt in range(retries):
            # Пауза между любыми запросами, чтобы не провоцировать антибот.
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                resp = self.session.get(url, timeout=30)
            except requests.RequestsError as exc:
                err = f'{type(exc).__name__}: {exc}'
            else:
                challenged = 'cf_chl' in resp.text[:20000] or 'Just a moment' in resp.text[:2000]
                if resp.status_code == 200 and not challenged:
                    return resp
                err = f'HTTP {resp.status_code}' + (' (Cloudflare challenge)' if challenged else '')
                if resp.status_code == 404:
                    break
            if attempt + 1 < retries:
                time.sleep(5 * (attempt + 1))  # растущая пауза только перед повтором
        raise SourceError(f'{url}: {err}')


def skip_card(source: str, ident, exc: Exception) -> None:
    """Одна карточка с неожиданной структурой не должна обрывать сбор — пропускаем её с предупреждением."""
    print(f'{source}: пропущена карточка {ident}: {type(exc).__name__}: {exc}', file=sys.stderr)


def within(posted: datetime, now: datetime, hours: int = 24) -> bool:
    return now - timedelta(hours=hours) <= posted <= now + timedelta(minutes=5)


# ---------- work.ua ----------

WORK_CARD = re.compile(r'<div[^>]*class="card card-hover[^"]*job-link')


UA_MONTHS = {m: i for i, m in enumerate(('січня', 'лютого', 'березня', 'квітня', 'травня', 'червня', 'липня',
                                          'серпня', 'вересня', 'жовтня', 'листопада', 'грудня'), 1)}


def parse_work_listing(page: str, city: str) -> list[tuple[Vacancy, datetime | None]]:
    """Разбирает страницу выдачи work.ua. Возвращает вакансии с точным временем публикации.
    У платных «гарячих» карточек времени нет (None) — есть только дата в title ссылки."""
    starts = [m.start() for m in WORK_CARD.finditer(page)] + [len(page)]
    result = []
    for a, b in zip(starts, starts[1:]):
        card = page[a:b]
        link = re.search(r'<h2[^>]*>\s*<a[^>]*href="/jobs/(\d+)/"([^>]*)>(.*?)</a>', card, re.S)
        if not link:
            continue
        when = re.search(r'<time[^>]*datetime="([^"]+)"', card)
        try:
            posted = datetime.fromisoformat(when.group(1)).replace(tzinfo=KYIV) if when else None
        except ValueError as exc:
            skip_card('work.ua', link.group(1), exc)
            continue
        posted_str = posted.isoformat(timespec='minutes') if posted else ''
        if not posted:
            # «вакансія від 16 жовтня 2026» → только дата
            d = re.search(r'від (\d{1,2}) (\w+) (\d{4})', link.group(2))
            if d and d.group(2) in UA_MONTHS:
                posted_str = f'{d.group(3)}-{UA_MONTHS[d.group(2)]:02d}-{int(d.group(1)):02d}'
        # Только span сразу за иконкой: без суммы там «ЗП обговорюється…» без strong-600,
        # и поиск дальше по карточке захватывал бы название компании.
        salary = re.search(r'glyphicon-hryvnia[^>]*></span>\s*<span[^>]*>(.*?)</span>', card, re.S)
        company = re.search(r'glyphicon-company.*?<span class="strong-600">(.*?)</span>', card, re.S)
        address = re.search(r'title="Адреса роботи"></span><span[^>]*>(.*?)</span>', card, re.S)
        snippet = re.search(r'<p class="ellipsis[^"]*"[^>]*>(.*?)(?:</p>|<div)', card, re.S)
        job_id = link.group(1)
        result.append((Vacancy(
            key=f'work:{job_id}', source='work.ua', city=city,
            title=text(link.group(3)), company=text(company.group(1)) if company else '',
            salary=text(salary.group(1)) if salary else '', posted=posted_str,
            url=f'https://www.work.ua/jobs/{job_id}/',
            snippet=(text(snippet.group(1))[:300] if snippet else '') + (f' [{text(address.group(1)).strip(", ")}]' if address else ''),
        ), posted))
    return result


def work_no_results(page: str) -> bool:
    """Страница work.ua «За вашим запитом … вакансій поки немає» (проверено на живой выдаче)."""
    return 'ga-search-no-results' in page or 'вакансій поки немає' in page


def search_work(fetcher: Fetcher, city: str, now: datetime, max_pages: int = 30) -> list[Vacancy]:
    slug = CITIES[city].work_slug
    today = now.date().isoformat()
    found: dict[str, Vacancy] = {}
    for page in range(1, max_pages + 1):
        # days=122 — фильтр work.ua «новые» (~2 суток); точные 24 часа отсекаем по времени карточки.
        url = f'https://www.work.ua/jobs-{slug}-{quote(QUERY)}/?days=122&page={page}'
        body = fetcher.get(url).text
        cards = parse_work_listing(body, city)
        if not cards:
            # Пустая выдача без признака «ничего не найдено» — это блокировка или новая разметка, а не 0 вакансий.
            if page == 1 and not work_no_results(body):
                raise SourceError(f'{url}: карточки не найдены — возможно, изменилась разметка')
            break
        for vac, posted in cards:
            # «Гарячі» без времени берём, только если размещены сегодня.
            if (posted and within(posted, now)) or (posted is None and vac.posted == today):
                found[vac.key] = vac
        # Обычные карточки отсортированы по дате: встретили старую — дальше не идём.
        # Страницы из одних «гарячих» карточек (в начале выдачи Киева) пропускаем.
        if any(p and not within(p, now) for _, p in cards) or f'page={page + 1}' not in body:
            break
    return list(found.values())


# ---------- robota.ua ----------

ROBOTA_API = 'https://api.robota.ua'


def robota_salary(v: dict) -> str:
    lo, hi, fixed = v.get('salaryFrom') or 0, v.get('salaryTo') or 0, v.get('salary') or 0
    if lo and hi:
        return f'{lo}–{hi} грн'
    if fixed or lo or hi:
        return f'{fixed or lo or hi} грн'
    return ''


def parse_robota_search(data: dict, city: str) -> list[tuple[Vacancy, datetime]]:
    city_id = CITIES[city].robota_id
    result = []
    for v in data.get('documents') or []:
        # Поиск может подмешивать вакансии других городов — оставляем только нужный.
        if v.get('cityId') != city_id:
            continue
        try:
            # API отдаёт локальное киевское время без зоны, иногда с долями секунды.
            posted = datetime.fromisoformat(v['date'][:19]).replace(tzinfo=KYIV)
            vac = Vacancy(
                key=f'robota:{v["id"]}', source='robota.ua', city=city,
                title=text(v.get('name') or ''), company=text(v.get('companyName') or ''),
                salary=robota_salary(v), posted=posted.isoformat(timespec='minutes'),
                url=f'https://robota.ua/company{v["notebookId"]}/vacancy{v["id"]}',
                snippet=text(v.get('shortDescription') or '')[:300],
            )
        except (KeyError, TypeError, ValueError) as exc:
            skip_card('robota.ua', v.get('id', '?'), exc)
            continue
        result.append((vac, posted))
    return result


def search_robota(fetcher: Fetcher, city: str, now: datetime, max_pages: int = 10) -> list[Vacancy]:
    found: dict[str, Vacancy] = {}
    for page in range(max_pages):
        # period=2 — «за сутки»; дату всё равно проверяем сами.
        url = (f'{ROBOTA_API}/vacancy/search?keyWords={quote(QUERY)}&cityId={CITIES[city].robota_id}'
               f'&period=2&count=40&page={page}')
        try:
            data = fetcher.get(url).json()
        except ValueError as exc:
            raise SourceError(f'{url}: ответ не JSON') from exc
        docs = data.get('documents') or []
        for vac, posted in parse_robota_search(data, city):
            if within(posted, now):
                found[vac.key] = vac
        if len(docs) < 40 or (page + 1) * 40 >= (data.get('total') or 0):
            break
    return list(found.values())


def search(fetcher: Fetcher, source: str, city: str, now: datetime, max_pages: int = 30) -> list[Vacancy]:
    if source == 'work.ua':
        return search_work(fetcher, city, now, max_pages)
    if source == 'robota.ua':
        return search_robota(fetcher, city, now, max_pages)
    raise ValueError(f'неизвестный источник: {source}')
