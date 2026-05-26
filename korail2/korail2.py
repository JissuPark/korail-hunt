"""
    korail2.korail2
    ~~~~~~~~~~~~~~~

    :copyright: (c) 2014 by Taehoon Kim.
    :license: BSD, see LICENSE for more details.
"""
import base64
import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from functools import reduce

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

EMAIL_REGEX = re.compile(r"[^@]+@[^@]+\.[^@]+")
PHONE_NUMBER_REGEX = re.compile(r"(\d{3})-(\d{3,4})-(\d{4})")

SCHEME = "https"
KORAIL_HOST = "smart.letskorail.com"
KORAIL_PORT = "443"

KORAIL_DOMAIN = "%s://%s:%s" % (SCHEME, KORAIL_HOST, KORAIL_PORT)
KORAIL_MOBILE = "%s/classes/com.korail.mobile" % KORAIL_DOMAIN

KORAIL_LOGIN = "%s.login.Login" % KORAIL_MOBILE
KORAIL_LOGOUT = "%s.common.logout" % KORAIL_MOBILE
KORAIL_SEARCH_SCHEDULE = "%s.seatMovie.ScheduleView" % KORAIL_MOBILE
KORAIL_TICKETRESERVATION = "%s.certification.TicketReservation" % KORAIL_MOBILE
KORAIL_REFUND = "%s.refunds.RefundsRequest" % KORAIL_MOBILE
KORAIL_MYTICKETLIST = "%s.myTicket.MyTicketList" % KORAIL_MOBILE
KORAIL_MYTICKET_SEAT = "%s.refunds.SelTicketInfo" % KORAIL_MOBILE

KORAIL_MYRESERVATIONLIST = "%s.reservation.ReservationView" % KORAIL_MOBILE
KORAIL_CANCEL = "%s.reservationCancel.ReservationCancelChk" % KORAIL_MOBILE

KORAIL_STATION_DB = "%s.common.stationinfo?device=ip" % KORAIL_MOBILE
KORAIL_STATION_DB_DATA = "%s.common.stationdata" % KORAIL_MOBILE
KORAIL_EVENT = "%s.common.event" % KORAIL_MOBILE
KORAIL_PAYMENT = "%s/ebizmw/PrdPkgMainList.do" % KORAIL_DOMAIN
KORAIL_PAYMENT_VOUCHER = "%s/ebizmw/PrdPkgBoucherView.do" % KORAIL_DOMAIN

KORAIL_CODE = "%s.common.code.do" % KORAIL_MOBILE

DEFAULT_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 5.1.1; Nexus 4 Build/LMY48T)"


class Schedule:
    """Korail train object. Highly inspired by `korail.py
    <https://raw.githubusercontent.com/devxoul/korail/master/korail/korail.py>`_
    by `Suyeol Jeon <http://xoul.kr/>`_ at 2014.
    """

    #: 기차 종류
    #: 00: KTX
    #: 01: 새마을호
    #: 02: 무궁화호
    #: 03: 통근열차
    #: 04: 누리로
    #: 05: 전체 (검색시에만 사용)
    #: 06: 공학직통
    #: 07: KTX-산천
    #: 08: ITX-새마을
    #: 09: ITX-청춘
    train_type = None  # h_trn_clsf_cd, selGoTrain

    train_group = None  # h_trn_gp_cd

    #: 기차 종류 이름
    train_type_name = None  # h_trn_clsf_nm

    #: 기차 번호
    train_no = None  # h_trn_no

    #: 출발역 이름
    dep_name = None  # h_dpt_rs_stn_nm

    #: 출발역 코드
    dep_code = None  # h_dpt_rs_stn_cd

    #: 출발 날짜 (yyyyMMdd)
    dep_date = None  # h_dpt_dt

    #: 출발 시각 (hhmmss)
    dep_time = None  # h_dpt_tm

    #: 도착역 이름
    arr_name = None  # h_arv_rs_stn_nm

    #: 도착역 코드
    arr_code = None  # h_arv_rs_stn_cd

    #: 도착 날짜 (yyyyMMdd)
    arr_date = None  # h_arv_dt

    #: 도착 시각 (hhmmss)
    arr_time = None  # h_arv_tm

    #: 운행 날짜 (yyyyMMdd)
    run_date = None  # h_run_dt

    def __init__(self, data):
        self.train_type = data.get('h_trn_clsf_cd')
        self.train_type_name = data.get('h_trn_clsf_nm')
        self.train_group = data.get('h_trn_gp_cd')
        self.train_no = data.get('h_trn_no')
        self.delay_time = data.get('h_expct_dlay_hr')

        self.dep_name = data.get('h_dpt_rs_stn_nm')
        self.dep_code = data.get('h_dpt_rs_stn_cd')
        self.dep_date = data.get('h_dpt_dt')
        self.dep_time = data.get('h_dpt_tm')

        self.arr_name = data.get('h_arv_rs_stn_nm')
        self.arr_code = data.get('h_arv_rs_stn_cd')
        self.arr_date = data.get('h_arv_dt')
        self.arr_time = data.get('h_arv_tm')

        self.run_date = data.get('h_run_dt')

    def __repr__(self):
        dep_time = "%s:%s" % (self.dep_time[:2], self.dep_time[2:4])
        arr_time = "%s:%s" % (self.arr_time[:2], self.arr_time[2:4])
        dep_date = "%s월 %s일" % (int(self.dep_date[4:6]), int(self.dep_date[6:]))
        return '[%s] %s, %s~%s(%s~%s)' % (
            self.train_type_name,
            dep_date,
            self.dep_name,
            self.arr_name,
            dep_time,
            arr_time,
        )


class Train(Schedule):
    #: 지연 시간 (hhmm)
    delay_time = None  # h_expct_dlay_hr

    #: 예약 가능 여부
    reserve_possible = False  # h_rsv_psb_flg ('Y' or 'N')

    #: 예약 가능 여부
    reserve_possible_name = None  # h_rsv_psb_nm

    #: 특실 예약가능 여부
    #: 00: 특실 없음
    #: 11: 예약 가능
    #: 13: 매진
    special_seat = None  # h_spe_rsv_cd

    #: 일반실 예약가능 여부
    #: 00: 일반실 없음
    #: 11: 예약 가능
    #: 13: 매진
    general_seat = None  # h_gen_rsv_cd

    #: 예약 대기 가능 여부
    #: -2: 좌석 있음
    #:  9: 예약 대기 (일반석)
    #:  0: 예약 대기 없음 (매진)
    wait_reserve_flag = None  # h_wait_rsv_flg

    def __init__(self, data):
        super().__init__(data)
        self.reserve_possible = data.get('h_rsv_psb_flg')
        self.reserve_possible_name = data.get('h_rsv_psb_nm')

        self.special_seat = data.get('h_spe_rsv_cd')
        self.general_seat = data.get('h_gen_rsv_cd')

        self.wait_reserve_flag = data.get('h_wait_rsv_flg')
        if self.wait_reserve_flag:
            self.wait_reserve_flag = int(self.wait_reserve_flag)

    def __repr__(self):
        repr_str = super().__repr__()

        if self.reserve_possible_name is not None:
            seats = []
            if self.has_special_seat():
                seats.append("특실")

            if self.has_general_seat():
                seats.append("일반실")

            if self.has_general_waiting_list():
                seats.append("예약 대기(일반)")

            repr_str += " " + (",".join(seats)) + " " + self.reserve_possible_name.replace('\n', ' ')

        return repr_str

    def has_special_seat(self):
        return self.special_seat == '11'

    def has_general_seat(self):
        return self.general_seat == '11'

    def has_seat(self):
        return self.has_general_seat() or self.has_special_seat()

    def has_waiting_list(self):
        return self.has_general_waiting_list()

    def has_general_waiting_list(self):
        return self.wait_reserve_flag == 9


class Ticket(Train):
    """Ticket object"""

    #: 호차 번호
    car_no = None  # h_srcar_no

    #: 자리 갯수
    seat_no_count = None  # h_seat_cnt  ex) 001

    #: 자리 번호
    seat_no = None  # h_seat_no

    #: 자리 번호
    seat_no_end = None  # h_seat_no_end

    #: 구매자 성함
    buyer_name = None  # h_buy_ps_nm

    #: 구매 날짜 (yyyyMMdd)
    sale_date = None  # h_orgtk_sale_dt

    #: 구매 정보1
    sale_info1 = None  # h_orgtk_wct_no

    #: 구매 정보2
    sale_info2 = None  # h_orgtk_ret_sale_dt

    #: 구매 정보3
    sale_info3 = None  # h_orgtk_sale_sqno

    #: 구매 정보4
    sale_info4 = None  # h_orgtk_ret_pwd

    #: 구매 가격
    price = None  # h_rcvd_amt  ex) 00013900

    def __init__(self, data):
        raw_data = data['ticket_list'][0]['train_info'][0]
        super().__init__(raw_data)

        self.seat_no_end = raw_data.get('h_seat_no_end')
        self.seat_no_count = int(raw_data.get('h_seat_cnt'))

        self.buyer_name = raw_data.get('h_buy_ps_nm')
        self.sale_date = raw_data.get('h_orgtk_sale_dt')
        self.sale_info1 = raw_data.get('h_orgtk_wct_no')
        self.sale_info2 = raw_data.get('h_orgtk_ret_sale_dt')
        self.sale_info3 = raw_data.get('h_orgtk_sale_sqno')
        self.sale_info4 = raw_data.get('h_orgtk_ret_pwd')
        self.price = int(raw_data.get('h_rcvd_amt'))

        self.car_no = raw_data.get('h_srcar_no')
        self.seat_no = raw_data.get('h_seat_no')

    def __repr__(self):
        # 의도적으로 Train.__repr__을 건너뛰고 Schedule.__repr__만 사용한다.
        # (예약 가능 여부 표시는 발권된 티켓에는 불필요)
        repr_str = Schedule.__repr__(self)

        repr_str += " => %s호" % self.car_no

        if int(self.seat_no_count) != 1:
            repr_str += " %s~%s" % (self.seat_no, self.seat_no_end)
        else:
            repr_str += " %s" % self.seat_no

        repr_str += ", %s원" % self.price

        return repr_str

    def get_ticket_no(self):
        return "-".join(map(str, (self.sale_info1, self.sale_info2, self.sale_info3, self.sale_info4)))


class Passenger:
    """승객. Passenger List를 검색과 예약에 쓰도록 한다."""
    typecode = None        # txtPsgTpCd1    : '1',   #손님 종류 (어른 1, 어린이 3)
    discount_type = '000'  # txtDiscKndCd1  : '000', #할인 타입 (경로, 동반유아, 군장병 등..)
    count = 1              # txtCompaCnt1   : '1',   #인원수
    card = ''              # txtCardCode_1  : '',    #할인카드 종류
    card_no = ''           # txtCardNo_1    : '',    #할인카드 번호
    card_pw = ''           # txtCardPw_1    : '',    #할인카드 비밀번호

    @staticmethod
    def reduce(passenger_list):
        """Reduce passenger's list."""
        if list(filter(lambda x: not isinstance(x, Passenger), passenger_list)):
            raise TypeError("Passengers must be based on Passenger")

        groups = itertools.groupby(passenger_list, lambda x: x.group_key())
        return [merged for merged in (reduce(lambda a, b: a + b, g) for _, g in groups) if merged.count > 0]

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Passenger is abstract class. Do not make instance.")

    def __init_internal__(self, typecode, count=1, discount_type='000', card='', card_no='', card_pw=''):
        self.typecode = typecode
        self.count = count
        self.discount_type = discount_type
        self.card = card
        self.card_no = card_no
        self.card_pw = card_pw

    def __add__(self, other):
        assert isinstance(other, self.__class__)
        if self.group_key() == other.group_key():
            return self.__class__(count=self.count + other.count, discount_type=self.discount_type, card=self.card,
                                  card_no=self.card_no, card_pw=self.card_pw)
        else:
            raise TypeError(
                "other's group_key(%s) is not equal to self's group_key(%s)." % (other.group_key(), self.group_key()))

    def group_key(self):
        """get group string from attributes except count"""
        return "%s_%s_%s_%s_%s" % (self.typecode, self.discount_type, self.card, self.card_no, self.card_pw)

    def get_dict(self, index):
        assert isinstance(index, int)
        index = str(index)
        return {
            'txtPsgTpCd' + index: self.typecode,
            'txtDiscKndCd' + index: self.discount_type,
            'txtCompaCnt' + index: self.count,
            'txtCardCode_' + index: self.card,
            'txtCardNo_' + index: self.card_no,
            'txtCardPw_' + index: self.card_pw,
        }


# noinspection PyMissingConstructor
class AdultPassenger(Passenger):
    def __init__(self, count=1, discount_type='000', card='', card_no='', card_pw=''):
        Passenger.__init_internal__(self, '1', count, discount_type, card, card_no, card_pw)


# noinspection PyMissingConstructor
class ChildPassenger(Passenger):
    def __init__(self, count=1, discount_type='000', card='', card_no='', card_pw=''):
        Passenger.__init_internal__(self, '3', count, discount_type, card, card_no, card_pw)


# noinspection PyMissingConstructor
class ToddlerPassenger(Passenger):
    def __init__(self, count=1, discount_type='321', card='', card_no='', card_pw=''):
        Passenger.__init_internal__(self, '3', count, discount_type, card, card_no, card_pw)


# noinspection PyMissingConstructor
class SeniorPassenger(Passenger):
    def __init__(self, count=1, discount_type='131', card='', card_no='', card_pw=''):
        Passenger.__init_internal__(self, '1', count, discount_type, card, card_no, card_pw)


class TrainType:
    KTX = "100"             # "KTX, KTX-산천"
    SAEMAEUL = "101"        # "새마을호"
    MUGUNGHWA = "102"       # "무궁화호"
    TONGGUEN = "103"        # "통근열차"
    NURIRO = "102"          # "누리로"
    ALL = "109"             # "전체"
    AIRPORT = "105"         # "공항직통"
    KTX_SANCHEON = "100"    # "KTX-산천"
    ITX_SAEMAEUL = "101"    # "ITX-새마을"
    ITX_CHEONGCHUN = "104"  # "ITX-청춘"

    def __init__(self):
        raise NotImplementedError("Do not make instance.")


class ReserveOption:
    GENERAL_FIRST = "GENERAL_FIRST"  # 일반실 우선
    GENERAL_ONLY = "GENERAL_ONLY"    # 일반실만
    SPECIAL_FIRST = "SPECIAL_FIRST"  # 특실 우선
    SPECIAL_ONLY = "SPECIAL_ONLY"    # 특실만

    def __init__(self):
        raise NotImplementedError("Do not make instance.")


class Reservation(Train):
    """Reservation object"""

    #: 예약번호
    rsv_id = None  # h_pnr_no

    #: 여정 번호
    journey_no = None  # txtJrnySqno

    #: 여정 카운트
    journey_cnt = None  # txtJrnyCnt

    #: 예약변경 번호
    rsv_chg_no = "00000"

    #: 자리 갯수
    seat_no_count = None  # h_tot_seat_cnt  ex) 001

    #: 결제 기한 날짜
    buy_limit_date = None  # h_ntisu_lmt_dt

    #: 결제 기한 시간
    buy_limit_time = None  # h_ntisu_lmt_tm

    #: 예약 가격
    price = None  # h_rsv_amt  ex) 00013900

    #: 열차 번호 (Not implemented)
    car_no = None  # h_srcar_no

    #: 자리 번호 (Not implemented)
    seat_no = None  # h_seat_no

    #: 자리 번호 (Not implemented)
    seat_no_end = None  # h_seat_no_end

    def __init__(self, data):
        super().__init__(data)
        # 응답에 h_dpt_dt / h_arv_dt 가 빠져 있어 h_run_dt 로 채운다.
        self.dep_date = data.get('h_run_dt')
        self.arr_date = data.get('h_run_dt')

        self.rsv_id = data.get('h_pnr_no')
        self.seat_no_count = int(data.get('h_tot_seat_cnt'))
        self.buy_limit_date = data.get('h_ntisu_lmt_dt')
        self.buy_limit_time = data.get('h_ntisu_lmt_tm')
        self.price = int(data.get('h_rsv_amt'))
        self.journey_no = data.get('txtJrnySqno', "001")
        self.journey_cnt = data.get('txtJrnyCnt', "01")
        self.rsv_chg_no = data.get('hidRsvChgNo', "00000")

    def __repr__(self):
        repr_str = super().__repr__()

        repr_str += ", %s원(%s석)" % (self.price, self.seat_no_count)

        buy_limit_time = "%s:%s" % (self.buy_limit_time[:2], self.buy_limit_time[2:4])
        buy_limit_date = "%s월 %s일" % (int(self.buy_limit_date[4:6]), int(self.buy_limit_date[6:]))

        repr_str += ", 구입기한 %s %s" % (buy_limit_date, buy_limit_time)

        return repr_str


class ExceptionForm(type):
    codes = set()

    def __contains__(cls, item):
        return item in cls.codes


class KorailError(Exception, metaclass=ExceptionForm):
    """Korail Base Error Class"""

    def __init__(self, msg, code):
        self.msg = msg
        self.code = code

    def __str__(self):
        return "%s (%s)" % (self.msg, self.code)


class NeedToLoginError(KorailError):
    """Korail NeedToLogin Error Class"""
    codes = {'P058'}

    def __init__(self, code=None):
        KorailError.__init__(self, "Need to Login", code)


class NoResultsError(KorailError):
    """Korail NoResults Error Class"""
    codes = {
        'P100',
        'WRG000000',
        'WRD000061',  # 직통열차는 없지만, 환승으로 조회 가능합니다.
        'WRT300005',
    }

    def __init__(self, code=None):
        KorailError.__init__(self, "No Results", code)


class SoldOutError(KorailError):
    codes = {'ERR211161'}

    def __init__(self, code=None):
        KorailError.__init__(self, "Sold out", code)


class Korail:
    """Korail object"""

    _device = 'AD'
    _version = '190617001'

    def __init__(self, korail_id, korail_pw, auto_login=True, want_feedback=False):
        # 인스턴스 단위 세션/키. 과거에는 클래스 속성이라 여러 Korail 인스턴스가
        # 세션과 멤버 정보를 공유하는 버그가 있었다.
        self._session = requests.session()
        self._session.headers.update({'User-Agent': DEFAULT_USER_AGENT})
        self._key = 'korail1234567890'
        self._idx = None

        self.membership_number = None
        self.name = None
        self.email = None

        self.korail_id = korail_id
        self.korail_pw = korail_pw
        self.want_feedback = want_feedback
        self.logined = False
        if auto_login:
            self.login(korail_id, korail_pw)

    def __enc_password(self, password):
        url = KORAIL_CODE
        data = {
            'code': "app.login.cphd",
        }

        r = self._session.post(url, data=data)
        j = json.loads(r.text)

        if j['strResult'] == 'SUCC' and j.get('app.login.cphd') is not None:
            self._idx = j['app.login.cphd']['idx']
            key = j['app.login.cphd']['key']

            encrypt_key = key.encode(encoding='utf-8', errors='strict')
            iv = key[:16].encode(encoding='utf-8', errors='strict')
            cipher = AES.new(encrypt_key, AES.MODE_CBC, iv)

            padded_data = pad(password.encode("utf-8"), AES.block_size)

            return base64.b64encode(base64.b64encode(cipher.encrypt(padded_data))).decode("utf-8")
        else:
            return False

    def login(self, korail_id=None, korail_pw=None):
        """Login to Korail server.

:param korail_id : `Korail membership number` or `phone number` or `email`
    membership   : xxxxxxxx (8 digits)
    phone number : xxx-xxxx-xxxx
    email        : xxx@xxx.xxx
:param korail_pw : Korail account korail_pw

First, you need to create a Korail object.

    >>> from korail2 import *
    >>> korail = Korail("12345678", YOUR_PASSWORD) # with membership number
    >>> korail = Korail("carpedm20@gmail.com", YOUR_PASSWORD) # with email
    >>> korail = Korail("010-9964-xxxx", YOUR_PASSWORD) # with phone number

If you do not want login automatically,

    >>> korail = Korail("12345678", YOUR_PASSWORD, auto_login=False)
    >>> korail.login()
    True

When you want change ID using existing object,

    >>> korail.login(ANOTHER_ID, ANOTHER_PASSWORD)
    True
"""
        if korail_id is None:
            korail_id = self.korail_id
        else:
            self.korail_id = korail_id

        if korail_pw is None:
            korail_pw = self.korail_pw
        else:
            self.korail_pw = korail_pw

        if EMAIL_REGEX.match(korail_id):
            txt_input_flg = '5'
        elif PHONE_NUMBER_REGEX.match(korail_id):
            txt_input_flg = '4'
        else:
            txt_input_flg = '2'

        url = KORAIL_LOGIN
        data = {
            'Device': self._device,
            'Version': '231231001',  # HACK: 서버가 구버전을 거부함
            # 2 : for membership number,
            # 4 : for phone number,
            # 5 : for email,
            'txtInputFlg': txt_input_flg,
            'txtMemberNo': korail_id,
            'txtPwd': self.__enc_password(korail_pw),
            'idx': self._idx,
        }

        r = self._session.post(url, data=data)
        j = json.loads(r.text)

        if j['strResult'] == 'SUCC' and j.get('strMbCrdNo') is not None:
            self._key = j['Key']
            self.membership_number = j['strMbCrdNo']
            self.name = j['strCustNm']
            self.email = j['strEmailAdr']
            self.logined = True
            return True
        else:
            self.logined = False
            return False

    def logout(self):
        """Logout from Korail server"""
        url = KORAIL_LOGOUT
        self._session.get(url)
        self.logined = False

    def _result_check(self, j):
        """Result data check"""
        if self.want_feedback:
            print(j['h_msg_txt'])

        if j['strResult'] == 'FAIL':
            h_msg_cd = j.get('h_msg_cd')
            h_msg_txt = j.get('h_msg_txt')
            for exc_class in (NoResultsError, NeedToLoginError, SoldOutError):
                if h_msg_cd in exc_class:
                    raise exc_class(h_msg_cd)
            raise KorailError(h_msg_txt, h_msg_cd)

        return True

    def search_train_allday(self, dep, arr, date=None, time=None, train_type=TrainType.ALL,
                            passengers=None, include_no_seats=False):
        """Search all trains for specific time and date."""
        min1 = timedelta(minutes=1)
        all_trains = []
        dep_time = time
        for _ in range(15):  # 최대 15번 호출
            try:
                trains = self.search_train(dep, arr, date, dep_time, train_type, passengers, True)
                all_trains.extend(trains)
                # 마지막 승차권의 출발시각이 23:59이면 다음 날로 넘어가지 않게 중지
                last_dep_time = datetime.strptime(all_trains[-1].dep_time, "%H%M%S")
                if last_dep_time.hour == 23 and last_dep_time.minute == 59:
                    break
                # 마지막 열차시각 + 1분으로 다음 페이지 검색
                t = last_dep_time + min1
                dep_time = t.strftime("%H%M%S")
            except NoResultsError:
                break

        if not include_no_seats:
            all_trains = [t for t in all_trains if t.has_seat()]

        if len(all_trains) == 0:
            raise NoResultsError()

        return all_trains

    def search_train(self, dep, arr, date=None, time=None, train_type=TrainType.ALL,
                     passengers=None, include_no_seats=False, include_waiting_list=False):
        """Search trains for specific time and date.

:param dep: A departure station in Korean  ex) '서울'
:param arr: A arrival station in Korean  ex) '부산'
:param date: (optional) A departure date in `yyyyMMdd` format
:param time: (optional) A departure time in `hhmmss` format
:param train_type: (optional) A type of train
                   - 00: KTX, KTX-산천
                   - 01: 새마을호
                   - 02: 무궁화호
                   - 03: 통근열차
                   - 04: 누리로
                   - 05: 전체 (기본값)
                   - 06: 공항직통
                   - 07: KTX-산천
                   - 08: ITX-새마을
                   - 09: ITX-청춘
:param passengers=None: (optional) List of Passenger Objects. None means 1 AdultPassenger.
:param include_no_seats=False: (optional) When True, a result includes trains which has no seats.
:param include_waiting_list=False: (optional) When True, a result also includes trains which has no seats but can make a wait reservation(예약 대기).

    >>> dep = '서울'
    >>> arr = '동대구'
    >>> date = '20140815'
    >>> time = '144000'
    >>> trains = korail.search_train(dep, arr, date, time)
"""
        # 코레일 API는 한국시간(KST) 기준
        kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
        if date is None:
            date = kst_now.strftime("%Y%m%d")
        if time is None:
            time = kst_now.strftime("%H%M%S")

        if passengers is None:
            passengers = [AdultPassenger()]

        passengers = Passenger.reduce(passengers)

        adult_count = sum(p.count for p in passengers if isinstance(p, AdultPassenger))
        child_count = sum(p.count for p in passengers if isinstance(p, ChildPassenger))
        toddler_count = sum(p.count for p in passengers if isinstance(p, ToddlerPassenger))
        senior_count = sum(p.count for p in passengers if isinstance(p, SeniorPassenger))

        url = KORAIL_SEARCH_SCHEDULE
        data = {
            'Device': self._device,
            'radJobId': '1',
            'selGoTrain': train_type,
            'txtCardPsgCnt': '0',
            'txtGdNo': '',
            'txtGoAbrdDt': date,
            'txtGoEnd': arr,
            'txtGoHour': time,
            'txtGoStart': dep,
            'txtJobDv': '',
            'txtMenuId': '11',
            'txtPsgFlg_1': adult_count,    # 어른
            'txtPsgFlg_2': child_count,    # 어린이
            'txtPsgFlg_8': toddler_count,  # 유아
            'txtPsgFlg_3': senior_count,   # 경로
            'txtPsgFlg_4': '0',            # 중증 장애인
            'txtPsgFlg_5': '0',            # 경증 장애인
            'txtSeatAttCd_2': '000',
            'txtSeatAttCd_3': '000',
            'txtSeatAttCd_4': '015',
            'txtTrnGpCd': train_type,
            'Version': self._version,
        }

        r = self._session.get(url, params=data)
        j = json.loads(r.text)

        if self._result_check(j):
            train_infos = j['trn_infos']['trn_info']
            trains = [Train(info) for info in train_infos]

            filter_fns = [lambda x: x.has_seat()]
            if include_no_seats:
                filter_fns.append(lambda x: not x.has_seat())
            if include_waiting_list:
                filter_fns.append(lambda x: x.has_waiting_list())

            trains = [t for t in trains if any(f(t) for f in filter_fns)]

            if len(trains) == 0:
                raise NoResultsError()

            return trains

    def reserve(self, train, passengers=None, option=ReserveOption.GENERAL_FIRST, try_waiting=False):
        """Reserve a train.

:param train: An instance of `Train`.
:param passengers=None: (optional) List of Passenger Objects. None means 1 AdultPassenger.
:param option=ReserveOption.GENERAL_FIRST : (optional)

When tickets are not enough much for passengers, it raises SoldOutError.

If you want to select priority of seat grade, general or special,
there are 4 options in ReserveOption class.

- GENERAL_FIRST : Economic than Comfortable.
- GENERAL_ONLY  : Reserve only general seats.
- SPECIAL_FIRST : Comfortable than Economic.
- SPECIAL_ONLY  : Special only.

:param try_waiting: (optional) When the train allows waiting, enroll for the
                    waiting list instead of failing in case there are no seats.
        """
        reserving_seat = True
        seat_type = None
        try:
            if train.has_seat() is False:
                # 둘 다 없으면 매진
                raise SoldOutError()
            elif option == ReserveOption.GENERAL_ONLY:
                if train.has_general_seat():
                    seat_type = '1'
                else:
                    raise SoldOutError()
            elif option == ReserveOption.SPECIAL_ONLY:
                if train.has_special_seat():
                    seat_type = '2'
                else:
                    raise SoldOutError()
            elif option == ReserveOption.GENERAL_FIRST:
                seat_type = '1' if train.has_general_seat() else '2'
            elif option == ReserveOption.SPECIAL_FIRST:
                seat_type = '2' if train.has_special_seat() else '1'
        except SoldOutError:
            if try_waiting and option != ReserveOption.SPECIAL_ONLY and train.has_general_waiting_list():
                reserving_seat = False
                seat_type = '1'
            else:
                raise

        if passengers is None:
            passengers = [AdultPassenger()]

        passengers = Passenger.reduce(passengers)
        cnt = sum(p.count for p in passengers)
        url = KORAIL_TICKETRESERVATION
        data = {
            'Device': self._device,
            'Version': self._version,
            'Key': self._key,
            'txtGdNo': '',
            'txtJobId': '1101' if reserving_seat else '1102',
            'txtTotPsgCnt': cnt,
            'txtSeatAttCd1': '000',
            'txtSeatAttCd2': '000',
            'txtSeatAttCd3': '000',
            'txtSeatAttCd4': '015',
            'txtSeatAttCd5': '000',
            'hidFreeFlg': 'N',
            'txtStndFlg': 'N',
            'txtMenuId': '11',
            'txtSrcarCnt': '0',
            'txtJrnyCnt': '1',

            # 여정정보 1
            'txtJrnySqno1': '001',
            'txtJrnyTpCd1': '11',
            'txtDptDt1': train.dep_date,
            'txtDptRsStnCd1': train.dep_code,
            'txtDptTm1': train.dep_time,
            'txtArvRsStnCd1': train.arr_code,
            'txtTrnNo1': train.train_no,
            'txtRunDt1': train.run_date,
            'txtTrnClsfCd1': train.train_type,
            'txtPsrmClCd1': seat_type,
            'txtTrnGpCd1': train.train_group,
            'txtChgFlg1': '',

            # 여정정보 2 (미사용)
            'txtJrnySqno2': '',
            'txtJrnyTpCd2': '',
            'txtDptDt2': '',
            'txtDptRsStnCd2': '',
            'txtDptTm2': '',
            'txtArvRsStnCd2': '',
            'txtTrnNo2': '',
            'txtRunDt2': '',
            'txtTrnClsfCd2': '',
            'txtPsrmClCd2': '',
            'txtChgFlg2': '',
        }

        for index, psg in enumerate(passengers, start=1):
            data.update(psg.get_dict(index))

        r = self._session.get(url, params=data)
        j = json.loads(r.text)
        if self._result_check(j):
            rsv_id = j['h_pnr_no']
            rsvlist = [r for r in self.reservations() if r.rsv_id == rsv_id]
            if len(rsvlist) == 1:
                return rsvlist[0]

    def tickets(self):
        """Get list of tickets"""
        url = KORAIL_MYTICKETLIST
        data = {
            'Device': self._device,
            'Version': self._version,
            'Key': self._key,
            'txtIndex': '1',
            'h_page_no': '1',
            'txtDeviceId': '',
            'h_abrd_dt_from': '',
            'h_abrd_dt_to': '',
        }

        r = self._session.get(url, params=data)
        j = json.loads(r.text)
        try:
            if self._result_check(j):
                ticket_infos = j['reservation_list']

                tickets = []
                for info in ticket_infos:
                    ticket = Ticket(info)
                    seat_url = KORAIL_MYTICKET_SEAT
                    seat_data = {
                        'Device': self._device,
                        'Version': self._version,
                        'Key': self._key,
                        'h_orgtk_wct_no': ticket.sale_info1,
                        'h_orgtk_ret_sale_dt': ticket.sale_info2,
                        'h_orgtk_sale_sqno': ticket.sale_info3,
                        'h_orgtk_ret_pwd': ticket.sale_info4,
                    }
                    sr = self._session.get(seat_url, params=seat_data)
                    sj = json.loads(sr.text)
                    if self._result_check(sj):
                        seat = sj['ticket_infos']['ticket_info'][0]['tk_seat_info'][0]
                        ticket.seat_no = seat.get('h_seat_no')
                        ticket.seat_no_end = None

                    tickets.append(ticket)

                return tickets
        except NoResultsError:
            return []

    def reservations(self):
        """Get my reservations"""
        url = KORAIL_MYRESERVATIONLIST
        data = {
            'Device': self._device,
            'Version': self._version,
            'Key': self._key,
        }
        r = self._session.get(url, params=data)
        j = json.loads(r.text)
        try:
            if self._result_check(j):
                rsv_infos = j['jrny_infos']['jrny_info']

                reserves = []
                for info in rsv_infos:
                    for tinfo in info['train_infos']['train_info']:
                        reserves.append(Reservation(tinfo))
                return reserves
        except NoResultsError:
            return []

    def cancel(self, rsv):
        """Cancel a reservation (refund is for issued tickets)."""
        assert isinstance(rsv, Reservation)
        url = KORAIL_CANCEL
        data = {
            'Device': self._device,
            'Version': self._version,
            'Key': self._key,
            'txtPnrNo': rsv.rsv_id,
            'txtJrnySqno': rsv.journey_no,
            'txtJrnyCnt': rsv.journey_cnt,
            'hidRsvChgNo': rsv.rsv_chg_no,
        }
        r = self._session.get(url, data=data)
        j = json.loads(r.text)
        if self._result_check(j):
            return True
