"""
실제 코레일 API 를 호출하는 통합 테스트.

자격증명이 없으면 모든 테스트가 skip 된다.
실 예약/취소 테스트는 KORAIL_RESERVE_TEST=1 일 때만 실행된다.

실행:
    cp .env.example .env  # 그리고 값을 채운다
    python -m unittest test.test_integration -v

예약 테스트까지 같이 돌리려면:
    KORAIL_RESERVE_TEST=1 python -m unittest test.test_integration -v
"""
import os
import unittest
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from korail2 import (
    AdultPassenger,
    ChildPassenger,
    Korail,
    NoResultsError,
    ReserveOption,
    SeniorPassenger,
    SoldOutError,
)


def _has_credentials():
    return bool(os.environ.get('KORAIL_ID') and os.environ.get('KORAIL_PW'))


def _reserve_tests_enabled():
    return os.environ.get('KORAIL_RESERVE_TEST') == '1'


@unittest.skipUnless(_has_credentials(), "KORAIL_ID/KORAIL_PW 환경변수 미설정")
class KorailIntegrationTests(unittest.TestCase):
    """읽기 전용 통합 테스트. 자격증명이 있으면 항상 실행."""

    @classmethod
    def setUpClass(cls):
        cls.koid = os.environ['KORAIL_ID']
        cls.kopw = os.environ['KORAIL_PW']
        cls.korail = Korail(cls.koid, cls.kopw, auto_login=False)
        if not cls.korail.login():
            raise unittest.SkipTest(f"로그인 실패: {cls.koid}")

    @staticmethod
    def _tomorrow():
        return date.today() + timedelta(days=1)

    def test_login_state(self):
        self.assertTrue(self.korail.logined)
        self.assertIsNotNone(self.korail.membership_number)

    def test_search_train_returns_results(self):
        try:
            trains = self.korail.search_train(
                "서울", "부산",
                self._tomorrow().strftime("%Y%m%d"), "100000",
            )
        except NoResultsError:
            self.skipTest("내일 10시 서울→부산 좌석 없음")
        self.assertGreater(len(trains), 0)

    def test_search_train_allday_returns_at_least_search_train(self):
        date_str = self._tomorrow().strftime("%Y%m%d")
        try:
            single = self.korail.search_train("서울", "부산", date_str, "100000")
        except NoResultsError:
            self.skipTest("좌석 없음")
        allday = self.korail.search_train_allday("서울", "부산", date_str, "100000")
        self.assertGreaterEqual(len(allday), len(single))

    def test_reservations_is_list(self):
        rsvs = self.korail.reservations()
        self.assertIsInstance(rsvs, list)

    def test_tickets_is_list(self):
        tickets = self.korail.tickets()
        self.assertIsInstance(tickets, list)


@unittest.skipUnless(_has_credentials(), "자격증명 미설정")
@unittest.skipUnless(_reserve_tests_enabled(), "KORAIL_RESERVE_TEST=1 미설정 — 실 예약 테스트 skip")
class KorailReserveIntegrationTests(unittest.TestCase):
    """실제로 예약을 만들고 즉시 취소한다. 매우 신중하게 사용하라."""

    @classmethod
    def setUpClass(cls):
        cls.korail = Korail(
            os.environ['KORAIL_ID'], os.environ['KORAIL_PW'], auto_login=False,
        )
        if not cls.korail.login():
            raise unittest.SkipTest("로그인 실패")

    @staticmethod
    def _tomorrow_str():
        return (date.today() + timedelta(days=1)).strftime("%Y%m%d")

    def _reserve_and_cancel(self, option=ReserveOption.GENERAL_FIRST, passengers=None):
        try:
            trains = self.korail.search_train(
                "서울", "부산", self._tomorrow_str(), "100000",
                passengers=passengers,
            )
        except NoResultsError:
            self.skipTest("좌석 없음")

        try:
            rsv = self.korail.reserve(trains[0], passengers=passengers, option=option)
        except SoldOutError:
            self.skipTest("매진")

        try:
            rsvs = self.korail.reservations()
            self.assertEqual(len([r for r in rsvs if r.rsv_id == rsv.rsv_id]), 1)
        finally:
            self.korail.cancel(rsv)

        rsvs = self.korail.reservations()
        self.assertEqual(len([r for r in rsvs if r.rsv_id == rsv.rsv_id]), 0)

    def test_reserve_and_cancel_general(self):
        self._reserve_and_cancel()

    def test_reserve_and_cancel_special_only(self):
        self._reserve_and_cancel(option=ReserveOption.SPECIAL_ONLY)

    def test_reserve_and_cancel_multi_passenger(self):
        self._reserve_and_cancel(passengers=[
            AdultPassenger(1), ChildPassenger(1), SeniorPassenger(1),
        ])


if __name__ == '__main__':
    unittest.main()
