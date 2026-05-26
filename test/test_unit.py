"""
korail2 단위 테스트.

네트워크/자격증명이 필요 없다. 모든 외부 호출은 mock 처리한다.
실제 코레일 API 와의 통합 검증은 test_integration.py 를 참조하라.
"""
import json
import unittest
from unittest.mock import Mock, patch

from korail2 import (
    AdultPassenger,
    ChildPassenger,
    Korail,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    Passenger,
    ReserveOption,
    SeniorPassenger,
    SoldOutError,
    ToddlerPassenger,
    TrainType,
)
from korail2.korail2 import Reservation, Schedule, Ticket, Train


# ---------------------------------------------------------------------------
# 테스트 픽스처
# ---------------------------------------------------------------------------

# AES-256 키. UTF-8 인코딩 시 32바이트가 되도록 32 ASCII 문자
FAKE_AES_KEY = "0123456789abcdef0123456789abcdef"


def make_train_data(
    *,
    train_no="001",
    dep_name="서울", arr_name="부산",
    dep_date="20260601", dep_time="100000",
    arr_date="20260601", arr_time="124200",
    special="00", general="11", wait="-2",
    rsv_psb_flg="Y", rsv_psb_nm="예약가능",
    train_type="00", train_type_name="KTX", train_group="100",
):
    return {
        'h_trn_clsf_cd': train_type,
        'h_trn_clsf_nm': train_type_name,
        'h_trn_gp_cd': train_group,
        'h_trn_no': train_no,
        'h_expct_dlay_hr': '00',
        'h_dpt_rs_stn_nm': dep_name, 'h_dpt_rs_stn_cd': '0001',
        'h_dpt_dt': dep_date, 'h_dpt_tm': dep_time,
        'h_arv_rs_stn_nm': arr_name, 'h_arv_rs_stn_cd': '0020',
        'h_arv_dt': arr_date, 'h_arv_tm': arr_time,
        'h_run_dt': dep_date,
        'h_rsv_psb_flg': rsv_psb_flg,
        'h_rsv_psb_nm': rsv_psb_nm,
        'h_spe_rsv_cd': special,
        'h_gen_rsv_cd': general,
        'h_wait_rsv_flg': wait,
    }


def make_reservation_data(**overrides):
    base = make_train_data(special="11", general="11")
    base.update({
        'h_pnr_no': 'PNR1234',
        'h_tot_seat_cnt': '001',
        'h_ntisu_lmt_dt': '20260530',
        'h_ntisu_lmt_tm': '140500',
        'h_rsv_amt': '00059800',
        'txtJrnySqno': '001',
        'txtJrnyCnt': '01',
        'hidRsvChgNo': '00000',
    })
    base.update(overrides)
    return base


def make_response(payload):
    return Mock(text=json.dumps(payload))


def make_logged_in_korail(membership="12345678"):
    """auto_login 없이 만든 뒤 로그인한 상태로 위장."""
    k = Korail(membership, "pw", auto_login=False)
    k.logined = True
    k._key = "session_key"
    k.membership_number = membership
    return k


def login_post_side_effect(url, **kwargs):
    """KORAIL_CODE → AES 키 응답, KORAIL_LOGIN → 로그인 성공 응답."""
    if 'code.do' in url:
        return make_response({
            "strResult": "SUCC",
            "app.login.cphd": {"idx": "idx1", "key": FAKE_AES_KEY},
        })
    if 'login.Login' in url:
        return make_response({
            "strResult": "SUCC",
            "strMbCrdNo": "12345678",
            "strCustNm": "홍길동",
            "strEmailAdr": "hong@example.com",
            "Key": "session_key_xyz",
        })
    raise AssertionError(f"Unexpected POST URL: {url}")


# ---------------------------------------------------------------------------
# Passenger
# ---------------------------------------------------------------------------

class PassengerTests(unittest.TestCase):

    def test_abstract_passenger_cannot_be_instantiated(self):
        with self.assertRaises(NotImplementedError):
            Passenger()

    def test_reduce_rejects_non_passenger(self):
        with self.assertRaises(TypeError):
            Passenger.reduce([AdultPassenger, "string"])

    def test_reduce_groups_same_type(self):
        reduced = Passenger.reduce([
            AdultPassenger(), AdultPassenger(), ChildPassenger(), SeniorPassenger(), SeniorPassenger(),
        ])
        self.assertEqual(len(reduced), 3)
        counts = {type(p).__name__: p.count for p in reduced}
        self.assertEqual(counts, {'AdultPassenger': 2, 'ChildPassenger': 1, 'SeniorPassenger': 2})

    def test_reduce_drops_zero_and_negative_counts(self):
        reduced = Passenger.reduce([
            AdultPassenger(), AdultPassenger(), AdultPassenger(count=-1),
            ChildPassenger(count=0),
            SeniorPassenger(count=-1),
        ])
        self.assertEqual(len(reduced), 1)
        self.assertIsInstance(reduced[0], AdultPassenger)
        self.assertEqual(reduced[0].count, 1)

    def test_get_dict_uses_index(self):
        d = AdultPassenger(2).get_dict(1)
        self.assertEqual(d['txtPsgTpCd1'], '1')
        self.assertEqual(d['txtCompaCnt1'], 2)
        self.assertEqual(d['txtDiscKndCd1'], '000')

    def test_add_combines_count_for_same_group(self):
        combined = AdultPassenger(1) + AdultPassenger(2)
        self.assertEqual(combined.count, 3)

    def test_senior_has_discount_code(self):
        self.assertEqual(SeniorPassenger().discount_type, '131')

    def test_toddler_has_discount_code(self):
        self.assertEqual(ToddlerPassenger().discount_type, '321')


# ---------------------------------------------------------------------------
# Train / Schedule / Reservation / Ticket
# ---------------------------------------------------------------------------

class TrainModelTests(unittest.TestCase):

    def test_has_general_seat(self):
        t = Train(make_train_data(general="11", special="00"))
        self.assertTrue(t.has_general_seat())
        self.assertFalse(t.has_special_seat())
        self.assertTrue(t.has_seat())

    def test_has_special_seat(self):
        t = Train(make_train_data(general="00", special="11"))
        self.assertFalse(t.has_general_seat())
        self.assertTrue(t.has_special_seat())
        self.assertTrue(t.has_seat())

    def test_sold_out_has_no_seat(self):
        t = Train(make_train_data(general="13", special="13"))
        self.assertFalse(t.has_seat())

    def test_general_waiting_list_detected(self):
        t = Train(make_train_data(general="13", special="13", wait="9"))
        self.assertTrue(t.has_general_waiting_list())
        self.assertTrue(t.has_waiting_list())

    def test_no_waiting_list_when_flag_is_minus_two(self):
        t = Train(make_train_data(wait="-2"))
        self.assertFalse(t.has_general_waiting_list())

    def test_schedule_repr_format(self):
        s = Schedule(make_train_data(dep_time="103000", arr_time="124500"))
        self.assertEqual(repr(s), "[KTX] 6월 1일, 서울~부산(10:30~12:45)")

    def test_train_repr_includes_seats(self):
        t = Train(make_train_data(general="11", special="11"))
        r = repr(t)
        self.assertIn("특실", r)
        self.assertIn("일반실", r)
        self.assertIn("예약가능", r)

    def test_train_repr_sold_out_shows_no_seats(self):
        t = Train(make_train_data(general="13", special="13"))
        r = repr(t)
        self.assertNotIn("특실", r)
        self.assertNotIn("일반실", r)

    def test_reservation_repr_includes_price_and_deadline(self):
        rsv = Reservation(make_reservation_data())
        r = repr(rsv)
        self.assertIn("59800원", r)
        self.assertIn("1석", r)
        self.assertIn("구입기한 5월 30일 14:05", r)

    def test_ticket_repr_single_seat(self):
        ticket_payload = {
            'ticket_list': [{'train_info': [dict(
                make_train_data(),
                h_seat_no_end="3A",
                h_seat_cnt="001",
                h_buy_ps_nm="홍길동",
                h_orgtk_sale_dt="20260601",
                h_orgtk_wct_no="W1",
                h_orgtk_ret_sale_dt="20260601",
                h_orgtk_sale_sqno="S1",
                h_orgtk_ret_pwd="PW",
                h_rcvd_amt="00059800",
                h_srcar_no="3",
                h_seat_no="3A",
            )]}],
        }
        t = Ticket(ticket_payload)
        r = repr(t)
        self.assertIn("=> 3호 3A", r)
        self.assertIn("59800원", r)

    def test_ticket_repr_multi_seat(self):
        ticket_payload = {
            'ticket_list': [{'train_info': [dict(
                make_train_data(),
                h_seat_no_end="3B",
                h_seat_cnt="002",
                h_buy_ps_nm="홍길동",
                h_orgtk_sale_dt="20260601",
                h_orgtk_wct_no="W1",
                h_orgtk_ret_sale_dt="20260601",
                h_orgtk_sale_sqno="S1",
                h_orgtk_ret_pwd="PW",
                h_rcvd_amt="00119600",
                h_srcar_no="3",
                h_seat_no="3A",
            )]}],
        }
        t = Ticket(ticket_payload)
        self.assertIn("=> 3호 3A~3B", repr(t))


# ---------------------------------------------------------------------------
# 에러 클래스 (ExceptionForm metaclass)
# ---------------------------------------------------------------------------

class ErrorClassTests(unittest.TestCase):

    def test_known_code_in_no_results_error(self):
        self.assertIn("P100", NoResultsError)
        self.assertIn("WRG000000", NoResultsError)

    def test_known_code_in_need_to_login_error(self):
        self.assertIn("P058", NeedToLoginError)

    def test_known_code_in_sold_out_error(self):
        self.assertIn("ERR211161", SoldOutError)

    def test_unknown_code_not_in_error_class(self):
        self.assertNotIn("XXX", NoResultsError)

    def test_error_str(self):
        e = KorailError("msg", "CODE")
        self.assertEqual(str(e), "msg (CODE)")


# ---------------------------------------------------------------------------
# Korail._result_check
# ---------------------------------------------------------------------------

class ResultCheckTests(unittest.TestCase):

    def setUp(self):
        self.k = Korail("12345678", "pw", auto_login=False)

    def test_success_returns_true(self):
        self.assertTrue(self.k._result_check({"strResult": "SUCC", "h_msg_txt": "OK"}))

    def test_no_results_code_raises_no_results_error(self):
        with self.assertRaises(NoResultsError):
            self.k._result_check({"strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "no"})

    def test_need_to_login_code_raises(self):
        with self.assertRaises(NeedToLoginError):
            self.k._result_check({"strResult": "FAIL", "h_msg_cd": "P058", "h_msg_txt": "no"})

    def test_sold_out_code_raises(self):
        with self.assertRaises(SoldOutError):
            self.k._result_check({"strResult": "FAIL", "h_msg_cd": "ERR211161", "h_msg_txt": "no"})

    def test_unknown_failure_raises_base_korail_error(self):
        with self.assertRaises(KorailError) as cm:
            self.k._result_check({"strResult": "FAIL", "h_msg_cd": "UNKNOWN", "h_msg_txt": "boom"})
        # 서브클래스가 아닌 base 여야 한다
        self.assertIs(type(cm.exception), KorailError)


# ---------------------------------------------------------------------------
# Korail.login
# ---------------------------------------------------------------------------

class LoginTests(unittest.TestCase):

    def _build(self):
        return Korail("12345678", "pw", auto_login=False)

    def _captured_login_data(self, mock_post):
        # mock_post.call_args_list[1] 이 KORAIL_LOGIN 호출
        login_call = next(c for c in mock_post.call_args_list if 'login.Login' in c.args[0])
        return login_call.kwargs['data']

    def test_membership_number_uses_input_flag_2(self):
        k = self._build()
        with patch.object(k._session, 'post', side_effect=login_post_side_effect) as mp:
            self.assertTrue(k.login())
        self.assertEqual(self._captured_login_data(mp)['txtInputFlg'], '2')

    def test_email_uses_input_flag_5(self):
        k = Korail("foo@bar.com", "pw", auto_login=False)
        with patch.object(k._session, 'post', side_effect=login_post_side_effect) as mp:
            self.assertTrue(k.login())
        self.assertEqual(self._captured_login_data(mp)['txtInputFlg'], '5')

    def test_phone_uses_input_flag_4(self):
        k = Korail("010-1234-5678", "pw", auto_login=False)
        with patch.object(k._session, 'post', side_effect=login_post_side_effect) as mp:
            self.assertTrue(k.login())
        self.assertEqual(self._captured_login_data(mp)['txtInputFlg'], '4')

    def test_success_sets_session_state(self):
        k = self._build()
        with patch.object(k._session, 'post', side_effect=login_post_side_effect):
            k.login()
        self.assertTrue(k.logined)
        self.assertEqual(k._key, "session_key_xyz")
        self.assertEqual(k.membership_number, "12345678")
        self.assertEqual(k.name, "홍길동")
        self.assertEqual(k.email, "hong@example.com")

    def test_failure_returns_false(self):
        k = self._build()

        def fail_side_effect(url, **kwargs):
            if 'code.do' in url:
                return make_response({
                    "strResult": "SUCC",
                    "app.login.cphd": {"idx": "idx1", "key": FAKE_AES_KEY},
                })
            return make_response({"strResult": "FAIL", "h_msg_txt": "bad credentials"})

        with patch.object(k._session, 'post', side_effect=fail_side_effect):
            self.assertFalse(k.login())
        self.assertFalse(k.logined)

    def test_login_with_new_id_updates_attribute(self):
        k = self._build()
        with patch.object(k._session, 'post', side_effect=login_post_side_effect):
            k.login("99999999", "newpw")
        self.assertEqual(k.korail_id, "99999999")
        self.assertEqual(k.korail_pw, "newpw")

    def test_logout_clears_flag(self):
        k = make_logged_in_korail()
        with patch.object(k._session, 'get') as mg:
            mg.return_value = make_response({})
            k.logout()
        self.assertFalse(k.logined)


# ---------------------------------------------------------------------------
# Korail.search_train / search_train_allday
# ---------------------------------------------------------------------------

class SearchTrainTests(unittest.TestCase):

    def setUp(self):
        self.k = make_logged_in_korail()

    def _search_response(self, *trains):
        return make_response({
            "strResult": "SUCC",
            "h_msg_cd": "",
            "h_msg_txt": "",
            "trn_infos": {"trn_info": list(trains)},
        })

    def test_filters_no_seat_trains_by_default(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = self._search_response(
                make_train_data(train_no="A", general="11"),
                make_train_data(train_no="B", general="13", special="13"),
            )
            trains = self.k.search_train("서울", "부산")
        self.assertEqual(len(trains), 1)
        self.assertEqual(trains[0].train_no, "A")

    def test_include_no_seats_returns_all(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = self._search_response(
                make_train_data(train_no="A", general="11"),
                make_train_data(train_no="B", general="13", special="13"),
            )
            trains = self.k.search_train("서울", "부산", include_no_seats=True)
        self.assertEqual(len(trains), 2)

    def test_include_waiting_list_returns_waiting_trains(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = self._search_response(
                make_train_data(train_no="A", general="11"),
                make_train_data(train_no="B", general="13", special="13", wait="9"),
            )
            trains = self.k.search_train("서울", "부산", include_waiting_list=True)
        ids = {t.train_no for t in trains}
        self.assertEqual(ids, {"A", "B"})

    def test_no_seats_at_all_raises_no_results(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = self._search_response(
                make_train_data(train_no="A", general="13", special="13"),
            )
            with self.assertRaises(NoResultsError):
                self.k.search_train("서울", "부산")

    def test_api_error_propagates(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = make_response({
                "strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "no",
            })
            with self.assertRaises(NoResultsError):
                self.k.search_train("서울", "부산")


class SearchTrainAlldayTests(unittest.TestCase):

    def setUp(self):
        self.k = make_logged_in_korail()

    def test_paginates_until_no_results(self):
        responses = [
            [make_train_data(train_no="A", dep_time="100000", general="11"),
             make_train_data(train_no="B", dep_time="110000", general="11")],
            [make_train_data(train_no="C", dep_time="120000", general="11")],
        ]
        call_count = {'n': 0}

        def side_effect(url, **kwargs):
            i = call_count['n']
            call_count['n'] += 1
            if i < len(responses):
                return make_response({
                    "strResult": "SUCC",
                    "trn_infos": {"trn_info": responses[i]},
                })
            return make_response({
                "strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "no",
            })

        with patch.object(self.k._session, 'get', side_effect=side_effect):
            trains = self.k.search_train_allday("서울", "부산", "20260601", "100000")
        self.assertEqual([t.train_no for t in trains], ["A", "B", "C"])

    def test_stops_at_2359(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = make_response({
                "strResult": "SUCC",
                "trn_infos": {"trn_info": [
                    make_train_data(train_no="A", dep_time="235900", general="11"),
                ]},
            })
            trains = self.k.search_train_allday("서울", "부산", "20260601", "230000")
        self.assertEqual(len(trains), 1)
        # 한 번만 호출되고 멈춰야 한다
        self.assertEqual(mg.call_count, 1)

    def test_empty_result_raises_no_results(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = make_response({
                "strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "no",
            })
            with self.assertRaises(NoResultsError):
                self.k.search_train_allday("서울", "부산", "20260601", "100000")


# ---------------------------------------------------------------------------
# Korail.reserve
# ---------------------------------------------------------------------------

class ReserveTests(unittest.TestCase):

    def setUp(self):
        self.k = make_logged_in_korail()

    def _reserve_response(self):
        return make_response({
            "strResult": "SUCC",
            "h_pnr_no": "PNR1234",
        })

    def _reservations_response(self):
        return make_response({
            "strResult": "SUCC",
            "jrny_infos": {"jrny_info": [{"train_infos": {"train_info": [
                make_reservation_data(h_pnr_no="PNR1234"),
            ]}}]},
        })

    def _patch_session(self, urls):
        """url 부분 매칭으로 응답을 매핑."""
        def side_effect(url, **kwargs):
            for needle, resp in urls.items():
                if needle in url:
                    return resp
            raise AssertionError(f"Unexpected URL: {url}")
        return patch.object(self.k._session, 'get', side_effect=side_effect)

    def test_sold_out_train_raises(self):
        train = Train(make_train_data(general="13", special="13"))
        with self.assertRaises(SoldOutError):
            self.k.reserve(train)

    def test_general_only_without_general_seat_raises(self):
        train = Train(make_train_data(general="13", special="11"))
        with self.assertRaises(SoldOutError):
            self.k.reserve(train, option=ReserveOption.GENERAL_ONLY)

    def test_special_only_without_special_seat_raises(self):
        train = Train(make_train_data(general="11", special="13"))
        with self.assertRaises(SoldOutError):
            self.k.reserve(train, option=ReserveOption.SPECIAL_ONLY)

    def test_general_first_picks_general_when_available(self):
        train = Train(make_train_data(general="11", special="11"))
        captured = {}

        def side_effect(url, **kwargs):
            if 'TicketReservation' in url:
                captured['params'] = kwargs.get('params', {})
                return self._reserve_response()
            if 'ReservationView' in url:
                return self._reservations_response()
            raise AssertionError(url)

        with patch.object(self.k._session, 'get', side_effect=side_effect):
            self.k.reserve(train, option=ReserveOption.GENERAL_FIRST)
        self.assertEqual(captured['params']['txtPsrmClCd1'], '1')

    def test_special_first_picks_special_when_available(self):
        train = Train(make_train_data(general="11", special="11"))
        captured = {}

        def side_effect(url, **kwargs):
            if 'TicketReservation' in url:
                captured['params'] = kwargs.get('params', {})
                return self._reserve_response()
            if 'ReservationView' in url:
                return self._reservations_response()
            raise AssertionError(url)

        with patch.object(self.k._session, 'get', side_effect=side_effect):
            self.k.reserve(train, option=ReserveOption.SPECIAL_FIRST)
        self.assertEqual(captured['params']['txtPsrmClCd1'], '2')

    def test_try_waiting_falls_back_to_waiting_list(self):
        train = Train(make_train_data(general="13", special="13", wait="9"))
        captured = {}

        def side_effect(url, **kwargs):
            if 'TicketReservation' in url:
                captured['params'] = kwargs.get('params', {})
                return self._reserve_response()
            if 'ReservationView' in url:
                return self._reservations_response()
            raise AssertionError(url)

        with patch.object(self.k._session, 'get', side_effect=side_effect):
            self.k.reserve(train, try_waiting=True)
        # txtJobId 1102 = 예약대기, 1101 = 일반 예약
        self.assertEqual(captured['params']['txtJobId'], '1102')

    def test_try_waiting_special_only_still_raises(self):
        train = Train(make_train_data(general="13", special="13", wait="9"))
        with self.assertRaises(SoldOutError):
            self.k.reserve(train, option=ReserveOption.SPECIAL_ONLY, try_waiting=True)

    def test_reserve_returns_matching_reservation(self):
        train = Train(make_train_data(general="11"))
        with self._patch_session({
            'TicketReservation': self._reserve_response(),
            'ReservationView': self._reservations_response(),
        }):
            rsv = self.k.reserve(train)
        self.assertIsInstance(rsv, Reservation)
        self.assertEqual(rsv.rsv_id, 'PNR1234')


# ---------------------------------------------------------------------------
# Korail.reservations / tickets / cancel
# ---------------------------------------------------------------------------

class ReservationsTests(unittest.TestCase):

    def setUp(self):
        self.k = make_logged_in_korail()

    def test_returns_list_of_reservations(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = make_response({
                "strResult": "SUCC",
                "jrny_infos": {"jrny_info": [{"train_infos": {"train_info": [
                    make_reservation_data(),
                ]}}]},
            })
            rsvs = self.k.reservations()
        self.assertEqual(len(rsvs), 1)
        self.assertIsInstance(rsvs[0], Reservation)

    def test_no_results_returns_empty_list(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = make_response({
                "strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "no",
            })
            self.assertEqual(self.k.reservations(), [])

    def test_cancel_returns_true_on_success(self):
        rsv = Reservation(make_reservation_data())
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = make_response({"strResult": "SUCC"})
            self.assertTrue(self.k.cancel(rsv))


class TicketsTests(unittest.TestCase):

    def setUp(self):
        self.k = make_logged_in_korail()

    def test_no_results_returns_empty_list(self):
        with patch.object(self.k._session, 'get') as mg:
            mg.return_value = make_response({
                "strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "no",
            })
            self.assertEqual(self.k.tickets(), [])

    def test_returns_ticket_objects_with_seat_info(self):
        ticket_info = {
            'ticket_list': [{'train_info': [dict(
                make_train_data(),
                h_seat_no_end="3A",
                h_seat_cnt="001",
                h_buy_ps_nm="홍길동",
                h_orgtk_sale_dt="20260601",
                h_orgtk_wct_no="W1",
                h_orgtk_ret_sale_dt="20260601",
                h_orgtk_sale_sqno="S1",
                h_orgtk_ret_pwd="PW",
                h_rcvd_amt="00059800",
                h_srcar_no="3",
                h_seat_no="3A",
            )]}],
        }
        list_resp = make_response({
            "strResult": "SUCC",
            "reservation_list": [ticket_info],
        })
        seat_resp = make_response({
            "strResult": "SUCC",
            "ticket_infos": {"ticket_info": [{"tk_seat_info": [{"h_seat_no": "3A"}]}]},
        })

        def side_effect(url, **kwargs):
            if 'MyTicketList' in url:
                return list_resp
            if 'SelTicketInfo' in url:
                return seat_resp
            raise AssertionError(url)

        with patch.object(self.k._session, 'get', side_effect=side_effect):
            tickets = self.k.tickets()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].seat_no, '3A')


# ---------------------------------------------------------------------------
# 회귀: 두 Korail 인스턴스는 상태를 공유하지 않아야 한다
# ---------------------------------------------------------------------------

class InstanceIsolationTests(unittest.TestCase):
    """과거에는 _session, membership_number 등이 클래스 속성이라 인스턴스 간 공유됐다."""

    def test_sessions_are_independent(self):
        a = Korail("11111111", "pw", auto_login=False)
        b = Korail("22222222", "pw", auto_login=False)
        self.assertIsNot(a._session, b._session)

    def test_member_info_does_not_leak(self):
        a = Korail("11111111", "pw", auto_login=False)
        b = Korail("22222222", "pw", auto_login=False)
        a.membership_number = "A_NUMBER"
        a.name = "A_NAME"
        self.assertIsNone(b.membership_number)
        self.assertIsNone(b.name)

    def test_login_key_is_per_instance(self):
        a = Korail("11111111", "pw", auto_login=False)
        b = Korail("22222222", "pw", auto_login=False)
        a._key = "a_key"
        self.assertNotEqual(b._key, "a_key")

    def test_session_header_mutation_does_not_leak(self):
        a = Korail("11111111", "pw", auto_login=False)
        b = Korail("22222222", "pw", auto_login=False)
        a._session.headers.update({'X-Test': 'a'})
        self.assertNotIn('X-Test', b._session.headers)


if __name__ == '__main__':
    unittest.main()
