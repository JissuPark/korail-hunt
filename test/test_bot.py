"""
bot.py 단위 테스트 — 네트워크/봇 연결 없이 파서와 포매터를 검증한다.

Conversation handler 자체는 python-telegram-bot 의 통합 환경이 필요해 여기서
다루지 않는다. 직접 봇을 띄워 수동 검증하라.
"""
import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TelegramError

import bot
from bot import (
    KEY_HUNT_TASKS,
    KEY_LOGIN_ID,
    KEY_PENDING,
    KEY_SESSIONS,
    KEY_USERS,
    LOGIN_ID,
    LOGIN_PW,
    Session,
    _help_for,
    allowed_chat_ids,
    authorized_chat_id,
    cb_access,
    cb_revoke,
    cmd_login,
    cmd_logout,
    cmd_reservations,
    cmd_users,
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
    register_commands,
    restore_sessions,
    save_state,
    snapshot_on_stop,
    users,
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


if __name__ == '__main__':
    unittest.main()
