"""
bot.py 단위 테스트 — 네트워크/봇 연결 없이 파서와 포매터를 검증한다.

Conversation handler 자체는 python-telegram-bot 의 통합 환경이 필요해 여기서
다루지 않는다. 직접 봇을 띄워 수동 검증하라.
"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TelegramError

import bot
from bot import (
    ASK_COUNT,
    ASK_DATE,
    KEY_ADULTS,
    KEY_ARR,
    KEY_DATE,
    KEY_DEP,
    KEY_HUNT_TASKS,
    KEY_LOGIN_ID,
    KEY_PENDING,
    KEY_SELECTED_TRAIN_IDX,
    KEY_SESSIONS,
    KEY_TIME,
    KEY_TRAINS,
    KEY_USERS,
    LOGIN_ID,
    LOGIN_PW,
    PASSENGER_MAX,
    PASSENGER_MIN,
    Session,
    _chat_hunts,
    _count_keyboard,
    _format_hunt_label,
    _help_for,
    _hunt_loop,
    _hunt_spec,
    _load_hunt_spec,
    _show_trains,
    _train_hunt_loop,
    allowed_chat_ids,
    authorized_chat_id,
    build_passengers,
    cb_access,
    cb_revoke,
    check_config,
    clamp_passenger_count,
    cmd_login,
    cmd_logout,
    cmd_reservations,
    cmd_users,
    conv_count,
    conv_option,
    describe_passengers,
    dump_sessions,
    format_reservation_success,
    get_session,
    is_allowed,
    load_users,
    login_id,
    login_pw,
    normalize_korail_id,
    notify_restart,
    parse_date,
    parse_time,
    passengers_of,
    register_commands,
    report_config,
    restore_hunts,
    restore_sessions,
    save_state,
    snapshot_on_stop,
    users,
)
from korail2 import AdultPassenger, ChildPassenger, ReserveOption, SeniorPassenger
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


class NormalizeKorailIdTests(unittest.TestCase):
    # 코레일은 휴대폰번호를 하이픈 형식으로만 인식한다. 붙여 쓴 번호가
    # 회원번호로 오인돼 로그인이 실패하던 문제를 막는다.

    def test_11_digit_mobile_gets_hyphens(self):
        self.assertEqual(normalize_korail_id('01012345678'), '010-1234-5678')

    def test_10_digit_mobile_gets_hyphens(self):
        self.assertEqual(normalize_korail_id('0111234567'), '011-123-4567')

    def test_already_hyphenated_is_untouched(self):
        self.assertEqual(normalize_korail_id('010-1234-5678'), '010-1234-5678')

    def test_8_digit_membership_number_is_untouched(self):
        self.assertEqual(normalize_korail_id('12345678'), '12345678')

    def test_email_is_untouched(self):
        self.assertEqual(normalize_korail_id('hong@example.com'), 'hong@example.com')

    def test_whitespace_stripped(self):
        self.assertEqual(normalize_korail_id('  01012345678  '), '010-1234-5678')

    def test_digits_not_starting_with_zero_are_untouched(self):
        self.assertEqual(normalize_korail_id('12345678901'), '12345678901')


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


class AuthorizedChatIdTests(unittest.TestCase):

    def test_returns_int_when_set(self):
        with patch.dict(os.environ, {'TELEGRAM_AUTHORIZED_CHAT_ID': '12345'}):
            self.assertEqual(authorized_chat_id(), 12345)

    def test_returns_none_when_missing(self):
        env = os.environ.copy()
        env.pop('TELEGRAM_AUTHORIZED_CHAT_ID', None)
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(authorized_chat_id())

    def test_returns_none_when_not_integer(self):
        with patch.dict(os.environ, {'TELEGRAM_AUTHORIZED_CHAT_ID': 'abc'}):
            self.assertIsNone(authorized_chat_id())

    def test_returns_none_when_empty_string(self):
        with patch.dict(os.environ, {'TELEGRAM_AUTHORIZED_CHAT_ID': ''}):
            self.assertIsNone(authorized_chat_id())


class AllowedChatIdsTests(unittest.TestCase):

    def _env(self, **overrides):
        env = os.environ.copy()
        env.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
        env.pop('TELEGRAM_AUTHORIZED_CHAT_ID', None)
        env.update(overrides)
        return patch.dict(os.environ, env, clear=True)

    def test_parses_comma_separated_list(self):
        with self._env(TELEGRAM_ALLOWED_CHAT_IDS='111,222,333'):
            self.assertEqual(allowed_chat_ids(), {111, 222, 333})

    def test_tolerates_whitespace_and_blanks(self):
        with self._env(TELEGRAM_ALLOWED_CHAT_IDS=' 111 , ,222,'):
            self.assertEqual(allowed_chat_ids(), {111, 222})

    def test_skips_non_integer_entries(self):
        with self._env(TELEGRAM_ALLOWED_CHAT_IDS='111,abc,222'):
            self.assertEqual(allowed_chat_ids(), {111, 222})

    def test_empty_when_unset(self):
        with self._env():
            self.assertEqual(allowed_chat_ids(), set())

    def test_absorbs_legacy_single_chat_id(self):
        with self._env(TELEGRAM_AUTHORIZED_CHAT_ID='999'):
            self.assertEqual(allowed_chat_ids(), {999})

    def test_merges_legacy_with_list(self):
        with self._env(TELEGRAM_ALLOWED_CHAT_IDS='111', TELEGRAM_AUTHORIZED_CHAT_ID='999'):
            self.assertEqual(allowed_chat_ids(), {111, 999})


def _ctx():
    """봇 핸들러에 넘길 가짜 context."""
    ctx = MagicMock()
    ctx.bot_data = {KEY_SESSIONS: {}, KEY_HUNT_TASKS: {}}
    ctx.user_data = {}
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    return ctx


def _update(chat_id, text=None, name='홍길동', username='hong'):
    up = MagicMock()
    up.effective_chat.id = chat_id
    up.effective_user.full_name = name
    up.effective_user.username = username
    up.message.text = text
    up.message.reply_text = AsyncMock()
    up.message.delete = AsyncMock()
    up.effective_message.reply_text = AsyncMock()
    return up


def _done_task():
    return MagicMock(done=lambda: False)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    # Session 은 asyncio.Lock 을 들고 있어 이벤트 루프 안에서만 생성할 수 있다
    # (py3.9 에서 Lock 이 생성 시점의 루프에 바인딩된다). 봇에서도 async 핸들러
    # 안에서만 만든다.

    def test_each_session_gets_its_own_lock(self):
        # 계정이 다르면 서로의 코레일 호출을 막지 않아야 한다.
        a, b = Session(MagicMock()), Session(MagicMock())
        self.assertIsNot(a.lock, b.lock)

    def test_repr_does_not_leak_credentials(self):
        korail = MagicMock()
        korail.membership_number = '12345678'
        korail.korail_pw = 'hunter2'
        self.assertNotIn('hunter2', repr(Session(korail)))


class AuthGateTests(unittest.IsolatedAsyncioTestCase):

    async def test_listed_chat_without_session_is_told_to_login(self):
        with patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '111'}):
            up = _update(111)
            await cmd_reservations(up, _ctx())
        self.assertIn('/login', up.effective_message.reply_text.await_args[0][0])

    async def test_admin_is_always_allowed(self):
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': '111'}):
            up = _update(111)
            await cmd_reservations(up, _ctx())
        # 승인 요청이 아니라 로그인 안내로 흘러야 한다.
        self.assertIn('/login', up.effective_message.reply_text.await_args[0][0])

    async def test_approved_user_is_allowed_without_env_change(self):
        ctx = _ctx()
        ctx.bot_data[KEY_USERS] = {'approved': {999: {}}, 'denied': {}}
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': '111'}):
            up = _update(999)
            await cmd_reservations(up, ctx)
        self.assertIn('/login', up.effective_message.reply_text.await_args[0][0])


class HelpAndCommandMenuTests(unittest.IsolatedAsyncioTestCase):

    def test_help_lists_start(self):
        self.assertIn('/start', _help_for(999))

    def test_admin_sees_users_command(self):
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': '111'}):
            self.assertIn('/users', _help_for(111))
            self.assertNotIn('/users', _help_for(999))

    def test_help_reflects_handoff_setting(self):
        with patch.dict(os.environ, {'SESSION_HANDOFF_KEY': 'k'}):
            self.assertIn('세션은 넘겨지지만', _help_for(999))
        with patch.dict(os.environ, {'SESSION_HANDOFF_KEY': ''}):
            self.assertIn('재시작되면 다시 /login', _help_for(999))

    async def test_commands_registered_with_admin_scope(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': '111'}):
            await register_commands(app)

        calls = app.bot.set_my_commands.await_args_list
        self.assertEqual(len(calls), 2)
        default = [c.command for c in calls[0].args[0]]
        admin = [c.command for c in calls[1].args[0]]
        self.assertIn('login', default)
        self.assertNotIn('users', default)  # 관리자 명령은 전체에 노출하지 않는다
        self.assertIn('users', admin)
        self.assertEqual(calls[1].kwargs['scope'].chat_id, 111)

    async def test_registration_failure_does_not_break_startup(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock(side_effect=TelegramError('boom'))
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': '111'}):
            await register_commands(app)  # 예외가 새어나오면 안 된다


class AccessApprovalTests(unittest.IsolatedAsyncioTestCase):
    ADMIN = 111
    NEWBIE = 999

    def setUp(self):
        self.env = patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': str(self.ADMIN)})
        self.env.start()
        self.addCleanup(self.env.stop)
        fd, self.path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.remove(self.path)
        patcher = patch.object(bot, 'USERS_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _ctx(self):
        ctx = _ctx()
        ctx.bot_data[KEY_USERS] = {'approved': {}, 'denied': {}}
        ctx.bot_data[KEY_PENDING] = {}
        return ctx

    def _query(self, chat_id, data):
        up = MagicMock()
        up.effective_chat.id = chat_id
        up.callback_query.data = data
        up.callback_query.answer = AsyncMock()
        up.callback_query.edit_message_text = AsyncMock()
        return up

    async def test_unknown_user_triggers_admin_request(self):
        ctx = self._ctx()
        up = _update(self.NEWBIE)
        up.effective_user.full_name = '김철수'
        up.effective_user.username = 'chulsoo'
        await cmd_reservations(up, ctx)

        self.assertIn(self.NEWBIE, ctx.bot_data[KEY_PENDING])
        sent = ctx.bot.send_message.await_args
        self.assertEqual(sent.args[0], self.ADMIN)
        self.assertIn('김철수', sent.args[1])
        self.assertIn(str(self.NEWBIE), sent.args[1])

    async def test_requester_name_is_html_escaped(self):
        # 이름은 사용자가 정하므로 HTML 로 새면 안 된다.
        ctx = self._ctx()
        up = _update(self.NEWBIE)
        up.effective_user.full_name = '<b>bold</b>'
        up.effective_user.username = None
        await cmd_reservations(up, ctx)
        body = ctx.bot.send_message.await_args.args[1]
        self.assertIn('&lt;b&gt;bold&lt;/b&gt;', body)

    async def test_repeat_request_does_not_spam_admin(self):
        ctx = self._ctx()
        await cmd_reservations(_update(self.NEWBIE), ctx)
        await cmd_reservations(_update(self.NEWBIE), ctx)
        await cmd_reservations(_update(self.NEWBIE), ctx)
        self.assertEqual(ctx.bot.send_message.await_count, 1)

    async def test_approval_persists_and_grants_access(self):
        ctx = self._ctx()
        await cmd_reservations(_update(self.NEWBIE), ctx)
        await cb_access(self._query(self.ADMIN, f'access:approve:{self.NEWBIE}'), ctx)

        self.assertIn(self.NEWBIE, users(ctx)['approved'])
        self.assertNotIn(self.NEWBIE, ctx.bot_data[KEY_PENDING])
        with open(self.path, encoding='utf-8') as f:
            self.assertIn(str(self.NEWBIE), json.load(f)['approved'])

        # 재시작(파일에서 재적재)해도 승인이 유지된다 — .env 수정도 재시작도 불필요.
        fresh = _ctx()
        load_users(fresh)
        self.assertTrue(is_allowed(fresh, self.NEWBIE))

    async def test_denial_blocks_without_notifying_admin_again(self):
        ctx = self._ctx()
        await cmd_reservations(_update(self.NEWBIE), ctx)
        await cb_access(self._query(self.ADMIN, f'access:deny:{self.NEWBIE}'), ctx)
        self.assertIn(self.NEWBIE, users(ctx)['denied'])
        self.assertFalse(is_allowed(ctx, self.NEWBIE))

        ctx.bot.send_message.reset_mock()
        up = _update(self.NEWBIE)
        await cmd_reservations(up, ctx)
        ctx.bot.send_message.assert_not_awaited()
        self.assertIn('거부', up.effective_message.reply_text.await_args[0][0])

    async def test_non_admin_cannot_approve_itself(self):
        ctx = self._ctx()
        await cmd_reservations(_update(self.NEWBIE), ctx)
        up = self._query(self.NEWBIE, f'access:approve:{self.NEWBIE}')
        await cb_access(up, ctx)
        self.assertNotIn(self.NEWBIE, users(ctx)['approved'])
        up.callback_query.answer.assert_awaited_once()
        self.assertIn('관리자', up.callback_query.answer.await_args[0][0])

    async def test_revoke_drops_session_and_hunts(self):
        ctx = self._ctx()
        users(ctx)['approved'][self.NEWBIE] = {'name': '김철수'}
        ctx.bot_data[KEY_SESSIONS][self.NEWBIE] = Session(MagicMock())
        task = _done_task()
        ctx.bot_data[KEY_HUNT_TASKS] = {self.NEWBIE: {'h1': {'task': task, 'label': 'x'}}}

        await cb_revoke(self._query(self.ADMIN, f'revoke:{self.NEWBIE}'), ctx)

        task.cancel.assert_called_once()
        self.assertIsNone(get_session(ctx, self.NEWBIE))
        self.assertFalse(is_allowed(ctx, self.NEWBIE))

    async def test_non_admin_cannot_revoke(self):
        ctx = self._ctx()
        users(ctx)['approved'][self.NEWBIE] = {}
        await cb_revoke(self._query(self.NEWBIE, f'revoke:{self.NEWBIE}'), ctx)
        self.assertIn(self.NEWBIE, users(ctx)['approved'])

    async def test_users_command_is_admin_only(self):
        ctx = self._ctx()
        users(ctx)['approved'][self.NEWBIE] = {}
        up = _update(self.NEWBIE)
        await cmd_users(up, ctx)
        self.assertIn('관리자 전용', up.effective_message.reply_text.await_args[0][0])

    async def test_bootstrap_message_when_no_admin_configured(self):
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': ''}):
            up = _update(self.NEWBIE)
            await cmd_reservations(up, self._ctx())
        body = up.effective_message.reply_text.await_args[0][0]
        self.assertIn('TELEGRAM_ADMIN_CHAT_IDS', body)
        self.assertIn(str(self.NEWBIE), body)


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '111'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.korail = MagicMock()
        self.korail.login.return_value = True
        self.korail.name = '홍길동'
        self.korail.membership_number = '12345678'

    async def _run_login(self, ctx, korail_id='12345678', pw='hunter2'):
        with patch.object(bot, 'PatchedKorail', return_value=self.korail) as ctor:
            self.assertEqual(await cmd_login(_update(111), ctx), LOGIN_ID)
            self.assertEqual(await login_id(_update(111, korail_id), ctx), LOGIN_PW)
            pw_update = _update(111, pw)
            end = await login_pw(pw_update, ctx)
        return ctor, pw_update, end

    async def test_successful_login_creates_session(self):
        ctx = _ctx()
        ctor, _, _ = await self._run_login(ctx)
        ctor.assert_called_once_with('12345678', 'hunter2', auto_login=False)
        self.assertIs(get_session(ctx, 111).korail, self.korail)

    async def test_password_message_is_deleted(self):
        _, pw_update, _ = await self._run_login(_ctx())
        pw_update.message.delete.assert_awaited_once()

    async def test_login_id_is_stripped_and_cleared_afterwards(self):
        ctx = _ctx()
        ctor, _, _ = await self._run_login(ctx, korail_id='  12345678  ')
        ctor.assert_called_once_with('12345678', 'hunter2', auto_login=False)
        self.assertNotIn(KEY_LOGIN_ID, ctx.user_data)

    async def test_failed_login_creates_no_session(self):
        self.korail.login.return_value = False
        ctx = _ctx()
        await self._run_login(ctx)
        self.assertIsNone(get_session(ctx, 111))

    async def test_login_exception_creates_no_session(self):
        self.korail.login.side_effect = OSError('network down')
        ctx = _ctx()
        await self._run_login(ctx)
        self.assertIsNone(get_session(ctx, 111))

    async def test_second_login_is_refused_while_session_exists(self):
        ctx = _ctx()
        await self._run_login(ctx)
        up = _update(111)
        await cmd_login(up, ctx)
        self.assertIn('/logout', up.message.reply_text.await_args[0][0])

    async def test_logout_drops_session_and_cancels_hunts(self):
        ctx = _ctx()
        await self._run_login(ctx)
        task = _done_task()
        ctx.bot_data[KEY_HUNT_TASKS] = {111: {'h1': {'task': task, 'label': '서울→부산'}}}
        await cmd_logout(_update(111), ctx)
        task.cancel.assert_called_once()
        self.assertIsNone(get_session(ctx, 111))


class RestartStateTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.remove(self.path)
        patcher = patch.object(bot, 'STATE_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_state_file_records_hunts_but_no_credentials(self):
        ctx = _ctx()
        korail = MagicMock(korail_id='12345678', korail_pw='hunter2')
        ctx.bot_data[KEY_SESSIONS] = {111: Session(korail)}
        ctx.bot_data[KEY_HUNT_TASKS] = {
            111: {'h1': {'task': _done_task(), 'label': '서울→부산 06/01 10:00'}},
        }
        save_state(ctx)
        with open(self.path, encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('hunter2', raw)
        self.assertNotIn('12345678', raw)
        self.assertEqual(json.loads(raw), {'111': ['서울→부산 06/01 10:00']})

    def test_logged_in_chat_without_hunts_is_still_recorded(self):
        ctx = _ctx()
        ctx.bot_data[KEY_SESSIONS] = {111: Session(MagicMock())}
        save_state(ctx)
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'111': []})

    def test_finished_hunts_are_not_recorded(self):
        ctx = _ctx()
        ctx.bot_data[KEY_HUNT_TASKS] = {
            111: {'h1': {'task': MagicMock(done=lambda: True), 'label': '끝난 헌팅'}},
        }
        save_state(ctx)
        self.assertFalse(os.path.exists(self.path))

    def test_empty_state_removes_stale_file(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{"111": []}')
        save_state(_ctx())
        self.assertFalse(os.path.exists(self.path))

    async def test_restart_notice_lists_lost_hunts_then_consumes_file(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'111': ['서울→부산 06/01 10:00'], '222': []}, f, ensure_ascii=False)
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        await notify_restart(app)

        sent = {c.args[0]: c.args[1] for c in app.bot.send_message.await_args_list}
        self.assertEqual(set(sent), {111, 222})
        self.assertIn('서울→부산 06/01 10:00', sent[111])
        self.assertIn('/login', sent[222])
        self.assertNotIn('서울→부산', sent[222])
        self.assertFalse(os.path.exists(self.path))

    async def test_restored_sessions_are_not_told_to_login(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'111': ['서울→부산'], '222': []}, f, ensure_ascii=False)
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        await notify_restart(app, restored=frozenset({111}))

        sent = {c.args[0]: c.args[1] for c in app.bot.send_message.await_args_list}
        self.assertIn('복원', sent[111])
        self.assertNotIn('/login', sent[111])
        self.assertIn('/login', sent[222])  # 복원 안 된 쪽은 그대로 재로그인 안내

    async def test_restart_notice_is_a_noop_without_state_file(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        await notify_restart(app)
        app.bot.send_message.assert_not_awaited()

    def _write_state(self, snapshot):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False)

    def _sent(self, app):
        return {c.args[0]: c.args[1] for c in app.bot.send_message.await_args_list}

    async def test_resumed_hunt_user_is_told_it_continues_not_to_relogin(self):
        # 헌팅까지 이어졌으면 다시 걸라는 안내는 틀린 말이 된다.
        self._write_state({'111': ['서울→부산 06/01 10:00']})
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': ''}):
            await notify_restart(
                app,
                restored=frozenset({111}),
                resumed={111: ['서울→부산 06/01 10:00']},
            )

        body = self._sent(app)[111]
        self.assertIn('이어서 실행 중', body)
        self.assertIn('서울→부산 06/01 10:00', body)
        self.assertNotIn('/login', body)
        self.assertNotIn('/reserve', body)

    async def test_fully_restored_user_without_hunts_is_not_bothered(self):
        # 세션만 복원되고 할 일이 없으면 재배포는 사용자에게 비사건이다.
        self._write_state({'111': [], '222': []})
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': ''}):
            await notify_restart(app, restored=frozenset({111, 222}))
        app.bot.send_message.assert_not_awaited()

    async def test_hunt_that_could_not_resume_is_reported_to_its_owner(self):
        # 세션은 살았지만 헌팅이 못 이어진 경우엔 다시 걸라고 알려야 한다.
        self._write_state({'111': ['이어진 헌팅', '못 이어진 헌팅']})
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': ''}):
            await notify_restart(
                app, restored=frozenset({111}), resumed={111: ['이어진 헌팅']},
            )

        body = self._sent(app)[111]
        self.assertIn('이어가지 못한 헌팅', body)
        self.assertIn('못 이어진 헌팅', body)
        self.assertIn('/reserve', body)
        self.assertNotIn('/login', body)

    async def test_admin_gets_restart_summary(self):
        self._write_state({'111': ['서울→부산'], '222': []})
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': '999'}):
            await notify_restart(
                app, restored=frozenset({111}), resumed={111: ['서울→부산']},
            )

        summary = self._sent(app)[999]
        self.assertIn('재시작', summary)
        self.assertIn('세션 1개', summary)
        self.assertIn('헌팅 1개', summary)
        self.assertIn('1명 재로그인 필요', summary)  # 222 는 복원되지 않았다

    async def test_admin_summary_is_skipped_when_nothing_happened(self):
        # 최초 기동처럼 직전 흔적이 없으면 보고할 것도 없다.
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with patch.dict(os.environ, {'TELEGRAM_ADMIN_CHAT_IDS': '999'}):
            await notify_restart(app)
        app.bot.send_message.assert_not_awaited()

    async def test_shutdown_snapshot_survives_hunt_cleanup(self):
        # 정상 종료 시 헌팅 task 의 finally 가 스냅샷을 지우면 안 된다.
        ctx = _ctx()
        ctx.bot_data[KEY_SESSIONS] = {111: Session(MagicMock())}
        ctx.bot_data[KEY_HUNT_TASKS] = {
            111: {'h1': {'task': _done_task(), 'label': '서울→부산 06/01 10:00'}},
        }
        await snapshot_on_stop(ctx)
        ctx.bot_data[KEY_HUNT_TASKS][111].clear()  # 뒤늦게 도는 finally 흉내
        ctx.bot_data[KEY_SESSIONS].clear()
        save_state(ctx)
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'111': ['서울→부산 06/01 10:00']})


class ConfigCheckTests(unittest.IsolatedAsyncioTestCase):
    # .env 는 레포에 없어 파이프라인이 챙기지 못한다. 기능이 에러 없이 꺼진 채
    # 배포되던 문제를 기동 시 잡는다.

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for patcher in (
            patch.object(bot, 'STATE_FILE', os.path.join(self.dir, '.bot_state.json')),
            patch.object(bot, 'USERS_FILE', os.path.join(self.dir, '.bot_users.json')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _env(self, **overrides):
        env = os.environ.copy()
        for k in ('TELEGRAM_ADMIN_CHAT_IDS', 'SESSION_HANDOFF_KEY') + bot.PATH_ENV:
            env.pop(k, None)
        env.update(overrides)
        return patch.dict(os.environ, env, clear=True)

    def _healthy(self):
        return self._env(TELEGRAM_ADMIN_CHAT_IDS='111', SESSION_HANDOFF_KEY='k')

    def test_healthy_config_reports_nothing(self):
        with self._healthy():
            self.assertEqual(check_config(), [])

    def test_missing_admin_is_reported(self):
        with self._env(SESSION_HANDOFF_KEY='k'):
            self.assertTrue(any('TELEGRAM_ADMIN_CHAT_IDS' in p for p in check_config()))

    def test_missing_handoff_key_is_reported(self):
        with self._env(TELEGRAM_ADMIN_CHAT_IDS='111'):
            self.assertTrue(any('SESSION_HANDOFF_KEY' in p for p in check_config()))

    def test_empty_path_override_is_reported(self):
        # Docker 에서 컨테이너의 /app/data 설정을 덮어써 데이터가 사라지는 함정.
        with self._env(TELEGRAM_ADMIN_CHAT_IDS='111', SESSION_HANDOFF_KEY='k',
                       BOT_USERS_FILE=''):
            self.assertTrue(any('BOT_USERS_FILE' in p for p in check_config()))

    def test_set_path_override_is_fine(self):
        with self._env(TELEGRAM_ADMIN_CHAT_IDS='111', SESSION_HANDOFF_KEY='k',
                       BOT_USERS_FILE='/app/data/.bot_users.json'):
            self.assertFalse(any('BOT_USERS_FILE' in p for p in check_config()))

    def test_unwritable_state_dir_is_reported(self):
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        with self._healthy():
            self.assertTrue(any('쓸 수 없다' in p for p in check_config()))

    async def test_problems_are_sent_to_admin(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with self._env(TELEGRAM_ADMIN_CHAT_IDS='111'):
            await report_config(app)
        body = app.bot.send_message.await_args.args[1]
        self.assertIn('SESSION_HANDOFF_KEY', body)

    async def test_nothing_sent_when_config_is_healthy(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock()
        with self._healthy():
            await report_config(app)
        app.bot.send_message.assert_not_awaited()

    async def test_send_failure_does_not_break_startup(self):
        app = MagicMock()
        app.bot.send_message = AsyncMock(side_effect=TelegramError('boom'))
        with self._env(TELEGRAM_ADMIN_CHAT_IDS='111'):
            await report_config(app)  # 예외가 새어나오면 안 된다


class SessionHandoffTests(unittest.IsolatedAsyncioTestCase):
    CHAT = 111
    KEY = 'test-handoff-key'

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.enc')
        os.close(fd)
        os.remove(self.path)
        for patcher in (
            patch.object(bot, 'HANDOFF_FILE', self.path),
            patch.object(bot, 'HANDOFF_TTL', 300),
            patch.dict(os.environ, {'SESSION_HANDOFF_KEY': self.KEY}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _app_with_session(self):
        app = _ctx()
        korail = MagicMock()
        korail.korail_id, korail.korail_pw = '12345678', 'hunter2'
        korail._key, korail._idx = 'KEY123', 'IDX9'
        korail.membership_number, korail.name, korail.email = '12345678', '홍길동', 'a@b.c'
        korail.logined = True
        korail._session.cookies = [
            MagicMock(name='c', value='v', domain='.letskorail.com', path='/'),
        ]
        # MagicMock 의 name= 은 예약 인자라 따로 설정한다.
        korail._session.cookies[0].name = 'JSESSIONID'
        korail._session.cookies[0].value = 'abc123'
        app.bot_data[KEY_SESSIONS] = {self.CHAT: Session(korail)}
        return app

    def test_handoff_file_is_encrypted_and_not_readable_as_plaintext(self):
        self.assertTrue(dump_sessions(self._app_with_session()))
        with open(self.path, 'rb') as f:
            blob = f.read()
        self.assertNotIn(b'hunter2', blob)
        self.assertNotIn(b'12345678', blob)
        self.assertNotIn(b'abc123', blob)

    def test_handoff_file_is_owner_only(self):
        dump_sessions(self._app_with_session())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_round_trip_restores_session_and_deletes_file(self):
        dump_sessions(self._app_with_session())
        fresh = _ctx()
        with patch.object(bot, 'PatchedKorail') as ctor:
            restored = restore_sessions(fresh)
        self.assertEqual(restored, frozenset({self.CHAT}))
        ctor.assert_called_once_with('12345678', 'hunter2', auto_login=False)
        self.assertIsNotNone(get_session(fresh, self.CHAT))
        self.assertFalse(os.path.exists(self.path))  # 읽자마자 삭제

    def test_cookies_are_carried_over(self):
        dump_sessions(self._app_with_session())
        fresh = _ctx()
        with patch.object(bot, 'PatchedKorail') as ctor:
            restore_sessions(fresh)
        ctor.return_value._session.cookies.set.assert_called_once_with(
            'JSESSIONID', 'abc123', domain='.letskorail.com', path='/',
        )

    def test_expired_handoff_is_rejected(self):
        dump_sessions(self._app_with_session())
        fresh = _ctx()
        with patch.object(bot, 'HANDOFF_TTL', -1):
            restored = restore_sessions(fresh)
        self.assertEqual(restored, frozenset())
        self.assertIsNone(get_session(fresh, self.CHAT))
        self.assertFalse(os.path.exists(self.path))

    def test_wrong_key_is_rejected(self):
        dump_sessions(self._app_with_session())
        fresh = _ctx()
        with patch.dict(os.environ, {'SESSION_HANDOFF_KEY': 'a-different-key'}):
            restored = restore_sessions(fresh)
        self.assertEqual(restored, frozenset())
        self.assertFalse(os.path.exists(self.path))

    def test_tampered_file_is_rejected(self):
        dump_sessions(self._app_with_session())
        with open(self.path, 'r+b') as f:
            blob = bytearray(f.read())
            blob[-1] ^= 0xFF  # 인증 태그가 잡아내야 한다
            f.seek(0)
            f.write(blob)
        self.assertEqual(restore_sessions(_ctx()), frozenset())

    def test_disabled_without_key_and_nothing_is_written(self):
        with patch.dict(os.environ, {'SESSION_HANDOFF_KEY': ''}):
            self.assertFalse(dump_sessions(self._app_with_session()))
        self.assertFalse(os.path.exists(self.path))

    def test_stale_file_is_discarded_when_key_removed(self):
        dump_sessions(self._app_with_session())
        with patch.dict(os.environ, {'SESSION_HANDOFF_KEY': ''}):
            self.assertEqual(restore_sessions(_ctx()), frozenset())
        self.assertFalse(os.path.exists(self.path))

    def test_no_file_written_when_no_sessions(self):
        self.assertFalse(dump_sessions(_ctx()))
        self.assertFalse(os.path.exists(self.path))

    def test_missing_file_restores_nothing(self):
        self.assertEqual(restore_sessions(_ctx()), frozenset())


def _fake_train(train_no='001', dep_date='20260601', dep_time='100000', seat=True):
    tr = MagicMock()
    tr.train_no = train_no
    tr.dep_date = dep_date
    tr.dep_time = dep_time
    tr.train_type_name = 'KTX'
    tr.has_seat.return_value = seat
    return tr


def _callback_update(chat_id=111, data=''):
    up = _update(chat_id)
    up.callback_query.data = data
    up.callback_query.answer = AsyncMock()
    up.callback_query.edit_message_text = AsyncMock()
    return up


class PassengerConversionTests(unittest.TestCase):
    # 인원수 → Passenger 객체. 라이브러리가 다인 예약을 지원하므로 한 번의
    # 예약으로 여러 명을 같은 PNR 에 묶는다.

    def test_single_adult(self):
        psgrs = build_passengers(1)
        self.assertEqual(len(psgrs), 1)
        self.assertIsInstance(psgrs[0], AdultPassenger)
        self.assertEqual(psgrs[0].count, 1)

    def test_multiple_adults_are_one_grouped_passenger(self):
        psgrs = build_passengers(4)
        self.assertEqual([type(p) for p in psgrs], [AdultPassenger])
        self.assertEqual(psgrs[0].count, 4)

    def test_zero_is_raised_to_minimum(self):
        self.assertEqual(build_passengers(0)[0].count, PASSENGER_MIN)

    def test_over_max_is_capped(self):
        self.assertEqual(build_passengers(PASSENGER_MAX + 5)[0].count, PASSENGER_MAX)

    def test_negative_is_raised_to_minimum(self):
        self.assertEqual(build_passengers(-3)[0].count, PASSENGER_MIN)


class ClampPassengerCountTests(unittest.TestCase):
    # 오래된 인라인 키보드를 다시 눌러 범위 밖 값이 들어와도 대화가 끊기면 안 된다.

    def test_within_range_passthrough(self):
        self.assertEqual(clamp_passenger_count(5), 5)

    def test_boundaries_are_kept(self):
        self.assertEqual(clamp_passenger_count(PASSENGER_MIN), PASSENGER_MIN)
        self.assertEqual(clamp_passenger_count(PASSENGER_MAX), PASSENGER_MAX)

    def test_below_min(self):
        self.assertEqual(clamp_passenger_count(0), PASSENGER_MIN)

    def test_above_max(self):
        self.assertEqual(clamp_passenger_count(99), PASSENGER_MAX)

    def test_numeric_string_from_callback_data(self):
        self.assertEqual(clamp_passenger_count('3'), 3)

    def test_garbage_falls_back_to_min(self):
        self.assertEqual(clamp_passenger_count('셋'), PASSENGER_MIN)
        self.assertEqual(clamp_passenger_count(None), PASSENGER_MIN)


class DescribePassengersTests(unittest.TestCase):

    def test_adults_only(self):
        self.assertEqual(describe_passengers([AdultPassenger(2)]), '어른 2명')

    def test_mixed_types_are_listed(self):
        # UI 는 어른만 노출하지만, 확장 시 호출부를 고치지 않아도 되게 해둔다.
        text = describe_passengers([AdultPassenger(2), ChildPassenger(1), SeniorPassenger(1)])
        self.assertIn('어른 2명', text)
        self.assertIn('어린이 1명', text)
        self.assertIn('경로 1명', text)

    def test_zero_counts_are_omitted(self):
        self.assertEqual(describe_passengers([AdultPassenger(2), ChildPassenger(0)]), '어른 2명')

    def test_empty_list_falls_back(self):
        self.assertEqual(describe_passengers([]), f"어른 {PASSENGER_MIN}명")


class PassengersOfTests(unittest.TestCase):

    def test_reads_user_data(self):
        ctx = _ctx()
        ctx.user_data[KEY_ADULTS] = 3
        self.assertEqual(passengers_of(ctx)[0].count, 3)

    def test_missing_key_defaults_to_one(self):
        # 인원 단계를 거치지 않은 흐름도 예약이 깨지면 안 된다.
        self.assertEqual(passengers_of(_ctx())[0].count, PASSENGER_MIN)


class CountKeyboardTests(unittest.TestCase):

    def test_offers_every_allowed_count(self):
        data = [b.callback_data for row in _count_keyboard().inline_keyboard for b in row]
        self.assertIn(f"cnt:{PASSENGER_MIN}", data)
        self.assertIn(f"cnt:{PASSENGER_MAX}", data)
        self.assertNotIn(f"cnt:{PASSENGER_MAX + 1}", data)
        self.assertIn('cancel', data)


class ConvCountTests(unittest.IsolatedAsyncioTestCase):

    async def test_selection_is_stored_and_moves_to_date(self):
        ctx = _ctx()
        up = _callback_update(data='cnt:4')
        self.assertEqual(await conv_count(up, ctx), ASK_DATE)
        self.assertEqual(ctx.user_data[KEY_ADULTS], 4)
        self.assertIn('어른 4명', up.callback_query.edit_message_text.await_args[0][0])

    async def test_out_of_range_callback_is_clamped(self):
        ctx = _ctx()
        await conv_count(_callback_update(data='cnt:99'), ctx)
        self.assertEqual(ctx.user_data[KEY_ADULTS], PASSENGER_MAX)

    async def test_cancel_ends_conversation(self):
        ctx = _ctx()
        result = await conv_count(_callback_update(data='cancel'), ctx)
        self.assertNotEqual(result, ASK_DATE)
        self.assertNotIn(KEY_ADULTS, ctx.user_data)

    async def test_unknown_callback_stays_in_state(self):
        self.assertEqual(await conv_count(_callback_update(data='nope'), _ctx()), ASK_COUNT)


class HuntLabelTests(unittest.TestCase):
    # 여러 헌팅이 동시에 도는데 인원이 안 보이면 어느 게 몇 명짜리인지 모른다.

    def test_allday_label_shows_count(self):
        label = _format_hunt_label('서울', '부산', '20260601', '100000',
                                   passengers=build_passengers(3))
        self.assertIn('서울→부산', label)
        self.assertIn('어른 3명', label)

    def test_train_label_shows_count(self):
        label = _format_hunt_label('서울', '부산', '20260601', '100000',
                                   train=_fake_train(), passengers=build_passengers(2))
        self.assertIn('KTX 001', label)
        self.assertIn('어른 2명', label)

    def test_label_without_passengers_is_unchanged(self):
        self.assertNotIn('명', _format_hunt_label('서울', '부산', '20260601', '100000'))


class ReservationSuccessPassengerTests(unittest.TestCase):

    def test_passenger_line_is_shown(self):
        msg = format_reservation_success(_make_reservation(count=2), build_passengers(2))
        self.assertIn('어른 2명', msg)

    def test_omitted_when_not_given(self):
        self.assertNotIn('<b>인원</b>', format_reservation_success(_make_reservation()))


class PassengersReachKorailTests(unittest.IsolatedAsyncioTestCase):
    """검색·예약·헌팅이 모두 같은 passengers 를 쓰는지 본다. 1명으로 검색하고
    여러 명으로 예약하면 좌석이 있는 줄 알고 들어갔다가 매진으로 튕긴다."""

    def setUp(self):
        self.session = MagicMock()
        self.session.korail = MagicMock()
        for name in ('_session_or_end', '_ensure_login'):
            p = patch.object(bot, name, AsyncMock(return_value=self.session))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(bot, 'save_state', MagicMock())
        p.start()
        self.addCleanup(p.stop)

    def _ctx_with(self, adults):
        ctx = _ctx()
        ctx.user_data.update({
            KEY_ADULTS: adults, KEY_DATE: '20260601', KEY_TIME: '100000',
            KEY_DEP: '서울', KEY_ARR: '부산',
        })
        return ctx

    @staticmethod
    def _counts(call):
        """_korail_call 한 번에 실려간 passengers 의 인원수."""
        psgrs = call.kwargs.get('passengers')
        if psgrs is None:
            # reserve 는 위치 인자로 넘긴다: (session, fn, train, passengers, ...)
            psgrs = call.args[3]
        return sum(p.count for p in psgrs)

    async def test_search_uses_selected_count(self):
        ctx = self._ctx_with(3)
        call = AsyncMock(return_value=[_fake_train()])
        with patch.object(bot, '_korail_call', call):
            await _show_trains(_callback_update(), ctx)
        self.assertIs(call.await_args.args[1], self.session.korail.search_train)
        self.assertEqual(self._counts(call.await_args), 3)

    async def test_reserve_uses_same_count_as_search(self):
        ctx = self._ctx_with(2)
        train = _fake_train()
        ctx.user_data[KEY_TRAINS] = [train]
        ctx.user_data[KEY_SELECTED_TRAIN_IDX] = 0
        call = AsyncMock(return_value=_make_reservation(count=2))
        with patch.object(bot, '_korail_call', call):
            await conv_option(_callback_update(data='opt:GENERAL_FIRST'), ctx)
        self.assertIs(call.await_args.args[1], self.session.korail.reserve)
        self.assertEqual(self._counts(call.await_args), 2)

    async def test_reserve_success_message_shows_count(self):
        ctx = self._ctx_with(2)
        ctx.user_data[KEY_TRAINS] = [_fake_train()]
        ctx.user_data[KEY_SELECTED_TRAIN_IDX] = 0
        up = _callback_update(data='opt:GENERAL_FIRST')
        with patch.object(bot, '_korail_call', AsyncMock(return_value=_make_reservation(count=2))):
            await conv_option(up, ctx)
        self.assertIn('어른 2명', up.effective_message.reply_text.await_args[0][0])

    async def test_hunt_loop_searches_and_reserves_with_same_count(self):
        psgrs = build_passengers(4)
        ctx = self._ctx_with(4)
        call = AsyncMock(side_effect=[[_fake_train()], _make_reservation(count=4)])
        with patch.object(bot, '_korail_call', call):
            await _hunt_loop(ctx, self.session, 111, 'h1', 'label',
                             '서울', '부산', '20260601', '100000', psgrs, 0)

        search, reserve = call.await_args_list
        self.assertIs(search.args[1], self.session.korail.search_train_allday)
        self.assertIs(reserve.args[1], self.session.korail.reserve)
        self.assertEqual(self._counts(search), 4)
        self.assertEqual(self._counts(reserve), 4)
        self.assertIn('어른 4명', ctx.bot.send_message.await_args[0][1])

    async def test_train_hunt_loop_searches_and_reserves_with_same_count(self):
        psgrs = build_passengers(5)
        ctx = self._ctx_with(5)
        train = _fake_train(train_no='007')
        target = (train.train_no, train.dep_date, train.dep_time)
        call = AsyncMock(side_effect=[[train], _make_reservation(count=5)])
        with patch.object(bot, '_korail_call', call):
            await _train_hunt_loop(ctx, self.session, 111, 'h1', 'label', target,
                                   '서울', '부산', '20260601', '100000', psgrs,
                                   ReserveOption.GENERAL_FIRST, 0)

        search, reserve = call.await_args_list
        self.assertIs(search.args[1], self.session.korail.search_train)
        self.assertIs(reserve.args[1], self.session.korail.reserve)
        self.assertEqual(self._counts(search), 5)
        self.assertEqual(self._counts(reserve), 5)
        self.assertIn('어른 5명', ctx.bot.send_message.await_args[0][1])
class HuntPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """재시작 때 헌팅이 조건 그대로 되살아나는지 검증한다.

    실제 코레일 루프 대신 인자만 기록하는 스텁을 끼워, 어느 종류의 헌팅이 어떤
    조건으로 다시 떴는지만 본다.
    """
    CHAT = 111
    OTHER = 222
    KEY = 'test-handoff-key'

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.enc')
        os.close(fd)
        os.remove(self.path)
        self.dir = tempfile.mkdtemp()
        for patcher in (
            patch.object(bot, 'HANDOFF_FILE', self.path),
            patch.object(bot, 'HANDOFF_TTL', 300),
            patch.object(bot, 'STATE_FILE', os.path.join(self.dir, '.bot_state.json')),
            patch.dict(os.environ, {'SESSION_HANDOFF_KEY': self.KEY}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

        self.started = []
        for name in ('_hunt_loop', '_train_hunt_loop'):
            patcher = patch.object(bot, name, self._stub(name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _stub(self, name):
        async def _noop():
            return None

        def loop(*args):
            self.started.append((name, args))
            return _noop()
        return loop

    def _all_spec(self, label='전체 서울→부산 06/01 10:00'):
        # 인원을 1명이 아닌 값으로 둬야 인원이 실제로 실려 가는지 검증된다.
        return _hunt_spec(label, '서울', '부산', '20260601', '100000', 3.0, adults=3)

    def _train_spec(self, label='[KTX 101] 서울→부산 06/01 10:00', option=None):
        return _hunt_spec(
            label, '서울', '부산', '20260601', '100000', 2.5,
            target=('101', '20260601', '100000'),
            option=option or bot.ReserveOption.SPECIAL_ONLY,
            adults=3,
        )

    def _live_app(self, *hunts, **kwargs):
        """세션 1개와 주어진 헌팅이 돌고 있는 종료 직전 상태."""
        app = _ctx()
        korail = MagicMock()
        korail.korail_id, korail.korail_pw = '12345678', 'hunter2'
        korail._key, korail._idx = 'KEY123', 'IDX9'
        korail.membership_number, korail.name, korail.email = '12345678', '홍길동', 'a@b.c'
        korail.logined = True
        korail._session.cookies = []
        app.bot_data[KEY_SESSIONS] = {self.CHAT: Session(korail)}
        app.bot_data[KEY_HUNT_TASKS] = {
            self.CHAT: {
                hid: {'task': _done_task(), 'label': spec['label'], 'spec': spec}
                for hid, spec in hunts
            },
        }
        return app

    async def _restart(self, app):
        """정상 종료 → 재기동을 흉내내고 (새 app, restored, resumed) 를 돌려준다."""
        dump_sessions(app)
        fresh = _ctx()
        with patch.object(bot, 'PatchedKorail'):
            restored = restore_sessions(fresh)
            resumed = await restore_hunts(fresh)
        await asyncio.sleep(0)  # 스텁 task 를 마저 돌린다
        return fresh, restored, resumed

    async def test_all_train_hunt_round_trip(self):
        _, _, resumed = await self._restart(self._live_app(('h1', self._all_spec())))

        self.assertEqual(resumed, {self.CHAT: ['전체 서울→부산 06/01 10:00']})
        name, args = self.started[0]
        self.assertEqual(name, '_hunt_loop')
        # (context, session, chat_id, hunt_id, label, dep, arr, d, t, passengers, interval)
        self.assertEqual(
            args[2:9],
            (self.CHAT, 'h1', '전체 서울→부산 06/01 10:00',
             '서울', '부산', '20260601', '100000'),
        )
        # 인원이 살아 넘어와야 한다. 빠지면 4명짜리 헌팅이 조용히 1명이 된다.
        self.assertEqual(describe_passengers(args[9]), '어른 3명')
        self.assertEqual(args[10], 3.0)

    async def test_train_hunt_round_trip_keeps_target_tuple_and_option(self):
        _, _, resumed = await self._restart(self._live_app(('h1', self._train_spec())))

        self.assertEqual(resumed, {self.CHAT: ['[KTX 101] 서울→부산 06/01 10:00']})
        name, args = self.started[0]
        self.assertEqual(name, '_train_hunt_loop')
        # (context, session, chat_id, hunt_id, label, target, dep, arr, d, t,
        #  passengers, option, interval)
        # target 은 JSON 을 거치며 리스트가 되므로 튜플로 되돌아와야 열차 대조가 맞는다.
        self.assertEqual(args[5], ('101', '20260601', '100000'))
        self.assertEqual(describe_passengers(args[10]), '어른 3명')
        self.assertEqual(args[11], 'SPECIAL_ONLY')
        self.assertEqual(args[12], 2.5)

    async def test_hunt_ids_are_preserved_so_hunt_stop_still_works(self):
        app = self._live_app(('h2', self._all_spec('A')), ('h5', self._train_spec('B')))
        fresh, _, _ = await self._restart(app)

        self.assertEqual(sorted(_chat_hunts(fresh, self.CHAT)), ['h2', 'h5'])
        self.assertEqual([args[3] for _, args in self.started], ['h2', 'h5'])

    async def test_hunt_of_chat_without_session_is_not_resumed(self):
        # 세션 없이는 코레일 호출이 불가능하다.
        app = self._live_app(('h1', self._all_spec()))
        app.bot_data[KEY_HUNT_TASKS][self.OTHER] = {
            'h1': {'task': _done_task(), 'label': 'C', 'spec': self._all_spec('C')},
        }
        _, _, resumed = await self._restart(app)

        self.assertEqual(set(resumed), {self.CHAT})
        self.assertEqual(len(self.started), 1)

    async def test_one_broken_hunt_does_not_take_down_the_rest(self):
        broken = self._train_spec('깨진 헌팅')
        broken['option'] = 'NOT_A_REAL_OPTION'
        app = self._live_app(('h1', broken), ('h2', self._all_spec('멀쩡한 헌팅')))
        _, _, resumed = await self._restart(app)

        self.assertEqual(resumed, {self.CHAT: ['멀쩡한 헌팅']})
        self.assertEqual([args[3] for _, args in self.started], ['h2'])

    async def test_passenger_count_survives_the_handoff(self):
        # 인원 기능이 붙기 전이라 루프로 넘기지는 않지만, 조건은 실려 와야 한다.
        spec = self._all_spec()
        spec['adults'] = 4
        fresh, _, _ = await self._restart(self._live_app(('h1', spec)))
        self.assertEqual(_chat_hunts(fresh, self.CHAT)['h1']['spec']['adults'], 4)

    async def test_finished_hunt_is_not_carried_over(self):
        app = self._live_app(('h1', self._all_spec()))
        app.bot_data[KEY_HUNT_TASKS][self.CHAT]['h1']['task'] = MagicMock(done=lambda: True)
        _, _, resumed = await self._restart(app)
        self.assertEqual(resumed, {})

    async def test_hunt_conditions_are_not_stored_in_plaintext(self):
        # 조건도 자격증명과 같은 암호화 페이로드 안에만 존재해야 한다.
        dump_sessions(self._live_app(('h1', self._all_spec())))
        with open(self.path, 'rb') as f:
            blob = f.read()
        self.assertNotIn('서울'.encode('utf-8'), blob)
        self.assertNotIn(b'20260601', blob)

    async def test_expired_handoff_resumes_nothing(self):
        dump_sessions(self._live_app(('h1', self._all_spec())))
        fresh = _ctx()
        with patch.object(bot, 'HANDOFF_TTL', -1), patch.object(bot, 'PatchedKorail'):
            restore_sessions(fresh)
            resumed = await restore_hunts(fresh)
        self.assertEqual(resumed, {})
        self.assertEqual(self.started, [])

    async def test_disabled_handoff_resumes_nothing(self):
        with patch.dict(os.environ, {'SESSION_HANDOFF_KEY': ''}):
            dump_sessions(self._live_app(('h1', self._all_spec())))
            fresh = _ctx()
            restore_sessions(fresh)
            resumed = await restore_hunts(fresh)
        self.assertEqual(resumed, {})
        self.assertEqual(self.started, [])

    async def test_hunt_started_before_this_feature_is_skipped(self):
        # spec 없이 기록된 헌팅은 조건을 알 수 없어 되살릴 수 없다.
        app = self._live_app()
        app.bot_data[KEY_HUNT_TASKS][self.CHAT]['h1'] = {
            'task': _done_task(), 'label': '구버전 헌팅',
        }
        _, _, resumed = await self._restart(app)
        self.assertEqual(resumed, {})


class HuntSpecTests(unittest.TestCase):
    """직렬화 포맷 자체의 방어선. 이상한 값이 코레일 호출까지 흘러가면 안 된다."""

    def _raw(self, **overrides):
        spec = _hunt_spec(
            'L', '서울', '부산', '20260601', '100000', 3.0,
            target=('101', '20260601', '100000'),
            option=bot.ReserveOption.GENERAL_ONLY,
        )
        spec['hunt_id'] = 'h1'
        spec.update(overrides)
        return spec

    def test_round_trip_through_json(self):
        hunt_id, spec = _load_hunt_spec(json.loads(json.dumps(self._raw())))
        self.assertEqual(hunt_id, 'h1')
        self.assertEqual(spec['kind'], bot.HUNT_TRAIN)
        self.assertEqual(spec['target'], ('101', '20260601', '100000'))
        self.assertEqual(spec['option'], 'GENERAL_ONLY')
        self.assertEqual(spec['interval'], 3.0)

    def test_all_hunt_has_no_target_or_option(self):
        raw = _hunt_spec('L', '서울', '부산', '20260601', '100000', 3.0)
        raw['hunt_id'] = 'h1'
        _, spec = _load_hunt_spec(raw)
        self.assertEqual(spec['kind'], bot.HUNT_ALL)
        self.assertNotIn('target', spec)
        self.assertNotIn('option', spec)

    def test_every_reserve_option_survives(self):
        # ReserveOption 은 enum 이 아니라 문자열 상수라 값이 곧 이름이다.
        for name in ('GENERAL_FIRST', 'GENERAL_ONLY', 'SPECIAL_FIRST', 'SPECIAL_ONLY'):
            _, spec = _load_hunt_spec(self._raw(option=getattr(bot.ReserveOption, name)))
            self.assertEqual(spec['option'], name)

    def test_passenger_count_round_trips(self):
        # 인원을 잃으면 4명짜리 헌팅이 재시작 뒤 조용히 1명이 된다.
        raw = self._raw()
        raw['adults'] = 4
        _, spec = _load_hunt_spec(json.loads(json.dumps(raw)))
        self.assertEqual(spec['adults'], 4)

    def test_missing_passenger_count_falls_back_to_one(self):
        # 인원 필드가 없던 시절에 기록된 레코드도 받아야 한다.
        raw = self._raw()
        del raw['adults']
        _, spec = _load_hunt_spec(raw)
        self.assertEqual(spec['adults'], 1)

    def test_broken_passenger_count_falls_back_to_one(self):
        _, spec = _load_hunt_spec(self._raw(adults='없음'))
        self.assertEqual(spec['adults'], 1)

    def test_passenger_count_is_clamped_to_bookable_range(self):
        self.assertEqual(_load_hunt_spec(self._raw(adults=999))[1]['adults'], 9)
        self.assertEqual(_load_hunt_spec(self._raw(adults=0))[1]['adults'], 1)

    def test_unknown_option_is_rejected(self):
        with self.assertRaises(ValueError):
            _load_hunt_spec(self._raw(option='DROP_TABLE'))

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            _load_hunt_spec(self._raw(kind='weird'))

    def test_malformed_target_is_rejected(self):
        with self.assertRaises(ValueError):
            _load_hunt_spec(self._raw(target=['101']))

    def test_missing_field_is_rejected(self):
        raw = self._raw()
        del raw['dep']
        with self.assertRaises(KeyError):
            _load_hunt_spec(raw)


if __name__ == '__main__':
    unittest.main()
