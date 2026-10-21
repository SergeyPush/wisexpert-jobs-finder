"""Настройки: города, источники, переменные окружения."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent  # корень проекта: .env и criteria.md ищутся здесь
KYIV = ZoneInfo('Europe/Kyiv')
SOURCES = ('work.ua', 'robota.ua')
QUERY = 'бухгалтер'


@dataclass(frozen=True)
class City:
    work_slug: str  # часть URL work.ua: /jobs-<slug>-бухгалтер/
    robota_id: int  # cityId в api.robota.ua


CITIES = {
    'Одесса': City('odesa', 3),
    'Ужгород': City('uzhhorod', 28),
    'Киев': City('kyiv', 1),
    'Львов': City('lviv', 2),
}


def load_dotenv(path: Path = ROOT / '.env') -> None:
    """Минимальный загрузчик .env, без лишней зависимости. Уже заданные переменные не перезаписывает."""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def env(name: str, default: str = '') -> str:
    """Значение переменной; пустая строка (например, `KEY=` в .env) тоже даёт default."""
    return os.environ.get(name, '').strip() or default


def load_criteria(path: str = '') -> str:
    """Текст критериев отбора из criteria.md (или CRITERIA_FILE). HTML-комментарии вырезаются — это заметки для человека."""
    # По умолчанию — criteria.md в корне проекта, независимо от текущей папки.
    file = Path(path or env('CRITERIA_FILE') or ROOT / 'criteria.md')
    if not file.exists():
        raise SystemExit(f'Нет файла критериев {file}: создайте его, см. README')
    criteria = re.sub(r'<!--.*?-->', '', file.read_text(encoding='utf-8'), flags=re.S).strip()
    if not criteria:
        raise SystemExit(f'{file}: критерии пустые')
    return criteria
