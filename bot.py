"""
korail-hunt Telegram 봇.

흐름:
  /reserve  → 출발일 → 출발시각 → 출발역 → 도착역 → 열차 선택 → 좌석옵션 → 예약
  좌석이 없으면 [헌팅 시작] 버튼이 노출되고, polling 으로 자동 예약을 시도한다.
  /hunt_stop      → 진행 중인 헌팅 중단
  /reservations   → 현재 예약 목록
  /cancel         → 대화 취소
  /help           → 도움말

실행:
  pip install -e ".[bot]"
  .env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_AUTHORIZED_CHAT_ID, KORAIL_ID, KORAIL_PW 설정
  python bot.py
"""
import asyncio
import logging
import os
from datetime import date, timedelta
from functools import wraps

# 시스템 인증서 저장소(Windows/macOS) 사용. 사내 프록시·백신이 SSL 검사를
# 하는 환경에서 certifi 번들로는 실패하므로 OS 저장소로 우회한다.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from korail2 import (
    Korail,
    KorailError,
    NoResultsError,
    PatchedKorail,
    ReserveOption,
    SoldOutError,
)

logger = logging.getLogger(__name__)

# ConversationHandler states
ASK_DATE, ASK_TIME, ASK_DEP, ASK_ARR, SELECT_TRAIN, SELECT_OPTION = range(6)

# context.user_data 키
KEY_DATE = 'date'
KEY_TIME = 'time'
KEY_DEP = 'dep'
KEY_ARR = 'arr'
KEY_TRAINS = 'trains'
KEY_SELECTED_TRAIN_IDX = 'sel'

# context.bot_data 키
KEY_KORAIL = 'korail'
KEY_KORAIL_LOCK = 'korail_lock'  # 모든 코레일 호출 직렬화용 asyncio.Lock
KEY_HUNT_TASKS = 'hunt_tasks'  # chat_id -> asyncio.Task


# ---------------------------------------------------------------------------
# 파서
# ---------------------------------------------------------------------------

def parse_date(text, today=None):
    """YYYYMMDD / '오늘' / '내일' / '모레' / '+N' 형식을 YYYYMMDD 로 변환."""
    text = text.strip()
    if today is None:
        today = date.today()
    aliases = {'오늘': 0, '내일': 1, '모레': 2}
    if text in aliases:
        return (today + timedelta(days=aliases[text])).strftime('%Y%m%d')
    if text.startswith('+'):
        try:
            n = int(text[1:])
        except ValueError:
            raise ValueError(f"'+N' 형식이 잘못됨: {text!r}")
        return (today + timedelta(days=n)).strftime('%Y%m%d')
    if len(text) == 8 and text.isdigit():
        return text
    raise ValueError(f"날짜 형식을 인식할 수 없음: {text!r}. YYYYMMDD / 오늘 / 내일 / +N 사용")


def parse_time(text):
    """HHMM / HHMMSS / HH:MM / HH:MM:SS 형식을 HHMMSS 로 변환."""
    text = text.strip().replace(':', '')
    if len(text) == 4 and text.isdigit():
        return text + '00'
    if len(text) == 6 and text.isdigit():
        return text
    raise ValueError(f"시각 형식을 인식할 수 없음: {text!r}. HHMM / HHMMSS / HH:MM 사용")


def format_reservation_success(rsv):
    """예약 성공 메시지 (HTML)."""
    buy_dt = f"{rsv.buy_limit_date[:4]}-{rsv.buy_limit_date[4:6]}-{rsv.buy_limit_date[6:]}"
    buy_tm = f"{rsv.buy_limit_time[:2]}:{rsv.buy_limit_time[2:4]}"
    return (
        f"✅ <b>예약 성공</b>\n\n"
        f"{rsv!r}\n\n"
        f"<b>예약번호</b>: <code>{rsv.rsv_id}</code>\n"
        f"<b>구매기한</b>: {buy_dt} {buy_tm}\n"
        f"<b>금액</b>: {rsv.price:,}원 ({rsv.seat_no_count}석)\n\n"
        f"코레일톡 앱 → 승차권 → 결제대기에서 결제하라.\n"
        f"기한 초과 시 자동 취소된다."
    )


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------

def authorized_chat_id():
    v = os.environ.get('TELEGRAM_AUTHORIZED_CHAT_ID')
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        logger.warning("TELEGRAM_AUTHORIZED_CHAT_ID 가 정수가 아니다: %r", v)
        return None


def restricted(func):
    """인증된 chat_id 에서 온 업데이트만 처리한다."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        allowed = authorized_chat_id()
        chat_id = update.effective_chat.id if update.effective_chat else None
        if allowed is None:
            # 미설정 시 첫 사용자에게 본인 chat_id 를 알려준다 (셋업 도움)
            await update.effective_message.reply_text(
                "TELEGRAM_AUTHORIZED_CHAT_ID 가 설정되어 있지 않다.\n"
                f"이 chat_id 를 .env 에 적은 뒤 봇을 재시작하라: <code>{chat_id}</code>",
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END
        if chat_id != allowed:
            logger.warning("인증 실패: %s (허용: %s)", chat_id, allowed)
            await update.effective_message.reply_text("권한 없음")
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 공용
# ---------------------------------------------------------------------------

async def _korail_call(context: ContextTypes.DEFAULT_TYPE, fn, *args, **kwargs):
    """모든 코레일 호출은 이 헬퍼를 통해 직렬화. requests.Session 이
    thread-safe 하지 않아 동시 헌팅 N개가 cookie jar 등을 손상시키는 걸
    막는다."""
    async with context.bot_data[KEY_KORAIL_LOCK]:
        return await asyncio.to_thread(fn, *args, **kwargs)


async def _ensure_login(context: ContextTypes.DEFAULT_TYPE):
    """logined 플래그가 꺼져 있으면 재로그인. 검사+로그인을 같은 lock 안에서
    수행해서 동시에 두 코루틴이 둘 다 login() 을 호출하는 경쟁을 막는다."""
    korail = context.bot_data[KEY_KORAIL]
    async with context.bot_data[KEY_KORAIL_LOCK]:
        if not korail.logined:
            await asyncio.to_thread(korail.login)


# ---------------------------------------------------------------------------
# 기본 명령어
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "korail-hunt 봇\n"
    "/reserve - 예약 시작 (일시 → 역 → 열차 → 옵션)\n"
    "/reservations - 현재 예약\n"
    "/hunt_stop - 헌팅 중단\n"
    "/cancel - 진행 취소\n"
    "/help - 도움말"
)


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


@restricted
async def cmd_reservations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    korail: Korail = context.bot_data[KEY_KORAIL]
    await _ensure_login(context)
    try:
        rsvs = await _korail_call(context, korail.reservations)
    except KorailError as e:
        await update.message.reply_text(f"조회 실패: {e}")
        return
    if not rsvs:
        await update.message.reply_text("예약 없음")
        return
    text = "현재 예약:\n" + "\n".join(repr(r) for r in rsvs)
    await update.message.reply_text(text)


@restricted
async def cmd_hunt_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = _active_hunts(context, chat_id)
    if not active:
        await update.message.reply_text("진행 중인 헌팅 없음")
        return

    # 1개면 바로 중단. 중단 메시지는 헌팅 루프의 CancelledError 핸들러가 보낸다.
    if len(active) == 1:
        next(iter(active.values()))['task'].cancel()
        return

    rows = []
    for hid, entry in active.items():
        rows.append([InlineKeyboardButton(f"[{hid}] {entry['label']}", callback_data=f"stop:{hid}")])
    rows.append([InlineKeyboardButton("⛔ 전부 중단", callback_data="stop:all")])
    rows.append([InlineKeyboardButton("닫기", callback_data="stop:close")])
    await update.message.reply_text(
        f"중단할 헌팅 선택 ({len(active)}개 진행 중):",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_hunt_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/hunt_stop UI 의 'stop:*' 콜백. ConversationHandler 외부에 등록된다."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    target = query.data.split(":", 1)[1]

    if target == "close":
        await query.edit_message_text("(닫힘)")
        return

    active = _active_hunts(context, chat_id)
    if not active:
        await query.edit_message_text("진행 중인 헌팅 없음")
        return

    if target == "all":
        for entry in active.values():
            entry['task'].cancel()
        await query.edit_message_text(f"전체 헌팅 ({len(active)}개) 중단 요청")
        return

    entry = active.get(target)
    if entry is None:
        await query.edit_message_text(f"[{target}] 없음 (이미 끝났을 수 있음)")
        return
    entry['task'].cancel()
    await query.edit_message_text(f"[{target}] 중단 요청")


# ---------------------------------------------------------------------------
# Conversation: /reserve 흐름
# ---------------------------------------------------------------------------

WEEKDAY_KO = ['월', '화', '수', '목', '금', '토', '일']
DATE_OFFSETS = [0, 1, 2, 3, 4, 5, 6, 7, 14, 21, 28]
DATE_ALIASES = {0: '오늘', 1: '내일', 2: '모레'}

POPULAR_STATIONS = [
    '서울', '용산', '광명', '청량리',
    '수원', '천안아산', '오송', '대전',
    '동대구', '부산', '광주송정', '목포',
    '여수EXPO', '영주', '안동', '강릉',
]


def _station_keyboard(prefix):
    rows = []
    for i in range(0, len(POPULAR_STATIONS), 4):
        rows.append([
            InlineKeyboardButton(s, callback_data=f"{prefix}:{s}")
            for s in POPULAR_STATIONS[i:i + 4]
        ])
    rows.append([
        InlineKeyboardButton("직접 입력", callback_data=f"{prefix}:_text"),
        InlineKeyboardButton("취소", callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _date_keyboard(today=None):
    if today is None:
        today = date.today()
    rows, row = [], []
    for offset in DATE_OFFSETS:
        d = today + timedelta(days=offset)
        prefix = DATE_ALIASES.get(offset, f"+{offset}")
        label = f"{prefix} {d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"date:{d.strftime('%Y%m%d')}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("취소", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _time_keyboard():
    rows, row = [], []
    for h in range(24):
        row.append(InlineKeyboardButton(f"{h:02d}시", callback_data=f"time:{h:02d}0000"))
        if len(row) == 6:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton("취소", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


@restricted
async def conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("출발일 선택:", reply_markup=_date_keyboard())
    return ASK_DATE


async def conv_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("취소됨.")
        return ConversationHandler.END
    if not query.data.startswith("date:"):
        return ASK_DATE
    ymd = query.data.split(":", 1)[1]
    context.user_data[KEY_DATE] = ymd
    await query.edit_message_text(
        f"출발일: {ymd[:4]}-{ymd[4:6]}-{ymd[6:]}\n\n출발 시각 선택 (이 시각 이후 열차 검색):",
        reply_markup=_time_keyboard(),
    )
    return ASK_TIME


async def conv_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("취소됨.")
        return ConversationHandler.END
    if not query.data.startswith("time:"):
        return ASK_TIME
    hhmmss = query.data.split(":", 1)[1]
    context.user_data[KEY_TIME] = hhmmss
    await query.edit_message_text(
        f"출발 시각: {hhmmss[:2]}:00 이후\n\n출발역 선택:",
        reply_markup=_station_keyboard("dep"),
    )
    return ASK_DEP


async def conv_dep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("취소됨.")
        return ConversationHandler.END
    if query.data == "dep:_text":
        await query.edit_message_text("출발역 이름을 입력하라. 예: 서울")
        return ASK_DEP
    if not query.data.startswith("dep:"):
        return ASK_DEP
    station = query.data.split(":", 1)[1]
    context.user_data[KEY_DEP] = station
    await query.edit_message_text(
        f"출발역: {station}\n\n도착역 선택:",
        reply_markup=_station_keyboard("arr"),
    )
    return ASK_ARR


async def conv_dep_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[KEY_DEP] = update.message.text.strip()
    await update.message.reply_text(
        f"출발역: {context.user_data[KEY_DEP]}\n\n도착역 선택:",
        reply_markup=_station_keyboard("arr"),
    )
    return ASK_ARR


async def conv_arr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("취소됨.")
        return ConversationHandler.END
    if query.data == "arr:_text":
        await query.edit_message_text("도착역 이름을 입력하라. 예: 부산")
        return ASK_ARR
    if not query.data.startswith("arr:"):
        return ASK_ARR
    station = query.data.split(":", 1)[1]
    context.user_data[KEY_ARR] = station
    await query.edit_message_text(f"도착역: {station}\n\n열차 검색 중...")
    return await _show_trains(update, context)


async def conv_arr_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[KEY_ARR] = update.message.text.strip()
    return await _show_trains(update, context)


async def _show_trains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """열차 검색 결과를 출력하고 선택 키보드를 제시한다."""
    korail: Korail = context.bot_data[KEY_KORAIL]
    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]

    await _ensure_login(context)
    try:
        trains = await _korail_call(
            context, korail.search_train, dep, arr, d, t, include_no_seats=True,
        )
    except NoResultsError:
        trains = []
    except KorailError as e:
        await update.effective_message.reply_text(f"검색 실패: {e}")
        return ConversationHandler.END

    if not trains:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 헌팅 시작", callback_data="hunt"),
            InlineKeyboardButton("취소", callback_data="cancel"),
        ]])
        await update.effective_message.reply_text(
            f"{dep} → {arr} {d} {t}\n좌석 없음.",
            reply_markup=kb,
        )
        return SELECT_TRAIN

    context.user_data[KEY_TRAINS] = trains
    text_lines = [f"{dep} → {arr} {d} {t}", ""]
    buttons = []
    for i, tr in enumerate(trains):
        marker = "" if tr.has_seat() else " (매진)"
        text_lines.append(f"[{i + 1}] {tr!r}{marker}")
        label = f"#{i + 1} 선택" if tr.has_seat() else f"#{i + 1} 헌팅"
        buttons.append([InlineKeyboardButton(label, callback_data=f"train:{i}")])
    buttons.append([
        InlineKeyboardButton("🔁 전체 헌팅", callback_data="hunt"),
        InlineKeyboardButton("취소", callback_data="cancel"),
    ])
    await update.effective_message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SELECT_TRAIN


async def conv_train_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text("취소")
        return ConversationHandler.END

    if data == "hunt":
        await query.edit_message_text(query.message.text + "\n\n(헌팅 시작)")
        await _start_hunt(update, context)
        return ConversationHandler.END

    if data.startswith("train:"):
        idx = int(data.split(":", 1)[1])
        context.user_data[KEY_SELECTED_TRAIN_IDX] = idx
        train = context.user_data[KEY_TRAINS][idx]
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("일반 우선", callback_data="opt:GENERAL_FIRST"),
                InlineKeyboardButton("특실 우선", callback_data="opt:SPECIAL_FIRST"),
            ],
            [
                InlineKeyboardButton("일반만", callback_data="opt:GENERAL_ONLY"),
                InlineKeyboardButton("특실만", callback_data="opt:SPECIAL_ONLY"),
            ],
            [InlineKeyboardButton("취소", callback_data="cancel")],
        ])
        await query.edit_message_text(
            f"선택: {train!r}\n\n좌석 옵션:",
            reply_markup=kb,
        )
        return SELECT_OPTION

    return SELECT_TRAIN


async def conv_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("취소")
        return ConversationHandler.END
    if not query.data.startswith("opt:"):
        return SELECT_OPTION

    option = getattr(ReserveOption, query.data.split(":", 1)[1])
    korail: Korail = context.bot_data[KEY_KORAIL]
    train_idx = context.user_data[KEY_SELECTED_TRAIN_IDX]
    train = context.user_data[KEY_TRAINS][train_idx]

    # 매진 열차면 예약 시도 건너뛰고 즉시 해당 열차 헌팅 시작.
    if not train.has_seat():
        await query.edit_message_text(f"{train!r}\n좌석 없음 — 이 열차 헌팅 시작")
        await _start_train_hunt(update, context, train_idx, option)
        return ConversationHandler.END

    await query.edit_message_text(f"예약 중... {train!r}")
    await _ensure_login(context)
    try:
        rsv = await _korail_call(context, korail.reserve, train, None, option, False)
    except SoldOutError:
        # 검색 후 예약 직전에 매진 — 같은 열차로 헌팅 fallback.
        await update.effective_message.reply_text(
            f"{train!r}\n예약 직전 매진 — 이 열차 헌팅 시작"
        )
        await _start_train_hunt(update, context, train_idx, option)
        return ConversationHandler.END
    except KorailError as e:
        await update.effective_message.reply_text(f"예약 실패: {e}")
        return ConversationHandler.END

    if rsv is None:
        await update.effective_message.reply_text(
            "예약은 시도했지만 응답을 해석하지 못했다. /reservations 로 확인하라."
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        format_reservation_success(rsv),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


@restricted
async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("취소")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# 헌팅 백그라운드
# ---------------------------------------------------------------------------
# bot_data[KEY_HUNT_TASKS] = {chat_id: {hunt_id: {'task': Task, 'label': str}}}
# hunt_id 는 chat 단위로 'h1', 'h2', ... 자동 발급. 한 chat 에서 동시 다수 헌팅 가능.

def _chat_hunts(context, chat_id):
    return context.bot_data.setdefault(KEY_HUNT_TASKS, {}).setdefault(chat_id, {})


def _next_hunt_id(chat_hunts):
    n = 1
    while f"h{n}" in chat_hunts:
        n += 1
    return f"h{n}"


def _format_hunt_label(dep, arr, d, t, train=None):
    date_str = f"{d[4:6]}/{d[6:]}"
    if train is not None:
        time_str = f"{train.dep_time[:2]}:{train.dep_time[2:4]}"
        return f"[{train.train_type_name} {train.train_no}] {dep}→{arr} {date_str} {time_str}"
    time_str = f"{t[:2]}:{t[2:4]}"
    return f"전체 {dep}→{arr} {date_str} {time_str}~"


def _active_hunts(context, chat_id):
    return {hid: e for hid, e in _chat_hunts(context, chat_id).items() if not e['task'].done()}


async def _start_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_hunts = _chat_hunts(context, chat_id)

    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]
    interval = float(os.environ.get('TELEGRAM_HUNT_INTERVAL', '3'))

    hunt_id = _next_hunt_id(chat_hunts)
    label = _format_hunt_label(dep, arr, d, t, train=None)

    await update.effective_message.reply_text(
        f"[{hunt_id}] {label}\n전체 헌팅 시작 (간격 {interval}s). /hunt_stop 으로 중단."
    )

    task = asyncio.create_task(
        _hunt_loop(context, chat_id, hunt_id, label, dep, arr, d, t, interval)
    )
    chat_hunts[hunt_id] = {'task': task, 'label': label}


async def _hunt_loop(context, chat_id, hunt_id, label, dep, arr, d, t, interval):
    korail: Korail = context.bot_data[KEY_KORAIL]
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(context)
                trains = await _korail_call(
                    context, korail.search_train_allday, dep, arr, d, t,
                )
            except NoResultsError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                logger.warning("hunt[%s] search: %s", hunt_id, e)
                korail.logined = False
                await asyncio.sleep(interval)
                continue
            except Exception:
                logger.exception("hunt[%s] unexpected", hunt_id)
                await asyncio.sleep(interval)
                continue

            try:
                rsv = await _korail_call(
                    context, korail.reserve, trains[0], None, ReserveOption.GENERAL_FIRST, False,
                )
            except SoldOutError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                await bot.send_message(chat_id, f"[{hunt_id}] 예약 실패: {e}")
                return

            if rsv is None:
                await bot.send_message(
                    chat_id,
                    f"[{hunt_id}] 예약 시도했지만 응답 해석 실패. /reservations 확인.",
                )
                return

            await bot.send_message(
                chat_id,
                f"🎉 [{hunt_id}] {label}\n헌팅 성공 ({attempts}회 시도)\n\n{format_reservation_success(rsv)}",
                parse_mode=ParseMode.HTML,
            )
            return
    except asyncio.CancelledError:
        await bot.send_message(chat_id, f"[{hunt_id}] 헌팅 중단 ({attempts}회 시도)")
        raise
    finally:
        _chat_hunts(context, chat_id).pop(hunt_id, None)


async def _start_train_hunt(update, context, train_idx, option):
    """특정 열차 한 대만 노린 헌팅. 검색 후 매진 떴을 때 진입."""
    chat_id = update.effective_chat.id
    chat_hunts = _chat_hunts(context, chat_id)

    train = context.user_data[KEY_TRAINS][train_idx]
    target = (train.train_no, train.dep_date, train.dep_time)
    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]
    interval = float(os.environ.get('TELEGRAM_HUNT_INTERVAL', '3'))

    hunt_id = _next_hunt_id(chat_hunts)
    label = _format_hunt_label(dep, arr, d, t, train=train)

    await update.effective_message.reply_text(
        f"[{hunt_id}] {label}\n옵션: {option}, 간격 {interval}s. /hunt_stop 으로 중단."
    )

    task = asyncio.create_task(
        _train_hunt_loop(context, chat_id, hunt_id, label, target, dep, arr, d, t, option, interval)
    )
    chat_hunts[hunt_id] = {'task': task, 'label': label}


async def _train_hunt_loop(context, chat_id, hunt_id, label, target, dep, arr, d, t, option, interval):
    korail: Korail = context.bot_data[KEY_KORAIL]
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(context)
                trains = await _korail_call(
                    context, korail.search_train, dep, arr, d, t, include_no_seats=True,
                )
            except NoResultsError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                logger.warning("train hunt[%s] search: %s", hunt_id, e)
                korail.logined = False
                await asyncio.sleep(interval)
                continue
            except Exception:
                logger.exception("train hunt[%s] unexpected", hunt_id)
                await asyncio.sleep(interval)
                continue

            match = next(
                (tr for tr in trains
                 if (tr.train_no, tr.dep_date, tr.dep_time) == target),
                None,
            )
            if match is None or not match.has_seat():
                await asyncio.sleep(interval)
                continue

            try:
                rsv = await _korail_call(context, korail.reserve, match, None, option, False)
            except SoldOutError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                await bot.send_message(chat_id, f"[{hunt_id}] 예약 실패: {e}")
                return

            if rsv is None:
                await bot.send_message(
                    chat_id,
                    f"[{hunt_id}] 예약 시도했지만 응답 해석 실패. /reservations 확인.",
                )
                return

            await bot.send_message(
                chat_id,
                f"🎉 [{hunt_id}] {label}\n열차 헌팅 성공 ({attempts}회 시도)\n\n{format_reservation_success(rsv)}",
                parse_mode=ParseMode.HTML,
            )
            return
    except asyncio.CancelledError:
        await bot.send_message(chat_id, f"[{hunt_id}] 열차 헌팅 중단 ({attempts}회 시도)")
        raise
    finally:
        _chat_hunts(context, chat_id).pop(hunt_id, None)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        level=logging.INFO,
    )
    # PTB 가 자체 로깅이 많아 INFO 이상으로 낮춤
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)

    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수가 필요하다 (.env 확인)")

    korail_id = os.environ.get('KORAIL_ID')
    korail_pw = os.environ.get('KORAIL_PW')
    if not (korail_id and korail_pw):
        raise SystemExit("KORAIL_ID/KORAIL_PW 환경변수가 필요하다 (.env 확인)")

    korail = PatchedKorail(korail_id, korail_pw, auto_login=False)
    if not korail.login():
        raise SystemExit("코레일 로그인 실패 — 자격증명을 확인하라")

    app = Application.builder().token(token).build()
    app.bot_data[KEY_KORAIL] = korail
    app.bot_data[KEY_KORAIL_LOCK] = asyncio.Lock()
    app.bot_data[KEY_HUNT_TASKS] = {}

    # 콜백 패턴을 명시해서 ConversationHandler 가 자기 화면의 콜백만 잡도록.
    # 그래야 '/hunt_stop' 의 stop:* 콜백이 대화 도중에도 안전히 외부 핸들러로 흐른다.
    conv = ConversationHandler(
        entry_points=[CommandHandler('reserve', conv_start)],
        states={
            ASK_DATE: [CallbackQueryHandler(conv_date, pattern=r"^(date:|cancel$)")],
            ASK_TIME: [CallbackQueryHandler(conv_time, pattern=r"^(time:|cancel$)")],
            ASK_DEP: [
                CallbackQueryHandler(conv_dep, pattern=r"^(dep:|cancel$)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_dep_text),
            ],
            ASK_ARR: [
                CallbackQueryHandler(conv_arr, pattern=r"^(arr:|cancel$)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_arr_text),
            ],
            SELECT_TRAIN: [CallbackQueryHandler(conv_train_pick, pattern=r"^(train:|hunt$|cancel$)")],
            SELECT_OPTION: [CallbackQueryHandler(conv_option, pattern=r"^(opt:|cancel$)")],
        },
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    )

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(CommandHandler('reservations', cmd_reservations))
    app.add_handler(CommandHandler('hunt_stop', cmd_hunt_stop))
    app.add_handler(CallbackQueryHandler(cb_hunt_stop, pattern=r"^stop:"))
    app.add_handler(conv)

    logger.info("봇 시작 (인증 chat_id=%s)", authorized_chat_id())
    app.run_polling()


if __name__ == '__main__':
    main()
