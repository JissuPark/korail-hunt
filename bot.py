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

async def _ensure_login(korail: Korail):
    """logined 플래그가 꺼져 있으면 재로그인. 만료된 세션은 여기서 회복된다."""
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
    await _ensure_login(korail)
    try:
        rsvs = await asyncio.to_thread(korail.reservations)
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
    tasks = context.bot_data.setdefault(KEY_HUNT_TASKS, {})
    chat_id = update.effective_chat.id
    task = tasks.pop(chat_id, None)
    if task is None or task.done():
        await update.message.reply_text("진행 중인 헌팅 없음")
        return
    task.cancel()
    await update.message.reply_text("헌팅 중단 요청")


# ---------------------------------------------------------------------------
# Conversation: /reserve 흐름
# ---------------------------------------------------------------------------

@restricted
async def conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "출발일을 입력하라.\n예: 20260601 · 오늘 · 내일 · 모레 · +7"
    )
    return ASK_DATE


async def conv_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data[KEY_DATE] = parse_date(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"{e}\n다시 입력하라.")
        return ASK_DATE
    await update.message.reply_text(
        f"출발 시각을 입력하라.\n예: 1000 · 10:00 · 100000"
    )
    return ASK_TIME


async def conv_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data[KEY_TIME] = parse_time(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"{e}\n다시 입력하라.")
        return ASK_TIME
    await update.message.reply_text("출발역을 입력하라. 예: 서울")
    return ASK_DEP


async def conv_dep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[KEY_DEP] = update.message.text.strip()
    await update.message.reply_text("도착역을 입력하라. 예: 부산")
    return ASK_ARR


async def conv_arr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[KEY_ARR] = update.message.text.strip()
    return await _show_trains(update, context)


async def _show_trains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """열차 검색 결과를 출력하고 선택 키보드를 제시한다."""
    korail: Korail = context.bot_data[KEY_KORAIL]
    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]

    await _ensure_login(korail)
    try:
        trains = await asyncio.to_thread(
            korail.search_train, dep, arr, d, t, include_no_seats=True,
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
        text_lines.append(f"[{i + 1}] {tr!r}")
        if tr.has_seat():
            buttons.append([InlineKeyboardButton(f"#{i + 1} 선택", callback_data=f"train:{i}")])
    buttons.append([
        InlineKeyboardButton("🔁 헌팅", callback_data="hunt"),
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
    train = context.user_data[KEY_TRAINS][context.user_data[KEY_SELECTED_TRAIN_IDX]]

    await query.edit_message_text(f"예약 중... {train!r}")
    await _ensure_login(korail)
    try:
        rsv = await asyncio.to_thread(korail.reserve, train, None, option, False)
    except SoldOutError:
        await update.effective_message.reply_text("매진. /reserve 로 다시 시도하라.")
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

async def _start_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tasks = context.bot_data.setdefault(KEY_HUNT_TASKS, {})
    if chat_id in tasks and not tasks[chat_id].done():
        await update.effective_message.reply_text("이미 헌팅 중. /hunt_stop 으로 중단 가능")
        return

    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]
    interval = float(os.environ.get('TELEGRAM_HUNT_INTERVAL', '3'))

    await update.effective_message.reply_text(
        f"헌팅 시작: {dep} → {arr} {d} {t} (간격 {interval}s)\n"
        f"좌석이 잡히면 자동으로 예약한다. /hunt_stop 으로 중단."
    )

    task = asyncio.create_task(
        _hunt_loop(context, chat_id, dep, arr, d, t, interval)
    )
    tasks[chat_id] = task


async def _hunt_loop(context, chat_id, dep, arr, d, t, interval):
    korail: Korail = context.bot_data[KEY_KORAIL]
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(korail)
                trains = await asyncio.to_thread(
                    korail.search_train_allday, dep, arr, d, t,
                )
            except NoResultsError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                logger.warning("hunt search: %s", e)
                korail.logined = False  # 다음 사이클에서 재로그인
                await asyncio.sleep(interval)
                continue
            except Exception:
                logger.exception("hunt unexpected")
                await asyncio.sleep(interval)
                continue

            try:
                rsv = await asyncio.to_thread(
                    korail.reserve, trains[0], None, ReserveOption.GENERAL_FIRST, False,
                )
            except SoldOutError:
                # 검색 후 사이에 매진 — 다시 돌린다
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                await bot.send_message(chat_id, f"헌팅 중 예약 실패: {e}")
                return

            if rsv is None:
                await bot.send_message(
                    chat_id,
                    "예약 시도했지만 응답 해석 실패. /reservations 로 확인하라.",
                )
                return

            await bot.send_message(
                chat_id,
                f"🎉 헌팅 성공 ({attempts}회 시도)\n\n{format_reservation_success(rsv)}",
                parse_mode=ParseMode.HTML,
            )
            return
    except asyncio.CancelledError:
        await bot.send_message(chat_id, f"헌팅 중단 ({attempts}회 시도)")
        raise
    finally:
        tasks = context.bot_data.get(KEY_HUNT_TASKS, {})
        tasks.pop(chat_id, None)


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

    korail = Korail(korail_id, korail_pw, auto_login=False)
    if not korail.login():
        raise SystemExit("코레일 로그인 실패 — 자격증명을 확인하라")

    app = Application.builder().token(token).build()
    app.bot_data[KEY_KORAIL] = korail
    app.bot_data[KEY_HUNT_TASKS] = {}

    conv = ConversationHandler(
        entry_points=[CommandHandler('reserve', conv_start)],
        states={
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_date)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_time)],
            ASK_DEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_dep)],
            ASK_ARR: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_arr)],
            SELECT_TRAIN: [CallbackQueryHandler(conv_train_pick)],
            SELECT_OPTION: [CallbackQueryHandler(conv_option)],
        },
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    )

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(CommandHandler('reservations', cmd_reservations))
    app.add_handler(CommandHandler('hunt_stop', cmd_hunt_stop))
    app.add_handler(conv)

    logger.info("봇 시작 (인증 chat_id=%s)", authorized_chat_id())
    app.run_polling()


if __name__ == '__main__':
    main()
