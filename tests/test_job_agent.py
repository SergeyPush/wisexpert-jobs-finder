import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from job_agent.config import KYIV
from job_agent.sources import parse_robota_search, parse_work_listing, within
from job_agent.storage import CsvStore

NOW = datetime(2026, 10, 20, 22, 0, tzinfo=KYIV)

# Фрагмент реальной разметки выдачи work.ua (сокращён).
WORK_HTML = '''
<div tabindex="0" class="card card-hover card-visited wordwrap job-link mt-sm sm:mt-lg">
 <h2 class="my-0"><a tabindex="-1" href="/jobs/8613931/" title="x">Головний бухгалтер</a></h2>
 <div class="text-indent"><span class="glyphicon glyphicon-hryvnia-fill" title="Зарплата"></span>
  <span class="strong-600">50&#8239;000&nbsp;грн</span></div>
 <div class="text-indent"><span class="glyphicon glyphicon-company" title="Дані про компанію"></span>
  <span class=""><span class="strong-600">Теплі спогади</span></span></div>
 <span class="glyphicon glyphicon-map-marker" title="Адреса роботи"></span><span class="">Одеса, </span>
 <p class="ellipsis ellipsis-line">Досвід від 5 років, 1С 8.3</p>
 <time datetime="2026-10-20 17:19:09">5 год. тому</time>
</div>
<div tabindex="0" class="card card-hover card-visited wordwrap job-link mt-sm">
 <h2 class="my-0"><a href="/jobs/111/">Бухгалтер</a></h2>
 <time datetime="2026-10-18 10:00:00">2 дні тому</time>
</div>
<div tabindex="0" class="card card-hover card-visited wordwrap job-link js-hot-block">
 <h2 class="my-0"><a href="/jobs/222/" title="Бухгалтер, вакансія від 16 жовтня 2026">Бухгалтер</a></h2>
</div>
'''


class FakeStore:
    def __init__(self, known=()):
        self.rows, self.known = [], set(known)

    def known_urls(self):
        return set(self.known)

    def append(self, rows):
        self.rows += rows


class ParsingTest(unittest.TestCase):
    def test_work_listing(self):
        cards = parse_work_listing(WORK_HTML, 'Одесса')
        self.assertEqual(len(cards), 3)
        self.assertEqual((cards[2][1], cards[2][0].posted), (None, '2026-10-16'))
        vac, posted = cards[0]
        self.assertEqual((vac.key, vac.title, vac.company, vac.salary), ('work:8613931', 'Головний бухгалтер', 'Теплі спогади', '50 000 грн'))
        self.assertEqual(vac.url, 'https://www.work.ua/jobs/8613931/')
        self.assertTrue(within(posted, NOW))
        self.assertFalse(within(cards[1][1], NOW))

    def test_work_salary_not_specified(self):
        # Реальная разметка work.ua: зарплата не указана — текст без strong-600, компания не должна попасть в зарплату.
        card = '''
<div tabindex="0" class="card card-hover card-visited wordwrap job-link mt-sm sm:mt-lg">
 <h2 class="my-0"><a tabindex="-1" href="/jobs/5378424/" title="x">Бухгалтер з первинної документації</a></h2>
 <div class="text-indent">
  <span class="glyphicon glyphicon-hryvnia-fill glyphicon-fs-24 text-default-7" title="Зарплата"></span>
      <span class="text-default-7">ЗП обговорюється під час співбесіди</span>
 </div>
 <div class="text-indent"><span class="glyphicon glyphicon-company glyphicon-fs-24 text-default-7" title="Дані про компанію"></span>
  <span class="mr-xs"><span class="strong-600">Supramarine, LLC</span></span></div>
 <time datetime="2026-10-20 18:44:01"></time>
</div>'''
        vac = parse_work_listing(card, 'Одесса')[0][0]
        self.assertEqual((vac.company, vac.salary), ('Supramarine, LLC', 'ЗП обговорюється під час співбесіди'))

    def test_robota_filters_other_cities(self):
        data = {'documents': [
            {'id': 1, 'notebookId': 9, 'name': 'Бухгалтер', 'companyName': 'А', 'cityId': 1, 'date': '2026-10-20T15:01:00.93',
             'salaryFrom': 20000, 'salaryTo': 30000, 'shortDescription': 'текст'},
            {'id': 2, 'notebookId': 9, 'name': 'Бухгалтер', 'companyName': 'Б', 'cityId': 3, 'date': '2026-10-20T15:01:00'},
        ]}
        cards = parse_robota_search(data, 'Киев')
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0][0].url, 'https://robota.ua/company9/vacancy1')
        self.assertEqual(cards[0][0].salary, '20000–30000 грн')


class StorageTest(unittest.TestCase):
    def test_csv_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            store = CsvStore(str(Path(d) / 'jobs.csv'))
            store.append([['a', 'b', 'c', 'd', '=HYPERLINK()', 'f', 'g', '', '', 'h', 'классификатор', 'https://x/1']])
            self.assertEqual(store.known_urls(), {'https://x/1'})
            self.assertIn("'=HYPERLINK()", (Path(d) / 'jobs.csv').read_text(encoding='utf-8-sig'))

    def test_csv_old_header_is_not_mixed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'jobs.csv'
            path.write_text('Найдено,Ссылка\nx,https://x/1\n', encoding='utf-8-sig')
            store = CsvStore(str(path))
            # Несовместимый файл обнаруживается уже при чтении, до запросов к API.
            with self.assertRaises(RuntimeError):
                store.known_urls()
            with self.assertRaises(RuntimeError):
                store.append([['row']])


class FakeClassifier:
    keep, drop = 0.8, 0.2

    def __init__(self, probs):
        self.probs = probs

    def classify_many(self, vacancies):
        from job_agent.classifier import Verdict
        return {v.key: Verdict(self.probs[v.key], 'бухгалтер') for v in vacancies}


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        from job_agent.sources import Vacancy
        self.vacs = [Vacancy('work:1', 'work.ua', 'Киев', 'Бухгалтер', 'Ко', '', '2026-10-20T10:00+03:00',
                             'https://www.work.ua/jobs/1/', 'описание 1')]

    def test_decisions_response_parsing(self):
        from types import SimpleNamespace as NS
        from unittest import mock

        from job_agent.classifier import DecisionsClassifier
        resp = NS(answers=[NS(name='matches', type='predicate', probability=0.91),
                           NS(name='level', type='choice', choice='chief')])
        with mock.patch('openai.OpenAI') as cls:
            cls.return_value.decisions.create.return_value = resp
            c = DecisionsClassifier('Только удалённая работа.')
            v = c.classify(self.vacs[0])
            kwargs = cls.return_value.decisions.create.call_args.kwargs
        self.assertEqual((v.probability, v.level, v.decision(0.8, 0.2)), (0.91, 'главбух/зам', 'keep'))
        self.assertEqual(kwargs['model'], 'gpt-6-luna')
        self.assertIn('Должность: Бухгалтер', kwargs['input'])
        # Критерии из criteria.md попадают в главный вопрос.
        self.assertEqual(kwargs['questions'][0]['name'], 'matches')
        self.assertIn('Только удалённая работа.', kwargs['questions'][0]['instructions'])

    def test_api_error_stops_after_one_call(self):
        from unittest import mock

        import openai

        from job_agent.classifier import ClassifierError, DecisionsClassifier
        vacs = self.vacs * 20
        no_credits = openai.RateLimitError('You have no credits remaining', response=mock.Mock(status_code=429),
                                           body={'code': 'insufficient_quota'})
        with mock.patch('openai.OpenAI') as cls:
            create = cls.return_value.decisions.create
            create.side_effect = no_credits
            c = DecisionsClassifier('критерии')
            with self.assertRaises(ClassifierError):
                c.classify_many(vacs)
        # Нет кредитов — один запрос без повторов, а не по запросу на каждую вакансию.
        self.assertEqual(create.call_count, 1)
        cls.assert_called_once_with(max_retries=0)

    def test_transient_error_retried(self):
        from types import SimpleNamespace as NS
        from unittest import mock

        import openai

        from job_agent import classifier
        from job_agent.classifier import ClassifierError, DecisionsClassifier
        unavailable = openai.InternalServerError('Decision inference is unavailable.',
                                                 response=mock.Mock(status_code=503), body=None)
        ok = NS(answers=[NS(name='matches', type='predicate', probability=0.9)])
        with mock.patch('openai.OpenAI') as cls, mock.patch.object(classifier.time, 'sleep') as sleep:
            create = cls.return_value.decisions.create
            c = DecisionsClassifier('критерии')
            # 503 дважды, затем ответ — вакансия классифицирована.
            create.side_effect = [unavailable, unavailable, ok]
            self.assertEqual(c.classify(self.vacs[0]).probability, 0.9)
            self.assertEqual(sleep.call_count, 2)
            # 503 больше RETRIES раз — остановка.
            create.side_effect = unavailable
            create.reset_mock()
            with self.assertRaises(ClassifierError):
                c.classify(self.vacs[0])
            self.assertEqual(create.call_count, classifier.RETRIES + 1)

    def test_refusal_goes_to_review(self):
        from job_agent.classifier import Verdict
        self.assertEqual(Verdict(None, error='отказ').decision(0.8, 0.2), 'review')


class PipelineTest(unittest.TestCase):
    def run_pipeline(self, review):
        import job_agent.pipeline as pipeline
        from job_agent.sources import Vacancy
        mk = lambda i, t: Vacancy(f'work:{i}', 'work.ua', 'Киев', t, 'Ко', '', '2026-10-20T10:00+03:00',
                                  f'https://www.work.ua/jobs/{i}/', f'описание {i}')
        vacs = [mk(1, 'Бухгалтер'), mk(2, 'Касир'), mk(3, 'Спеціаліст з обліку'), mk(4, 'Головбух')]
        store = FakeStore(known={'https://www.work.ua/jobs/4/'})  # уже сохранена раньше
        clf = FakeClassifier({'work:1': 0.9, 'work:2': 0.1, 'work:3': 0.5})
        original = pipeline.search
        pipeline.search = lambda *a: vacs
        try:
            report, total = pipeline.run_classifier(store, ['Киев'], ['work.ua'], NOW, clf, fetcher=None, review=review)
        finally:
            pipeline.search = original
        return store, report, total

    def test_review_saved_with_mark(self):
        store, report, total = self.run_pipeline('save')
        self.assertEqual([(r[-2], r[-1]) for r in store.rows],
                         [('классификатор', 'https://www.work.ua/jobs/1/'), ('проверить', 'https://www.work.ua/jobs/3/')])
        self.assertEqual((total['found'], total['saved'], total['review'], total['dropped']), (4, 1, 1, 1))
        self.assertIn('новых 3', report[0])

    def test_classifier_error_kept_even_with_drop(self):
        import job_agent.pipeline as pipeline
        from job_agent.classifier import Verdict
        from job_agent.sources import Vacancy
        vac = Vacancy('work:7', 'work.ua', 'Киев', 'Бухгалтер', 'Ко', '', '2026-10-20T10:00+03:00',
                      'https://www.work.ua/jobs/7/', 'описание')

        class Failing(FakeClassifier):
            def classify_many(self, vacancies):
                return {v.key: Verdict(None, error='RateLimitError') for v in vacancies}

        store = FakeStore()
        original = pipeline.search
        pipeline.search = lambda *a: [vac]
        try:
            _, total = pipeline.run_classifier(store, ['Киев'], ['work.ua'], NOW, Failing({}), fetcher=None, review='drop')
        finally:
            pipeline.search = original
        self.assertEqual(store.rows[0][-2], 'проверить')
        self.assertEqual(total['dropped'], 0)

    def test_review_dropped(self):
        store, _, total = self.run_pipeline('drop')
        self.assertEqual(len(store.rows), 1)
        self.assertEqual((total['review'], total['dropped']), (0, 2))


class SnapshotTest(unittest.TestCase):
    def test_snapshot_with_and_without_verdicts(self):
        import csv

        from job_agent.classifier import Verdict
        from job_agent.storage import write_snapshot
        vac = parse_work_listing(WORK_HTML, 'Одесса')[0][0]
        with tempfile.TemporaryDirectory() as d:
            raw, scored = Path(d) / 'before.csv', Path(d) / 'after.csv'
            write_snapshot(str(raw), [(vac, None, '')], with_verdicts=False)
            write_snapshot(str(scored), [(vac, Verdict(0.93, 'главбух/зам'), 'подходит')], with_verdicts=True)
            raw_rows = list(csv.reader(raw.read_text(encoding='utf-8-sig').splitlines()))
            scored_rows = list(csv.reader(scored.read_text(encoding='utf-8-sig').splitlines()))
        self.assertEqual(raw_rows[0][-1], 'Ссылка')
        self.assertEqual(raw_rows[1][-1], vac.url)
        self.assertEqual(scored_rows[0][-4:], ['Вероятность', 'Уровень', 'Решение', 'Ошибка'])
        self.assertEqual(scored_rows[1][-4:], ['0.93', 'главбух/зам', 'подходит', ''])


def work_card(job_id, when=None, title='Бухгалтер', hot_date=None):
    """HTML одной карточки выдачи work.ua: обычной (с <time>) или «гарячої» (только дата в title)."""
    attr = f' title="{title}, вакансія від {hot_date}"' if hot_date else ''
    time_tag = f'<time datetime="{when}">x</time>' if when else ''
    return (f'<div tabindex="0" class="card card-hover card-visited wordwrap job-link">'
            f'<h2><a href="/jobs/{job_id}/"{attr}>{title}</a></h2>{time_tag}</div>')


class FakeFetcher:
    """Отдаёт заранее заданные страницы и запоминает запрошенные URL."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url):
        from types import SimpleNamespace as NS
        self.urls.append(url)
        page = int(url.rsplit('page=', 1)[1])
        return NS(text=self.pages.get(page, ''))


class SearchWorkTest(unittest.TestCase):
    def test_skips_hot_pages_and_stops_at_old_card(self):
        from job_agent.sources import search_work
        pages = {
            # Стр. 1: только «гарячі» — старая (не берём) и сегодняшняя (берём), дальше идём.
            1: work_card(1, hot_date='16 жовтня 2026') + work_card(2, hot_date='20 жовтня 2026') + 'page=2',
            # Стр. 2: свежая и старая обычные — берём свежую и останавливаемся, хотя есть page=3.
            2: work_card(3, '2026-10-20 12:00:00') + work_card(4, '2026-10-18 12:00:00') + 'page=3',
            3: work_card(5, '2026-10-20 13:00:00'),
        }
        fetcher = FakeFetcher(pages)
        found = search_work(fetcher, 'Киев', NOW, max_pages=10)
        self.assertEqual(sorted(v.key for v in found), ['work:2', 'work:3'])
        self.assertEqual(len(fetcher.urls), 2)

    def test_empty_first_page_is_error_unless_not_found(self):
        from job_agent.sources import SourceError, search_work
        with self.assertRaises(SourceError):
            search_work(FakeFetcher({1: '<html>капча</html>'}), 'Киев', NOW)
        empty = '<div class="ga-search-no-results">За вашим запитом «бухгалтер» вакансій поки немає.</div>'
        self.assertEqual(search_work(FakeFetcher({1: empty}), 'Киев', NOW), [])

    def test_bad_robota_card_is_skipped(self):
        import contextlib
        import io
        data = {'documents': [{'id': 1, 'cityId': 1, 'name': 'Бухгалтер'},  # нет date/notebookId
                              {'id': 2, 'notebookId': 9, 'name': 'Бухгалтер', 'cityId': 1, 'date': '2026-10-20T15:01:00'}]}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cards = parse_robota_search(data, 'Киев')
        self.assertEqual([c[0].key for c in cards], ['robota:2'])
        self.assertIn('пропущена карточка 1', err.getvalue())


class FetcherTest(unittest.TestCase):
    def make(self, responses):
        from types import SimpleNamespace as NS
        from unittest import mock

        from job_agent.sources import Fetcher
        f = Fetcher(delay=0)
        f.session = mock.MagicMock()
        f.session.get.side_effect = [NS(status_code=c, text=t) for c, t in responses]
        return f

    def test_retries_cloudflare_then_succeeds(self):
        from unittest import mock
        f = self.make([(200, '<title>Just a moment...</title>'), (200, 'ok')])
        with mock.patch('time.sleep') as sleep:
            self.assertEqual(f.get('https://x').text, 'ok')
        self.assertEqual(sleep.call_count, 1)

    def test_404_fails_fast_and_no_sleep_after_last_attempt(self):
        from unittest import mock

        from job_agent.sources import SourceError
        f = self.make([(404, 'нет')])
        with mock.patch('time.sleep') as sleep, self.assertRaises(SourceError):
            f.get('https://x')
        self.assertEqual(sleep.call_count, 0)
        f = self.make([(403, 'x')] * 3)
        with mock.patch('time.sleep') as sleep, self.assertRaises(SourceError) as ctx:
            f.get('https://x')
        self.assertEqual(sleep.call_count, 2)  # паузы только между попытками
        self.assertIn('HTTP 403', str(ctx.exception))


class SafetyTest(unittest.TestCase):
    def test_formula_variants_escaped(self):
        from job_agent.storage import safe
        for value in ['=1+1', ' =1+1', '\t=1', '\rx', '@SUM(A1)', '-2']:
            self.assertTrue(safe(value).startswith("'"), value)
        self.assertEqual(safe('Бухгалтер'), 'Бухгалтер')

    def test_empty_env_value_uses_default(self):
        import os
        from unittest import mock

        from job_agent.config import env
        with mock.patch.dict(os.environ, {'DECISION_KEEP': ''}):
            self.assertEqual(env('DECISION_KEEP', '0.8'), '0.8')

    def test_sheet_with_old_header_is_rejected(self):
        import sys
        from unittest import mock

        from job_agent.storage import GoogleSheet
        ws = mock.MagicMock()
        ws.row_values.return_value = ['Найдено', 'Ссылка']
        gspread = mock.MagicMock()
        gspread.service_account.return_value.open_by_key.return_value.worksheet.return_value = ws
        with mock.patch.dict(sys.modules, {'gspread': gspread}), self.assertRaises(RuntimeError):
            GoogleSheet('id', 'creds.json')
        ws.update.assert_not_called()  # старый лист не перезаписан


class MonitoringTest(unittest.TestCase):
    def test_suspicious_zeros(self):
        from job_agent.pipeline import suspicious_zeros
        cities = ['Одесса', 'Ужгород', 'Киев', 'Львов']
        found = {('work.ua', c): 0 for c in cities} | {('robota.ua', 'Ужгород'): 0, ('robota.ua', 'Львов'): 5}
        warnings = suspicious_zeros(found, ['work.ua', 'robota.ua'], cities)
        self.assertEqual(len(warnings), 1)
        self.assertIn('work.ua: 0 вакансий во всех городах', warnings[0])
        # Ноль в Киеве подозрителен сам по себе; ноль в одном маленьком городе — нет.
        self.assertIn('Киев', suspicious_zeros({('robota.ua', 'Киев'): 0}, ['robota.ua'], ['Киев'])[0])
        self.assertEqual(suspicious_zeros({('robota.ua', 'Ужгород'): 0}, ['robota.ua'], ['Ужгород']), [])

    def test_source_error_goes_to_problems(self):
        import contextlib
        import io

        import job_agent.pipeline as pipeline
        from job_agent.sources import SourceError

        def blocked(*a):
            raise SourceError('https://www.work.ua/...: HTTP 403 (Cloudflare challenge)')

        original = pipeline.search
        pipeline.search = blocked
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                _, total = pipeline.run_classifier(FakeStore(), ['Киев'], ['work.ua'], NOW, FakeClassifier({}), fetcher=None)
        finally:
            pipeline.search = original
        self.assertEqual(total['errors'], 1)
        self.assertIn('HTTP 403', total['problems'][0])

    def test_notify_modes_and_payload(self):
        import os
        from unittest import mock

        import job_agent.notify as notify
        cfg = {'TELEGRAM_BOT_TOKEN': 'T', 'TELEGRAM_CHAT_ID': '42', 'TELEGRAM_NOTIFY': 'problems'}
        with mock.patch.dict(os.environ, cfg), mock.patch.object(notify.requests, 'post') as post:
            post.return_value.status_code = 200
            notify.notify([], 'сводка')  # проблем нет — молчим
            post.assert_not_called()
            notify.notify(['work.ua · Киев: ОШИБКА HTTP 403'], 'сводка')
            url, kwargs = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, 'https://api.telegram.org/botT/sendMessage')
        self.assertEqual(kwargs['json']['chat_id'], '42')
        self.assertIn('HTTP 403', kwargs['json']['text'])
        with mock.patch.dict(os.environ, cfg | {'TELEGRAM_NOTIFY': 'always'}), \
                mock.patch.object(notify.requests, 'post') as post:
            post.return_value.status_code = 200
            notify.notify([], 'x' * 5000)
            self.assertLessEqual(len(post.call_args.kwargs['json']['text']), notify.LIMIT + 2)

    def test_telegram_failure_does_not_raise(self):
        import contextlib
        import io
        import os
        from unittest import mock

        import job_agent.notify as notify
        cfg = {'TELEGRAM_BOT_TOKEN': 'SECRET', 'TELEGRAM_CHAT_ID': '42'}
        err = io.StringIO()
        with mock.patch.dict(os.environ, cfg), contextlib.redirect_stderr(err), \
                mock.patch.object(notify.requests, 'post', side_effect=notify.requests.RequestsError('bot SECRET')):
            self.assertFalse(notify.send_telegram('текст'))
        self.assertNotIn('SECRET', err.getvalue())  # токен не утекает в лог
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_CHAT_ID': ''}):
            self.assertFalse(notify.send_telegram('текст'))


class CriteriaTest(unittest.TestCase):
    def test_comments_stripped(self):
        from job_agent.config import load_criteria
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'criteria.md'
            path.write_text('<!-- заметка -->\nТолько удалёнка.', encoding='utf-8')
            self.assertEqual(load_criteria(str(path)), 'Только удалёнка.')

    def test_project_criteria_file_loads(self):
        from job_agent.config import load_criteria
        self.assertIn('бухгалтер', load_criteria(str(Path(__file__).parent.parent / 'criteria.md')))


if __name__ == '__main__':
    unittest.main()
