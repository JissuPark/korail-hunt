"""
bot.py 단위 테스트 — 네트워크/봇 연결 없이 파서와 포매터를 검증한다.

Conversation handler 자체는 python-telegram-bot 의 통합 환경이 필요해 여기서
다루지 않는다. 직접 봇을 띄워 수동 검증하라.
"""
import os
import unittest
from datetime import date
from unittest.mock import patch

from bot import (
    authorized_chat_ids,
    format_reservation_success,
    parse_date,
    parse_time,
)
from korail2.korail2 import Reservation


def _make_reservation(price=59800, count=1, buy_dt='20260530', buy_tm='140500', rsv_id='PNR1234'):
    data = {
        'h_trn_clsf_cd': '00', 'h_trn_clsf_nm': 'KTX',
        'h_trn_gp_cd': '100', 'h_trn_no': '001',
        'h_expct_dlay_hr': '00',
        'h_dpt_rs_stn_nm': '서울', 'h_dpt_rs_stn_cd': '0001',
        'h_dpt_dt': '20260601', 'h_dpt_tm': '100000',
        'h_arv_rs_stn_nm': '부산', 'h_arv_rs_stn_cd': '0020',
        'h_arv_dt': '20260601', 'h_arv_tm': '124200',
        'h_run_dt': '20260601',
        'h_rsv_psb_flg': 'Y', 'h_rsv_psb_nm': '예약가능',
        'h_spe_rsv_cd': '11', 'h_gen_rsv_cd': '11',
        'h_wait_rsv_flg': '-2',
        'h_pnr_no': rsv_id,
        'h_tot_seat_cnt': f'{count:03d}',
        'h_ntisu_lmt_dt': buy_dt,
        'h_ntisu_lmt_tm': buy_tm,
        'h_rsv_amt': f'{price:08d}',
        'txtJrnySqno': '001',
        'txtJrnyCnt': '01',
        'hidRsvChgNo': '00000',
    }
    return Reservation(data)


class ParseDateTests(unittest.TestCase):

    def setUp(self):
        self.today = date(2026, 5, 26)

    def test_yyyymmdd_passthrough(self):
        self.assertEqual(parse_date('20260601'), '20260601')

    def test_today(self):
        self.assertEqual(parse_date('오늘', today=self.today), '20260526')

    def test_tomorrow(self):
        self.assertEqual(parse_date('내일', today=self.today), '20260527')

    def test_day_after_tomorrow(self):
        self.assertEqual(parse_date('모레', today=self.today), '20260528')

    def test_plus_n_days(self):
        self.assertEqual(parse_date('+7', today=self.today), '20260602')

    def test_plus_zero_is_today(self):
        self.assertEqual(parse_date('+0', today=self.today), '20260526')

    def test_whitespace_stripped(self):
        self.assertEqual(parse_date('  오늘  ', today=self.today), '20260526')

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            parse_date('어제')

    def test_invalid_plus_raises(self):
        with self.assertRaises(ValueError):
            parse_date('+abc')

    def test_wrong_length_digits_raises(self):
        with self.assertRaises(ValueError):
            parse_date('2026601')


class ParseTimeTests(unittest.TestCase):

    def test_hhmm_pads_seconds(self):
        self.assertEqual(parse_time('1000'), '100000')

    def test_hhmmss_passthrough(self):
        self.assertEqual(parse_time('100530'), '100530')

    def test_colon_format(self):
        self.assertEqual(parse_time('10:00'), '100000')

    def test_colon_with_seconds(self):
        self.assertEqual(parse_time('10:05:30'), '100530')

    def test_whitespace_stripped(self):
        self.assertEqual(parse_time('  1000  '), '100000')

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_time('abc')

    def test_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            parse_time('10000')


class FormatReservationSuccessTests(unittest.TestCase):

    def test_includes_reservation_number(self):
        msg = format_reservation_success(_make_reservation(rsv_id='ABC123'))
        self.assertIn('ABC123', msg)

    def test_includes_buy_deadline(self):
        msg = format_reservation_success(_make_reservation(
            buy_dt='20260530', buy_tm='140500',
        ))
        self.assertIn('2026-05-30 14:05', msg)

    def test_price_formatted_with_thousand_separator(self):
        msg = format_reservation_success(_make_reservation(price=123456))
        self.assertIn('123,456원', msg)

    def test_seat_count(self):
        msg = format_reservation_success(_make_reservation(count=3))
        self.assertIn('(3석)', msg)


class AuthorizedChatIdsTests(unittest.TestCase):

    def _clean_env(self, **overrides):
        env = os.environ.copy()
        env.pop('TELEGRAM_AUTHORIZED_CHAT_ID', None)
        env.pop('TELEGRAM_AUTHORIZED_CHAT_IDS', None)
        env.update(overrides)
        return env

    def test_single_id_plural_var(self):
        with patch.dict(os.environ, self._clean_env(TELEGRAM_AUTHORIZED_CHAT_IDS='12345'), clear=True):
            self.assertEqual(authorized_chat_ids(), {12345})

    def test_multiple_ids_comma_separated(self):
        with patch.dict(os.environ, self._clean_env(TELEGRAM_AUTHORIZED_CHAT_IDS='111,222,333'), clear=True):
            self.assertEqual(authorized_chat_ids(), {111, 222, 333})

    def test_whitespace_tolerance(self):
        with patch.dict(os.environ, self._clean_env(TELEGRAM_AUTHORIZED_CHAT_IDS=' 111 , 222 , 333 '), clear=True):
            self.assertEqual(authorized_chat_ids(), {111, 222, 333})

    def test_singular_var_backward_compat(self):
        with patch.dict(os.environ, self._clean_env(TELEGRAM_AUTHORIZED_CHAT_ID='12345'), clear=True):
            self.assertEqual(authorized_chat_ids(), {12345})

    def test_plural_takes_precedence(self):
        with patch.dict(os.environ, self._clean_env(
            TELEGRAM_AUTHORIZED_CHAT_ID='999',
            TELEGRAM_AUTHORIZED_CHAT_IDS='111,222',
        ), clear=True):
            self.assertEqual(authorized_chat_ids(), {111, 222})

    def test_empty_returns_empty_set(self):
        with patch.dict(os.environ, self._clean_env(), clear=True):
            self.assertEqual(authorized_chat_ids(), set())

    def test_non_integer_entries_skipped(self):
        with patch.dict(os.environ, self._clean_env(TELEGRAM_AUTHORIZED_CHAT_IDS='111,abc,222'), clear=True):
            self.assertEqual(authorized_chat_ids(), {111, 222})

    def test_negative_ids_allowed(self):
        # Telegram 그룹 chat_id 는 음수
        with patch.dict(os.environ, self._clean_env(TELEGRAM_AUTHORIZED_CHAT_IDS='-100123,456'), clear=True):
            self.assertEqual(authorized_chat_ids(), {-100123, 456})


if __name__ == '__main__':
    unittest.main()
