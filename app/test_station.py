import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
import ssl
import io
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from station import ACCOUNT_LOG_PATH, BILLING_REQUIRED, DAILY_STAT_PATH, DEFAULT_BASE_URL, STATUS_PATH, TOKEN_LOG_PATH, StationFailure, fetch_station, normalize_base_url, parse_recent_latency, parse_station_payload


class StationParsingTests(unittest.TestCase):
    def test_missing_tls_runtime_file_is_distinct_from_connectivity_failure(self):
        # Exercise both failures thrown during local HTTPS initialization and
        # failures wrapped by urllib. Do not leak dependency paths or Key data.
        for error in (FileNotFoundError('private-runtime-path'), URLError(FileNotFoundError('private-runtime-path'))):
            with self.subTest(error=type(error).__name__), patch('station.urlopen', side_effect=error):
                with self.assertRaises(StationFailure) as caught:
                    fetch_station('test-placeholder')
                self.assertEqual(caught.exception.code, 'tls_runtime')
                self.assertNotIn('private-runtime-path', str(caught.exception))
                self.assertNotIn('test-placeholder', str(caught.exception))

    def test_certificate_verification_failure_keeps_specific_error(self):
        with patch('station.urlopen', side_effect=URLError(ssl.SSLCertVerificationError(1, 'private-certificate-details'))):
            with self.assertRaises(StationFailure) as caught:
                fetch_station('test-placeholder')
        self.assertEqual(caught.exception.code, 'tls_certificate')
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn('private-certificate-details', str(caught.exception))

    def test_normalizes_station_url(self):
        self.assertEqual(normalize_base_url("https://www.dogapi.cc/"), "https://www.dogapi.cc")
        self.assertEqual(normalize_base_url("https://www.dogapi.cc/dashboard/overview?tab=usage"), "https://www.dogapi.cc")
        self.assertEqual(normalize_base_url("https://www.dogapi.cc/v1/"), "https://www.dogapi.cc")

    def test_separates_wallet_and_token_usage(self):
        snapshot = parse_station_payload(
            {
                "code": True,
                "data": {
                    "name": "Codex key",
                    "currency": "¥",
                    "total_used_display": 12.5,
                    "total_available_display": 87.5,
                    "unlimited_quota": False,
                },
            },
            {
                "code": True,
                "data": {
                    "balance_infos": [
                        {"currency": "¥", "total_balance": "98.25"},
                    ]
                },
            },
            checked_at=1700000000,
        )
        self.assertEqual(snapshot["walletBalance"], "98.25")
        self.assertEqual(snapshot["tokenUsed"], "12.5")
        self.assertEqual(snapshot["tokenAvailable"], "87.5")
        self.assertEqual(snapshot["provider"], "DogAPI")

    def test_unlimited_token_can_supply_wallet_balance(self):
        snapshot = parse_station_payload(
            {"data": {"currency": "$", "user_balance_display": 7.2, "unlimited_quota": True}},
            {"data": {"balance_infos": []}},
        )
        self.assertEqual(snapshot["walletBalance"], "7.2")
        self.assertIsNone(snapshot["tokenAvailable"])

    def test_unlimited_token_ignores_internal_negative_quota(self):
        snapshot = parse_station_payload(
            {"data": {"total_used_display": 4.5, "total_available_display": -4.5,
                      "total_available": -2250000, "unlimited_quota": True}}, {})
        self.assertIsNone(snapshot["tokenAvailable"])
        self.assertTrue(snapshot["unlimitedQuota"])

    def test_wallet_endpoint_failure_preserves_valid_token_balance(self):
        usage = {"data": {"user_balance_display": 12, "total_used_display": 3}}
        with patch('station._json_request', side_effect=[usage, StationFailure('钱包接口暂不可用')]):
            snapshot = fetch_station('test-placeholder')
        self.assertEqual(snapshot['walletBalance'], '12')
        self.assertEqual(snapshot['tokenUsed'], '3')

    def test_wallet_failure_is_visible_without_balance_fallback(self):
        with patch('station._json_request', side_effect=[{'data': {}}, StationFailure('钱包接口暂不可用')]):
            with self.assertRaisesRegex(StationFailure, '钱包接口暂不可用'):
                fetch_station('test-placeholder')


class DailyBillingTests(unittest.TestCase):
    def setUp(self):
        self.usage = {'data': {'currency': '¥', 'total_used': 5000000,
                              'total_used_display': 10, 'user_balance_display': 20}}
        self.balance = {'balance_infos': [{'currency': 'CNY', 'total_balance': '20.00'}]}
        self.stat = {'success': True, 'data': {'quota': 1234567, 'rpm': 0, 'tpm': 0}}
        self.status = {'success': True, 'data': {'quota_display_type': 'CNY',
                                               'quota_per_unit': 500000, 'usd_exchange_rate': 1}}
        self.logs = {'success': True, 'data': []}
        self.calls = []
        self.now = datetime(2026, 11, 6, 15, 13, tzinfo=timezone(timedelta(hours=8))).timestamp()

    def request(self, base, path, credential, timeout, extra_headers=None):
        self.calls.append((path, credential, extra_headers))
        if path == '/api/usage/token/':return self.usage
        if path == '/user/balance':return self.balance
        if path == STATUS_PATH:return self.status
        if path.startswith(DAILY_STAT_PATH + '?'):return self.stat
        if path == TOKEN_LOG_PATH or path.startswith(ACCOUNT_LOG_PATH + '?'):return self.logs
        self.fail('Unexpected endpoint: ' + path)

    def fetch(self, **kwargs):
        with patch('station._json_request', side_effect=self.request), patch('station.time.time', return_value=self.now):
            return fetch_station('api-credential', billing_access_token='billing-secret', account_id='42', **kwargs)

    def test_without_billing_permission_never_uses_recent_logs_as_today_total(self):
        with patch('station._json_request', side_effect=self.request):
            snapshot = fetch_station('api-credential')
        self.assertEqual([call[0] for call in self.calls], ['/api/usage/token/', '/user/balance', TOKEN_LOG_PATH])
        self.assertIsNone(snapshot['todayUsed'])
        self.assertEqual(snapshot['todayError'], BILLING_REQUIRED)
        self.assertEqual(snapshot['walletBalance'], '20')

    def test_full_account_stat_uses_shanghai_midnight_and_separate_credential(self):
        snapshot = self.fetch()
        self.assertEqual(snapshot['todayUsed'], '2.469134')
        self.assertIsNone(snapshot['todayError'])
        self.assertEqual(snapshot['todayDate'], '2026-11-06')
        self.assertEqual(snapshot['currency'], '¥')
        path, credential, headers = self.calls[2]
        self.assertEqual(urlsplit(path).path, DAILY_STAT_PATH)
        self.assertEqual(credential, 'billing-secret')
        self.assertEqual(headers, {'New-Api-User': '42'})
        query = parse_qs(urlsplit(path).query)
        midnight = int(datetime(2026, 11, 6, tzinfo=timezone(timedelta(hours=8))).timestamp())
        self.assertEqual(query, {'type': ['2'], 'start_timestamp': [str(midnight)], 'end_timestamp': [str(int(self.now))]})
        self.assertEqual(len(self.calls), 4)
        self.assertNotIn('billing-secret', json.dumps(snapshot))
        self.assertNotIn('api-credential', json.dumps(snapshot))

    def test_zero_cumulative_usage_uses_public_currency_configuration(self):
        self.usage['data'].update(total_used=0, total_used_display=0)
        self.status['data']['usd_exchange_rate'] = 7
        self.stat['data']['quota'] = 1000000
        snapshot = self.fetch()
        self.assertEqual(snapshot['todayUsed'], '14')
        self.assertIn((STATUS_PATH, '', None), self.calls)

    def test_known_conversion_returns_true_zero(self):
        self.stat['data']['quota'] = 0
        self.assertEqual(self.fetch()['todayUsed'], '0')

    def test_missing_units_or_currency_mismatch_never_guess_amount(self):
        self.usage['data'].update(total_used=0, total_used_display=0)
        del self.status['data']['quota_per_unit']
        snapshot = self.fetch()
        self.assertIsNone(snapshot['todayUsed']); self.assertIn('换算单位', snapshot['todayError'])
        self.status['data']['quota_per_unit'] = 500000
        self.usage['data']['currency'] = '$'
        snapshot = self.fetch()
        self.assertIsNone(snapshot['todayUsed']); self.assertIn('货币配置不一致', snapshot['todayError'])
        self.assertEqual(snapshot['walletBalance'], '20')

    def test_invalid_stat_data_is_not_fabricated_zero(self):
        for quota in (None, -1, 'NaN', True):
            with self.subTest(quota=quota):
                self.stat['data']['quota'] = quota
                snapshot = self.fetch()
                self.assertIsNone(snapshot['todayUsed'])
                self.assertIn('格式不正确', snapshot['todayError'])
                self.assertEqual(snapshot['walletBalance'], '20')

    def test_http_billing_failures_preserve_wallet_without_disclosing_credentials(self):
        for status in (400, 401, 403):
            def open_request(request, timeout):
                if request.full_url.endswith('/api/usage/token/'):
                    return io.BytesIO(json.dumps(self.usage).encode())
                if request.full_url.endswith('/user/balance'):
                    return io.BytesIO(json.dumps(self.balance).encode())
                if request.full_url.endswith(TOKEN_LOG_PATH):
                    return io.BytesIO(json.dumps(self.logs).encode())
                self.assertEqual(request.get_header('Authorization'), 'Bearer billing-secret')
                self.assertEqual(request.get_header('New-api-user'), '42')
                raise HTTPError(request.full_url, status, 'billing-secret api-credential', {}, io.BytesIO(b'billing-secret'))
            with self.subTest(status=status), patch('station.urlopen', side_effect=open_request):
                snapshot = fetch_station('api-credential', billing_access_token='billing-secret', account_id='42')
            self.assertEqual(snapshot['walletBalance'], '20')
            self.assertIsNone(snapshot['todayUsed'])
            self.assertTrue(snapshot['todayError'])
            self.assertNotIn('billing-secret', json.dumps(snapshot))
            self.assertNotIn('api-credential', json.dumps(snapshot))

    def test_business_auth_failure_is_safe_and_preserves_wallet(self):
        def open_request(request, timeout):
            payload = self.usage if request.full_url.endswith('/api/usage/token/') else self.balance
            if '/api/log/self/stat?' in request.full_url:
                payload = {'success': False, 'message': 'Unauthorized, invalid access token billing-secret api-credential'}
            return io.BytesIO(json.dumps(payload).encode())
        with patch('station.urlopen', side_effect=open_request):
            snapshot = fetch_station('api-credential', billing_access_token='billing-secret', account_id='42')
        self.assertEqual(snapshot['walletBalance'], '20')
        self.assertIsNone(snapshot['todayUsed'])
        self.assertIn('未获授权', snapshot['todayError'])
        self.assertNotIn('billing-secret', json.dumps(snapshot))
        self.assertNotIn('api-credential', json.dumps(snapshot))

    def test_invalid_account_id_does_not_break_wallet(self):
        with patch('station._json_request', side_effect=self.request):
            snapshot = fetch_station('api-credential', billing_access_token='billing-secret', account_id='42\nsecret')
        self.assertEqual(snapshot['walletBalance'], '20')
        self.assertIn('数字 User ID', snapshot['todayError'])
        self.assertEqual(len(self.calls), 3)

    def test_missing_account_header_business_error_suggests_account_id(self):
        def request(base, path, credential, timeout, extra_headers=None):
            if path.startswith(DAILY_STAT_PATH):
                raise StationFailure('Invalid New-Api-User user id: billing-secret', 'service')
            return self.request(base, path, credential, timeout, extra_headers)
        with patch('station._json_request', side_effect=request):
            snapshot = fetch_station('api-credential', billing_access_token='billing-secret')
        self.assertEqual(snapshot['walletBalance'], '20')
        self.assertIn('检查账单访问令牌与账号 ID', snapshot['todayError'])
        self.assertNotIn('billing-secret', json.dumps(snapshot))

    def test_daily_network_and_header_errors_preserve_wallet_safely(self):
        for error in (StationFailure('private billing-secret', 'timeout'), ValueError('invalid header billing-secret')):
            def request(base, path, credential, timeout, extra_headers=None):
                if path.startswith(DAILY_STAT_PATH):raise error
                return self.request(base, path, credential, timeout, extra_headers)
            with self.subTest(error=type(error).__name__), patch('station._json_request', side_effect=request):
                snapshot = fetch_station('api-credential', billing_access_token='billing-secret')
            self.assertEqual(snapshot['walletBalance'], '20')
            self.assertIsNone(snapshot['todayUsed'])
            self.assertNotIn('billing-secret', json.dumps(snapshot))

    def test_shanghai_date_can_differ_from_utc_date(self):
        self.now = datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc).timestamp()
        snapshot = self.fetch()
        self.assertEqual(snapshot['todayDate'], '2026-11-06')
        query = parse_qs(urlsplit(self.calls[2][0]).query)
        self.assertEqual(int(query['start_timestamp'][0]), int(datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc).timestamp()))


class CallLatencyTests(unittest.TestCase):
    def test_stream_first_token_ms_and_total_seconds_are_distinct(self):
        snapshot = parse_recent_latency({'data': [{'type': 2, 'created_at': 1700000020,
            'is_stream': True, 'other': '{"frt": 275.5}', 'use_time': 8.25}]}, 'token_log')
        self.assertEqual(snapshot, {'latencyFirstTokenMs': 275.5, 'latencyTotalMs': 8250,
            'latencySource': 'token_log', 'latencyCheckedAt': 1700000020})

    def test_stream_zero_is_valid_but_nonstream_has_no_first_token_metric(self):
        row = {'type': 2, 'created_at': 1700000020, 'is_stream': True, 'other': {'frt': 0}, 'use_time': 0}
        stream = parse_recent_latency({'data': {'items': [row]}}, 'account_log')
        self.assertEqual(stream['latencyFirstTokenMs'], 0)
        self.assertEqual(stream['latencyTotalMs'], 0)
        row['is_stream'] = False
        nonstream = parse_recent_latency({'data': [row]}, 'token_log')
        self.assertIsNone(nonstream['latencyFirstTokenMs'])
        self.assertEqual(nonstream['latencyTotalMs'], 0)

    def test_malformed_metadata_preserves_total_and_invalid_metrics_stay_unknown(self):
        row = {'type': 2, 'created_at': 1700000020, 'is_stream': True, 'other': 'not-json', 'use_time': 3}
        snapshot = parse_recent_latency({'data': [row]}, 'token_log')
        self.assertIsNone(snapshot['latencyFirstTokenMs']); self.assertEqual(snapshot['latencyTotalMs'], 3000)
        for value in (None, -1, 'NaN', float('inf'), True):
            with self.subTest(value=value):
                row.update(other={'frt': value}, use_time=value)
                result = parse_recent_latency({'data': [row]}, 'token_log')
                self.assertIsNone(result['latencyFirstTokenMs']); self.assertIsNone(result['latencyTotalMs'])

    def test_latest_consume_record_wins_without_using_error_log_or_older_metrics(self):
        rows = [
            {'type': 5, 'created_at': 1700000040, 'use_time': 99},
            {'type': 2, 'created_at': 1700000010, 'is_stream': True, 'use_time': 8, 'other': {'frt': 150}},
            {'type': 2, 'created_at': 1700000030, 'is_stream': False},
        ]
        result = parse_recent_latency({'data': rows}, 'account_log')
        self.assertEqual(result['latencyCheckedAt'], 1700000030)
        self.assertIsNone(result['latencyFirstTokenMs']); self.assertIsNone(result['latencyTotalMs'])

    def test_equal_timestamp_keeps_newest_server_order_despite_display_ids(self):
        rows = [{'id': 1, 'type': 2, 'created_at': 1700000030, 'use_time': 2},
                {'id': 2, 'type': 2, 'created_at': 1700000030, 'use_time': 9}]
        self.assertEqual(parse_recent_latency({'data': rows}, 'token_log')['latencyTotalMs'], 2000)

    def test_no_records_remains_unknown(self):
        result = parse_recent_latency({'data': []}, 'token_log')
        self.assertTrue(all(value is None for value in result.values()))

    def test_account_log_query_uses_first_page_and_billing_auth(self):
        requests = []
        usage = {'data': {'currency': '¥', 'total_used': 500000, 'total_used_display': 1, 'user_balance_display': 9}}
        def request(base, path, credential, timeout, extra_headers=None):
            requests.append((path, credential, extra_headers))
            if path == '/api/usage/token/':return usage
            if path == '/user/balance':return {'balance_infos': []}
            if path.startswith(DAILY_STAT_PATH + '?'):return {'data': {'quota': 100000}}
            if path.startswith(ACCOUNT_LOG_PATH + '?'):
                return {'data': {'items': [{'type': 2, 'created_at': 1700000030, 'is_stream': True,
                    'other': '{"frt": 125}', 'use_time': 2}]}}
            self.fail('Unexpected endpoint')
        with patch('station._json_request', side_effect=request):
            result = fetch_station('api-test', billing_access_token='billing-test', account_id='17')
        path, credential, headers = requests[-1]
        self.assertEqual(urlsplit(path).path, ACCOUNT_LOG_PATH)
        self.assertEqual(parse_qs(urlsplit(path).query), {'p': ['1'], 'page_size': ['5'], 'type': ['2']})
        self.assertEqual(credential, 'billing-test'); self.assertEqual(headers, {'New-Api-User': '17'})
        self.assertEqual(result['latencyFirstTokenMs'], 125); self.assertEqual(result['latencyTotalMs'], 2000)
        self.assertEqual(result['latencySource'], 'account_log')
        self.assertEqual(result['todayUsed'], '0.2')
        self.assertEqual(result['walletBalance'], '9')

    def test_account_log_auth_failure_falls_back_to_recent_token_call(self):
        calls = []
        def request(base, path, credential, timeout, extra_headers=None):
            calls.append((path, credential))
            if path == '/api/usage/token/':return {'data': {'currency': '¥', 'total_used': 500000, 'total_used_display': 1, 'user_balance_display': 9}}
            if path == '/user/balance':return {'balance_infos': []}
            if path.startswith(DAILY_STAT_PATH + '?'):return {'data': {'quota': 100000}}
            if path.startswith(ACCOUNT_LOG_PATH + '?'):raise StationFailure('private billing-test', 'authentication')
            if path == TOKEN_LOG_PATH:return {'data': [{'type': 2, 'is_stream': True, 'other': {'frt': 375}, 'use_time': 4}]}
            self.fail('Unexpected endpoint')
        with patch('station._json_request', side_effect=request):
            result = fetch_station('api-test', billing_access_token='billing-test', account_id='17')
        self.assertEqual(calls[-1], (TOKEN_LOG_PATH, 'api-test'))
        self.assertEqual(result['latencySource'], 'token_log')
        self.assertEqual(result['latencyFirstTokenMs'], 375)
        self.assertEqual(result['todayUsed'], '0.2')
        self.assertNotIn('billing-test', json.dumps(result))

    def test_log_failure_never_replaces_wallet_or_complete_daily_amount(self):
        def request(base, path, credential, timeout, extra_headers=None):
            if path == '/api/usage/token/':return {'data': {'currency': '¥', 'total_used': 500000, 'total_used_display': 1, 'user_balance_display': 9}}
            if path == '/user/balance':return {'balance_infos': []}
            if path.startswith(DAILY_STAT_PATH + '?'):return {'data': {'quota': 100000}}
            raise StationFailure('sensitive log/credential details', 'network')
        with patch('station._json_request', side_effect=request):
            result = fetch_station('api-test', billing_access_token='billing-test', account_id='17')
        self.assertEqual(result['walletBalance'], '9'); self.assertEqual(result['todayUsed'], '0.2')
        self.assertIsNone(result['todayError'])
        self.assertIsNone(result['latencyFirstTokenMs']); self.assertIsNone(result['latencyTotalMs'])
        self.assertIsNone(result['latencySource']); self.assertIsNone(result['latencyCheckedAt'])
        self.assertNotIn('sensitive', json.dumps(result))

    def test_no_billing_permission_uses_token_log_only_for_recent_latency(self):
        def request(base, path, credential, timeout, extra_headers=None):
            if path == '/api/usage/token/':return {'data': {'currency': '¥', 'user_balance_display': 9, 'total_used_display': 100}}
            if path == '/user/balance':return {'balance_infos': []}
            if path == TOKEN_LOG_PATH:return {'data': [{'type': 2, 'is_stream': True, 'other': {'frt': 150}, 'use_time': 5, 'quota': 900000}]}
            self.fail('Billing endpoint must not be queried')
        with patch('station._json_request', side_effect=request):
            result = fetch_station('api-test')
        self.assertEqual(result['latencyFirstTokenMs'], 150); self.assertEqual(result['latencyTotalMs'], 5000)
        self.assertIsNone(result['todayUsed']); self.assertEqual(result['todayError'], BILLING_REQUIRED)


class DashboardTests(unittest.TestCase):
    """Exercise request completion order without animations or real accounts."""
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from pet import Bubble, Pet
        class Harness:
            quota = Pet.quota
            refresh_station_silently = Pet.refresh_station_silently
            _station_context = Pet._station_context
            quota_ready = Pet.quota_ready
            quota_failed = Pet.quota_failed
            station_ready = Pet.station_ready
            station_failed = Pet.station_failed
            _render_dashboard = Pet._render_dashboard
        self.pet = p = Harness()
        p.bubble = Bubble(); p.quota_bubble = Bubble()
        p.hidden = False
        p.desktop_hidden = lambda: p.hidden
        p.note_interaction = Mock(); p.play_click_sound = Mock(); p.play = Mock()
        p.position_bubble = Mock(); p.click_timer = Mock(); p.bubble_timer = Mock(); p.quota_timer = Mock()
        p.manifest = {}; p.idles = [{}]; p.dock_edge = None; p.generation = 0
        p.dashboard_requested = False; p.quota_inflight = False; p.station_inflight = False
        p.quota_error = None; p.station_error = None; p.current_quota_windows = None
        p.station_snapshot = None; p.station_snapshot_context = None; p.station_request_context = None
        p.show_codex_panel = True; p.show_station_panel = True
        self.values = {'stationApiKey': 'test-placeholder', 'stationBaseUrl': DEFAULT_BASE_URL}
        p.settings = Mock(); p.settings.value.side_effect = lambda name, default=None: self.values.get(name, default)
        self.codex_patch = patch('pet.QuotaWorker'); self.codex_worker = self.codex_patch.start()
        self.station_patch = patch('pet.StationWorker'); self.station_worker = self.station_patch.start()

    def tearDown(self):
        self.pet.bubble.close(); self.pet.quota_bubble.close()
        self.codex_patch.stop(); self.station_patch.stop()

    def text(self):
        from PySide6.QtWidgets import QLabel
        self.app.processEvents()
        return '\n'.join(label.text() for label in self.pet.quota_bubble.findChildren(QLabel) if label.parent() is not None and not label.isHidden())

    def test_first_open_shows_independent_loading_then_dogapi_survives_codex_failure(self):
        p = self.pet; p.quota()
        self.assertTrue(p.quota_bubble.isVisible())
        from PySide6.QtWidgets import QPushButton
        self.assertTrue(p.quota_bubble.findChild(QPushButton, 'refreshCodex').property('loading'))
        self.assertTrue(p.quota_bubble.findChild(QPushButton, 'refreshStation').property('loading'))
        p.station_ready({'walletBalance': '12.30', 'todayUsed': '1.20', 'tokenUsed': '30.50', 'unlimitedQuota': True})
        p.generation += 5
        p.quota_failed('登录已过期')
        self.assertIn('钱包余额：12.300', self.text())
        self.assertIn('今日消耗：1.200', self.text())
        self.assertNotIn('Key 剩余额度', self.text())
        self.assertNotIn('累计消费', self.text())
        self.assertIn('Codex 额度查询失败：登录已过期', self.text())
        self.assertIsNone(p.station_error)

    def test_open_during_silent_refresh_reuses_request(self):
        p = self.pet; p.refresh_station_silently()
        self.assertFalse(p.quota_bubble.isVisible())
        p.quota()
        self.assertTrue(p.quota_bubble.isVisible())
        self.assertEqual(self.station_worker.call_count, 1)
        p.station_ready({'walletBalance': '20'})
        self.assertIn('钱包余额：20.000', self.text())

    def test_previous_day_cached_cost_is_not_shown_as_today(self):
        p = self.pet; p.quota()
        p.station_ready({'walletBalance': '20', 'todayUsed': '9.876543', 'todayDate': '2000-01-01'})
        self.assertIn('钱包余额：20.000', self.text())
        self.assertIn('今日统计尚未更新', self.text())
        self.assertNotIn('9.876543', self.text())

    def test_failure_keeps_cached_result_with_explicit_error(self):
        p = self.pet; p.quota(); p.station_ready({'walletBalance': '20'})
        p.quota_ready([{'name': '5小时', 'remaining': 80}])
        p.quota()
        self.assertIn('钱包余额：20.000', self.text())
        from PySide6.QtWidgets import QPushButton
        self.assertTrue(p.quota_bubble.findChild(QPushButton, 'refreshStation').property('loading'))
        p.station_failed('DogAPI 网络连接失败，请检查网络或代理。')
        self.assertIn('钱包余额：20.000', self.text())
        self.assertIn('刷新失败，保留上次结果', self.text())
        self.assertIn('DogAPI 网络连接失败', self.text())
        self.assertIsNone(p.quota_error)

    def test_silent_completion_does_not_open_dismissed_or_replace_chat_bubble(self):
        p = self.pet; p.refresh_station_silently(); p.station_ready({'walletBalance': '20'})
        self.assertFalse(p.quota_bubble.isVisible())
        p.quota(); p.quota_bubble.hide(); p.station_failed('网络失败')
        self.assertFalse(p.quota_bubble.isVisible())
        p.bubble.message('聊天任务进行中')
        p.station_ready({'walletBalance': '21'})
        self.assertEqual(p.bubble.layout.itemAt(0).widget().text(),'聊天任务进行中')
        self.assertFalse(p.bubble.is_station_result)

    def test_missing_key_and_disabled_cards_render_immediately(self):
        p = self.pet; p.show_codex_panel = False; self.values['stationApiKey'] = ''
        p.quota()
        self.assertIn('未配置 DogAPI API Key', self.text())
        self.assertEqual(self.station_worker.call_count, 0)
        p.show_station_panel = False; p.quota()
        self.assertIn('额度卡片已关闭', self.text())

    def test_old_key_response_is_discarded_and_new_key_is_queried(self):
        p = self.pet; p.quota()
        old_context = (DEFAULT_BASE_URL, 'test-placeholder', '', '')
        self.values['stationApiKey'] = 'new-test-placeholder'
        p.station_ready({'walletBalance': 'OLD-ACCOUNT'}, old_context)
        self.assertIsNone(p.station_snapshot)
        self.assertEqual(self.station_worker.call_count, 2)
        p.station_ready({'walletBalance': '22'}, (DEFAULT_BASE_URL, 'new-test-placeholder', '', ''))
        self.assertIn('钱包余额：22.000', self.text())
        self.assertNotIn('OLD-ACCOUNT', self.text())


if __name__ == "__main__":
    unittest.main()
