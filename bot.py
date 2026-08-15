"""
korail-hunt Telegram 봇.

chat 별로 각자의 코레일 계정을 쓴다. 자격증명은 **메모리에만** 보관하며 디스크에
쓰지 않는다. 따라서 봇을 재시작하면 모든 세션이 날아가고 각자 /login 을 다시 해야
한다. 재시작 시에는 마지막으로 로그인/헌팅 중이던 chat 에게 안내를 보낸다.

흐름:
  /login    → 코레일 아이디 → 비밀번호 (입력 즉시 메시지 삭제) → 세션 생성
  /reserve  → 인원 → 출발일 → 출발시각 → 출발역 → 도착역 → 열차 선택 → 좌석옵션 → 예약
  좌석이 없으면 [헌팅 시작] 버튼이 노출되고, polling 으로 자동 예약을 시도한다.
  /logout         → 세션 파기 (진행 중 헌팅도 중단)
  /hunt_stop      → 진행 중인 헌팅 중단
  /reservations   → 현재 예약 목록
  /cancel         → 대화 취소
  /help           → 도움말

실행:
  pip install -e ".[bot]"
  .env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS 설정
  python bot.py
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import time
from datetime import date, timedelta
from functools import wraps

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

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

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
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
    AdultPassenger,
    ChildPassenger,
    Korail,
    KorailError,
    NoResultsError,
    PatchedKorail,
    ReserveOption,
    SeniorPassenger,
    SoldOutError,
    ToddlerPassenger,
)

logger = logging.getLogger(__name__)

# ConversationHandler states — 두 대화가 독립이라 값이 겹치지 않게 분리한다.
ASK_COUNT, ASK_DATE, ASK_TIME, ASK_DEP, ASK_ARR, SELECT_TRAIN, SELECT_OPTION = range(7)
LOGIN_ID, LOGIN_PW = range(100, 102)

# context.user_data 키
KEY_ADULTS = 'adults'
KEY_DATE = 'date'
KEY_TIME = 'time'
KEY_DEP = 'dep'
KEY_ARR = 'arr'
KEY_TRAINS = 'trains'
KEY_SELECTED_TRAIN_IDX = 'sel'
KEY_LOGIN_ID = 'login_id'

# context.bot_data 키
KEY_SESSIONS = 'sessions'  # chat_id -> Session (메모리 전용, 절대 영속화 금지)
KEY_HUNT_TASKS = 'hunt_tasks'  # chat_id -> {hunt_id: {'task', 'label', 'spec'}}
KEY_RESUME_HUNTS = 'resume_hunts'  # 핸드오프에서 읽은 헌팅 조건. restore_hunts 가 소비한다.
KEY_SHUTDOWN = 'shutdown'  # 종료 스냅샷 확정 후 True
KEY_USERS = 'users'  # {'approved': {chat_id: info}, 'denied': {chat_id: info}}
KEY_PENDING = 'pending'  # chat_id -> info. 승인 대기열 (메모리 전용)

# 재시작 안내용 스냅샷 파일. 자격증명은 절대 기록하지 않는다.
STATE_FILE = os.environ.get('BOT_STATE_FILE') or '.bot_state.json'
# 승인된 사용자 목록. chat_id 와 표시 이름뿐이라 영속화해도 안전하며,
# 영속화해야 재시작·재배포가 승인 목록을 날리지 않는다.
USERS_FILE = os.environ.get('BOT_USERS_FILE') or '.bot_users.json'
# 세션 핸드오프 파일. 정상 종료 시에만 쓰고 기동 직후 읽자마자 지운다.
HANDOFF_FILE = os.environ.get('BOT_HANDOFF_FILE') or '.bot_handoff.enc'
HANDOFF_TTL = float(os.environ.get('SESSION_HANDOFF_TTL') or 300)


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


def normalize_korail_id(text):
    """코레일 로그인 ID 정규화.

    코레일은 휴대폰번호를 하이픈 형식(PHONE_NUMBER_REGEX)으로만 인식한다.
    '01012345678' 처럼 붙여 쓰면 회원번호로 분류돼 '잘못 입력하셨습니다(회원번호)'
    로 거부되므로 여기서 하이픈을 넣어준다. 회원번호는 8자리라 10~11자리인
    휴대폰번호와 겹치지 않는다.
    """
    text = text.strip()
    if text.isdigit() and text.startswith('0') and len(text) in (10, 11):
        if len(text) == 11:
            return f"{text[:3]}-{text[3:7]}-{text[7:]}"
        return f"{text[:3]}-{text[3:6]}-{text[6:]}"
    return text


def parse_time(text):
    """HHMM / HHMMSS / HH:MM / HH:MM:SS 형식을 HHMMSS 로 변환."""
    text = text.strip().replace(':', '')
    if len(text) == 4 and text.isdigit():
        return text + '00'
    if len(text) == 6 and text.isdigit():
        return text
    raise ValueError(f"시각 형식을 인식할 수 없음: {text!r}. HHMM / HHMMSS / HH:MM 사용")


# ---------------------------------------------------------------------------
# 인원
# ---------------------------------------------------------------------------
# 코레일은 한 번의 예약에 여러 명을 묶을 수 있고, 그래야 같은 예약번호(PNR)로
# 좌석이 붙어서 배정되고 결제도 한 번에 끝난다. 1명씩 여러 번 예약하면 좌석이
# 흩어지므로 passengers 리스트를 검색·예약 전 구간에 그대로 흘려보낸다.

# 인원 하한/상한. 코레일 예매는 1회 9매까지라는 게 통설이고 공식 문서로 확인은
# 못 했지만, 그 이상을 허용해봐야 예약 단계에서 코레일이 거절할 뿐이라 9로 막는다.
PASSENGER_MIN = 1
PASSENGER_MAX = 9

# 표시 순서와 이름. 지금 UI 는 어른만 고르게 하지만, 여기에 항목이 늘어도
# 라벨·메시지 쪽 호출부는 그대로 두면 되도록 리스트로 다룬다.
PASSENGER_LABELS = (
    (AdultPassenger, '어른'),
    (SeniorPassenger, '경로'),
    (ChildPassenger, '어린이'),
    (ToddlerPassenger, '유아'),
)


def clamp_passenger_count(n):
    """인원을 [PASSENGER_MIN, PASSENGER_MAX] 로 잘라낸다.

    콜백 데이터는 사용자가 오래된 키보드를 다시 눌러 들어올 수도 있어서
    범위를 신뢰할 수 없다. 예외로 대화를 끊는 대신 조용히 자른다.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return PASSENGER_MIN
    return max(PASSENGER_MIN, min(PASSENGER_MAX, n))


def build_passengers(adults):
    """인원수를 코레일 Passenger 리스트로 바꾼다.

    라이브러리는 passengers=None 을 어른 1명으로 취급하지만, 검색과 예약에
    같은 리스트를 명시적으로 넘겨야 '좌석 있음' 판정과 실제 예약 매수가
    어긋나지 않는다.
    """
    return [AdultPassenger(clamp_passenger_count(adults))]


def describe_passengers(passengers):
    """'어른 2명' 같은 인원 문구. 헌팅이 여러 개 동시에 돌 때 라벨만 보고
    어느 게 몇 명짜리인지 구분할 수 있어야 한다."""
    parts = []
    for cls, name in PASSENGER_LABELS:
        count = sum(p.count for p in passengers if isinstance(p, cls))
        if count > 0:
            parts.append(f"{name} {count}명")
    return ' '.join(parts) if parts else f"어른 {PASSENGER_MIN}명"


def passengers_of(context):
    """대화 상태에서 Passenger 리스트를 만든다. 인원 단계를 거치지 않은
    흐름(구버전 대화가 남아 있는 경우 등)도 1명으로 안전하게 동작한다."""
    return build_passengers(context.user_data.get(KEY_ADULTS, PASSENGER_MIN))


def format_reservation_success(rsv, passengers=None):
    """예약 성공 메시지 (HTML).

    좌석을 잡아둔 것과 승차권을 산 것은 다르다. 처음 쓰는 사람이 가장 자주 놓치는
    지점이라 결제 안내를 눈에 띄게 붙인다.
    """
    buy_dt = f"{rsv.buy_limit_date[:4]}-{rsv.buy_limit_date[4:6]}-{rsv.buy_limit_date[6:]}"
    buy_tm = f"{rsv.buy_limit_time[:2]}:{rsv.buy_limit_time[2:4]}"
    psg_line = f"<b>인원</b>: {describe_passengers(passengers)}\n" if passengers else ""
    return (
        f"✅ <b>예약 성공</b>\n\n"
        f"{html.escape(repr(rsv))}\n\n"
        f"<b>예약번호</b>: <code>{html.escape(str(rsv.rsv_id))}</code>\n"
        f"{psg_line}"
        f"<b>구입기한</b>: {buy_dt} {buy_tm}\n"
        f"<b>금액</b>: {rsv.price:,}원 ({rsv.seat_no_count}석)\n\n"
        f"💳 <b>결제는 봇이 하지 않는다. 직접 해야 한다.</b>\n"
        f"코레일톡 앱 → 승차권 → 결제대기 에서 위 구입기한까지 결제하라.\n"
        f"기한(보통 10분)이 지나면 예약은 자동으로 취소된다."
    )


def format_status(context, chat_id, exclude=None):
    """현재 상태 요약 (HTML).

    /reserve 를 끝낸 뒤 "지금 뭐가 돌고 있나"를 알기 어려워서 붙인다.

    일부러 코레일을 조회하지 않는다. 헌팅 루프가 이미 같은 계정으로 몇 초마다
    조회를 때리고 있어서, 안내 한 줄 때문에 호출을 더 얹으면 anti-bot 위험만
    커진다. 메모리에 있는 헌팅 목록으로 충분하고, 실제 예약 내역은 이미
    조회를 하는 /reservations 로 넘긴다.

    exclude 는 방금 끝난 헌팅의 hunt_id. 루프의 finally 가 아직 돌지 않아
    자기 자신이 "진행 중"으로 보이는 걸 막는다.
    """
    active = {
        hid: entry for hid, entry in _active_hunts(context, chat_id).items()
        if hid != exclude
    }
    lines = ["📋 <b>현재 상태</b>", ""]
    if active:
        lines.append(f"🔁 <b>진행 중인 헌팅 {len(active)}개</b>")
        for hid, entry in active.items():
            lines.append(f"· [<code>{html.escape(hid)}</code>] {html.escape(entry['label'])}")
        lines.append("")
        lines.append("중단하려면 /hunt_stop · 예약 내역은 /reservations")
    else:
        lines.append("🔁 진행 중인 헌팅 없음")
        lines.append("예약 내역은 /reservations · 새로 잡으려면 /reserve")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------

def authorized_chat_id():
    """하위호환: 단일 chat 만 쓰던 시절의 환경변수. allowed_chat_ids() 가 흡수한다."""
    v = os.environ.get('TELEGRAM_AUTHORIZED_CHAT_ID')
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        logger.warning("TELEGRAM_AUTHORIZED_CHAT_ID 가 정수가 아니다: %r", v)
        return None


def allowed_chat_ids():
    """봇 사용이 허용된 chat_id 집합. TELEGRAM_ALLOWED_CHAT_IDS 는 쉼표 구분."""
    ids = set()
    for part in (os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS') or '').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("TELEGRAM_ALLOWED_CHAT_IDS 항목이 정수가 아니다: %r", part)
    legacy = authorized_chat_id()
    if legacy is not None:
        ids.add(legacy)
    return ids


def admin_chat_ids():
    """관리자 chat_id 집합. 승인 요청을 받고 사용자를 등록/해제할 수 있다."""
    ids = set()
    for part in (os.environ.get('TELEGRAM_ADMIN_CHAT_IDS') or '').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("TELEGRAM_ADMIN_CHAT_IDS 항목이 정수가 아니다: %r", part)
    return ids


def users(context):
    return context.bot_data.setdefault(KEY_USERS, {'approved': {}, 'denied': {}})


def is_allowed(context, chat_id):
    """관리자 / .env 고정 허용 / 관리자가 승인한 사용자."""
    if chat_id is None:
        return False
    if chat_id in admin_chat_ids() or chat_id in allowed_chat_ids():
        return True
    return chat_id in users(context)['approved']


def describe_user(user, chat_id):
    """관리자에게 보여줄 요청자 설명. 이름은 사용자가 정하므로 반드시 이스케이프."""
    name = html.escape(user.full_name) if user and user.full_name else '(이름 없음)'
    line = f"<b>{name}</b>"
    if user and user.username:
        line += f" (@{html.escape(user.username)})"
    return f"{line}\nchat_id: <code>{chat_id}</code>"


def _user_info(user):
    return {
        'name': (user.full_name if user else '') or '',
        'username': (user.username if user else '') or '',
    }


def _user_label(info, chat_id):
    name = html.escape(info.get('name') or '(이름 없음)')
    username = info.get('username')
    if username:
        name += f" (@{html.escape(username)})"
    return f"{name} — <code>{chat_id}</code>"


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id):
    """미승인 사용자의 접근 요청을 관리자에게 전달한다."""
    admins = admin_chat_ids()
    if not admins:
        # 부트스트랩: 첫 사용자에게 본인 chat_id 를 알려준다.
        await update.effective_message.reply_text(
            "⚠️ 아직 관리자가 설정되어 있지 않다.\n"
            f"이 chat_id 를 .env 의 TELEGRAM_ADMIN_CHAT_IDS 에 적고 "
            f"봇을 재시작하라: <code>{chat_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if chat_id in users(context)['denied']:
        await update.effective_message.reply_text("⛔ 접근이 거부된 계정이다.")
        return

    pending = context.bot_data.setdefault(KEY_PENDING, {})
    if chat_id in pending:
        await update.effective_message.reply_text(
            "⏳ 이미 승인 요청이 접수됐다. 관리자가 승인하면 알림이 온다."
        )
        return

    user = update.effective_user
    pending[chat_id] = _user_info(user)
    logger.info("접근 요청: chat_id=%s", chat_id)

    await update.effective_message.reply_text(
        "🔒 이 봇은 승인된 사람만 쓸 수 있다.\n"
        "관리자에게 승인 요청을 보냈으니 기다려라. 승인되면 여기로 알림이 온다."
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 승인", callback_data=f"access:approve:{chat_id}"),
        InlineKeyboardButton("⛔ 거부", callback_data=f"access:deny:{chat_id}"),
    ]])
    for admin_id in admins:
        try:
            await context.bot.send_message(
                admin_id,
                f"🔔 <b>새 접근 요청</b>\n\n{describe_user(user, chat_id)}",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("승인 요청 전송 실패 (admin=%s): %s", admin_id, e)


def restricted(func):
    """승인된 chat 만 처리한다. 미승인이면 관리자 승인 절차로 넘긴다."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not is_allowed(context, chat_id):
            await request_access(update, context, chat_id)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


def admin_only(func):
    """관리자 전용 명령."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id not in admin_chat_ids():
            await update.effective_message.reply_text("관리자 전용 명령이다.")
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


def with_session(func):
    """restricted + 코레일 세션 주입. 핸들러는 (update, context, session) 을 받는다."""
    @restricted
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        session = get_session(context, update.effective_chat.id)
        if session is None:
            await update.effective_message.reply_text(
                "🔐 먼저 코레일 로그인이 필요하다.\n"
                "/login 으로 본인 코레일 계정을 등록하라. 처음이라면 /help 를 먼저 봐라."
            )
            return ConversationHandler.END
        return await func(update, context, session, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 세션 (메모리 전용)
# ---------------------------------------------------------------------------

class Session:
    """chat 하나에 대응하는 코레일 로그인 세션.

    자격증명은 이 객체(및 그 안의 Korail 인스턴스)의 수명 동안 프로세스 메모리에만
    존재한다. 디스크·로그 어디에도 쓰지 않으므로 프로세스가 죽으면 함께 사라진다.

    lock 은 세션마다 따로 둔다. requests.Session 이 thread-safe 하지 않아 같은
    계정의 동시 호출은 직렬화해야 하지만, 다른 계정끼리는 서로 막을 이유가 없다.
    """
    __slots__ = ('korail', 'lock')

    def __init__(self, korail):
        self.korail = korail
        self.lock = asyncio.Lock()

    def __repr__(self):
        # 자격증명이 로그로 새지 않도록 회원번호만 노출한다.
        return f"<Session member={self.korail.membership_number}>"


def sessions(context):
    return context.bot_data.setdefault(KEY_SESSIONS, {})


def get_session(context, chat_id):
    return sessions(context).get(chat_id)


async def _session_or_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """대화 중간 단계용. 세션이 없으면 안내하고 None 을 반환한다."""
    session = get_session(context, update.effective_chat.id)
    if session is None:
        await update.effective_message.reply_text(
            "🔐 로그인 세션이 사라졌다 (로그아웃 또는 봇 재시작).\n"
            "/login 으로 다시 로그인한 뒤 /reserve 로 시도하라."
        )
    return session


# ---------------------------------------------------------------------------
# 재시작 대응
# ---------------------------------------------------------------------------
# 세션은 메모리 전용이라 재시작하면 전부 날아간다. 복구는 불가능하지만, 최소한
# "누가 로그인해 있었고 무슨 헌팅이 돌고 있었는지"는 남겨 두었다가 재시작 직후
# 안내를 보낸다. 파일에는 chat_id 와 헌팅 라벨만 들어가고 자격증명은 넣지 않는다.

def load_users(app):
    """승인 목록을 디스크에서 읽어 bot_data 에 올린다 (기동 시 1회)."""
    data = {'approved': {}, 'denied': {}}
    try:
        with open(USERS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("승인 목록 읽기 실패: %s", e)
    else:
        for bucket in ('approved', 'denied'):
            for k, v in (raw.get(bucket) or {}).items():
                try:
                    data[bucket][int(k)] = v or {}
                except (TypeError, ValueError):
                    logger.warning("승인 목록의 chat_id 가 정수가 아니다: %r", k)
    app.bot_data[KEY_USERS] = data
    logger.info("승인된 사용자 %d명 로드", len(data['approved']))
    return data


def save_users(context):
    data = users(context)
    out = {
        bucket: {str(k): v for k, v in data[bucket].items()}
        for bucket in ('approved', 'denied')
    }
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("승인 목록 기록 실패: %s", e)


# ---------------------------------------------------------------------------
# 세션 핸드오프
# ---------------------------------------------------------------------------
# 재배포마다 전원이 다시 로그인하는 걸 피하기 위한 장치. 정상 종료 시 세션을
# 암호화해 디스크에 쓰고, 기동 직후 복원한 뒤 파일을 즉시 지운다. 디스크 체류
# 시간은 재시작에 걸리는 수 초뿐이다.
#
# 진행 중이던 헌팅 조건도 같은 페이로드에 실어 보낸다. 조건 자체에는 자격증명이
# 없지만, 헌팅을 되살리려면 어차피 세션이 있어야 해서 둘의 수명이 같다. 별도
# 평문 파일로 빼면 "누가 언제 어디로 가려 했는지"가 디스크에 남으므로 같이 묶는다.
#
# 안전 장치:
#   - SESSION_HANDOFF_KEY 가 없으면 기능 자체가 꺼진다 (명시적 옵트인)
#   - AES-GCM 으로 암호화·인증하므로 변조된 파일은 거부된다
#   - 타임스탬프가 페이로드 안에 있고 TTL(기본 5분)이 지나면 거부한다.
#     봇이 오래 죽어 있었다면 방치된 파일이 재사용되지 않는다
#   - 크래시·전원 차단이면 파일이 아예 없으므로 안전한 쪽(재로그인)으로 떨어진다
#
# 한계: 암호화 키가 같은 서버에 있으므로 서버가 털리면 무의미하다. 다만 root
# 권한 공격자는 /proc/<pid>/mem 으로 프로세스 메모리도 읽을 수 있어서, 메모리
# 전용 보관 대비 실질적인 노출 증가는 크지 않다.

def handoff_key():
    raw = (os.environ.get('SESSION_HANDOFF_KEY') or '').strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode('utf-8')).digest()


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    nonce = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + tag + ciphertext


def _decrypt(blob: bytes, key: bytes) -> bytes:
    if len(blob) < 28:
        raise ValueError("핸드오프 파일이 너무 짧다")
    cipher = AES.new(key, AES.MODE_GCM, nonce=blob[:12])
    return cipher.decrypt_and_verify(blob[28:], blob[12:28])


def _dump_session(session):
    korail = session.korail
    return {
        'korail_id': korail.korail_id,
        'korail_pw': korail.korail_pw,
        'key': korail._key,
        'idx': korail._idx,
        'membership_number': korail.membership_number,
        'name': korail.name,
        'email': korail.email,
        'logined': bool(korail.logined),
        'cookies': [
            {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
            for c in korail._session.cookies
        ],
    }


def _load_session(record):
    korail = PatchedKorail(record['korail_id'], record['korail_pw'], auto_login=False)
    korail._key = record['key']
    korail._idx = record['idx']
    korail.membership_number = record['membership_number']
    korail.name = record['name']
    korail.email = record['email']
    korail.logined = record['logined']
    for c in record.get('cookies') or []:
        korail._session.cookies.set(
            c['name'], c['value'], domain=c.get('domain', ''), path=c.get('path', '/'),
        )
    return Session(korail)


def dump_sessions(app):
    """post_stop 에서 호출. 세션이 없거나 기능이 꺼져 있으면 아무것도 안 한다."""
    key = handoff_key()
    live = sessions(app)
    if not key or not live:
        if live and not key:
            logger.info(
                "SESSION_HANDOFF_KEY 미설정 — 세션 %d개를 넘기지 않는다 (전원 재로그인 필요)",
                len(live),
            )
        return False

    payload = {
        'ts': time.time(),
        'sessions': {str(cid): _dump_session(s) for cid, s in live.items()},
        'hunts': _dump_hunts(app),
    }
    blob = _encrypt(json.dumps(payload).encode('utf-8'), key)
    try:
        # 0600 으로 생성한다. 먼저 만들고 chmod 하면 그 사이 창이 생긴다.
        fd = os.open(HANDOFF_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
    except OSError as e:
        logger.warning("세션 핸드오프 기록 실패: %s", e)
        return False
    logger.info(
        "세션 %d개, 헌팅 %d개 핸드오프 기록",
        len(live), sum(len(v) for v in payload['hunts'].values()),
    )
    return True


def restore_sessions(app):
    """post_init 에서 호출. 복원된 chat_id 집합을 반환하고 파일은 즉시 지운다."""
    key = handoff_key()
    try:
        with open(HANDOFF_FILE, 'rb') as f:
            blob = f.read()
    except FileNotFoundError:
        return frozenset()
    except OSError as e:
        logger.warning("핸드오프 파일 읽기 실패: %s", e)
        return frozenset()
    finally:
        pass

    # 무엇을 하든 파일은 지운다. 복원에 실패해도 남겨둘 이유가 없다.
    def _discard():
        try:
            os.remove(HANDOFF_FILE)
        except OSError:
            pass

    if not key:
        logger.warning("핸드오프 파일이 있으나 SESSION_HANDOFF_KEY 가 없다 — 폐기")
        _discard()
        return frozenset()

    try:
        payload = json.loads(_decrypt(blob, key))
    except (ValueError, KeyError) as e:
        # 키 불일치·변조·손상. 조용히 넘어가면 안 되지만 기동은 계속해야 한다.
        logger.warning("핸드오프 복호화 실패 (키 변경 또는 손상): %s", e)
        _discard()
        return frozenset()
    _discard()

    age = time.time() - float(payload.get('ts') or 0)
    if age > HANDOFF_TTL:
        logger.warning("핸드오프가 오래됐다 (%.0f초 > TTL %.0f초) — 폐기", age, HANDOFF_TTL)
        return frozenset()

    restored = set()
    store = sessions(app)
    for raw_id, record in (payload.get('sessions') or {}).items():
        try:
            chat_id = int(raw_id)
            store[chat_id] = _load_session(record)
        except (TypeError, ValueError, KeyError) as e:
            logger.warning("세션 복원 실패 (chat_id=%r): %s", raw_id, e)
            continue
        restored.add(chat_id)
    logger.info("세션 %d개 복원 (경과 %.0f초)", len(restored), age)
    # 헌팅 재개는 task 생성이라 실행 중인 루프가 필요하다. 여기서는 조건만 넘겨두고
    # restore_hunts() 가 소비한다. 이 함수는 동기 호출도 되어야 해서 분리했다.
    app.bot_data[KEY_RESUME_HUNTS] = payload.get('hunts') or {}
    return frozenset(restored)


def save_state(context):
    # 종료 스냅샷이 확정된 뒤에는, 정리되는 헌팅 task 의 finally 가 파일을
    # 덮어써서 "중단된 헌팅" 목록을 지워버리지 않도록 무시한다.
    if context.bot_data.get(KEY_SHUTDOWN):
        return
    snapshot = {}
    for chat_id in sessions(context):
        snapshot[str(chat_id)] = []
    for chat_id, hunts in context.bot_data.get(KEY_HUNT_TASKS, {}).items():
        labels = [e['label'] for e in hunts.values() if not e['task'].done()]
        if labels:
            snapshot.setdefault(str(chat_id), []).extend(labels)
    try:
        if not snapshot:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
            return
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("상태 파일 기록 실패: %s", e)


async def snapshot_on_stop(app: Application):
    """post_stop 훅. 종료 시점의 로그인/헌팅 현황을 확정 기록한다.

    헌팅 task 는 여기서 명시적으로 취소하지 않는다. 취소하면 각 루프의 finally 가
    돌면서 방금 기록한 스냅샷을 지우기 때문이다. 대신 플래그를 세워 이후의
    save_state() 호출을 무력화한다.
    """
    save_state(app)
    app.bot_data[KEY_SHUTDOWN] = True
    if not dump_sessions(app):
        logger.info("종료 — 세션 %d개가 메모리에서 사라진다", len(sessions(app)))


async def on_startup(app: Application):
    """post_init 훅. 승인 목록 적재 → 세션 복원 → 헌팅 재개 → 재시작 안내 순서."""
    load_users(app)
    await report_config(app)
    await register_commands(app)
    restored = restore_sessions(app)
    # 안내보다 먼저 재개해야 "무엇이 이어졌는지"를 담아 알릴 수 있다.
    resumed = await restore_hunts(app)
    await notify_restart(app, restored, resumed)
    # notify_restart 가 상태 파일을 소비했으므로, 복원된 세션 기준으로 다시 남긴다.
    # 이게 없으면 연속 재시작 시 두 번째부터 안내가 나가지 않는다.
    save_state(app)


async def notify_restart(app: Application, restored=frozenset(), resumed=None):
    """재시작을 알려야 할 사람에게만 알린다.

    세션도 헌팅도 전부 이어졌다면 사용자 입장에서는 아무 일도 없었던 것이라
    침묵한다. 재배포가 잦아서 그때마다 알림이 가면 소음이 된다. 실제로 조치가
    필요한 경우(재로그인, 끊긴 헌팅)와 이어서 도는 헌팅이 있을 때만 보낸다.
    관리자는 배포가 제대로 붙었는지 알아야 하므로 요약을 따로 받는다.

    resumed 는 {chat_id: [label, ...]} 형태의 재개된 헌팅 목록이다.
    """
    resumed = resumed or {}
    snapshot = {}
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            snapshot = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("상태 파일 읽기 실패: %s", e)

    previous = {}
    for raw_id, labels in snapshot.items():
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            logger.warning("상태 파일의 chat_id 가 정수가 아니다: %r", raw_id)
            continue
        previous[chat_id] = list(labels or [])
    # 상태 파일 기록이 실패했더라도 되살아난 헌팅은 알린다.
    for chat_id in resumed:
        previous.setdefault(chat_id, [])

    relogin = []
    for chat_id, labels in previous.items():
        back = resumed.get(chat_id) or []
        if chat_id in restored:
            # 헌팅 라벨은 재개 여부와 무관하게 같은 값이라 그대로 대조할 수 있다.
            lost = [lb for lb in labels if lb not in back]
            if not back and not lost:
                continue  # 조용히 이어졌다 — 알릴 사건이 없다
            blocks = []
            if back:
                blocks.append(
                    f"🔄 봇이 재시작됐다. 헌팅 {len(back)}개가 이어서 실행 중이다.\n"
                    + "\n".join(f"· {lb}" for lb in back)
                )
            if lost:
                # 이어진 헌팅이 하나도 없으면 재시작 사실부터 알려야 말이 통한다.
                head = "" if back else "🔄 봇이 재시작됐다. 코레일 세션은 복원됐다.\n\n"
                blocks.append(
                    head + "<b>이어가지 못한 헌팅</b>\n"
                    + "\n".join(f"· {lb}" for lb in lost)
                    + "\n\n/reserve 로 다시 걸어라."
                )
            text = "\n\n".join(blocks)
        else:
            relogin.append(chat_id)
            text = (
                "🔄 봇이 재시작됐다.\n"
                "자격증명을 메모리에만 두는 구조라 로그인 세션이 초기화됐다. "
                "/login 으로 다시 로그인하라."
            )
            if labels:
                text += (
                    "\n\n<b>중단된 헌팅</b>\n"
                    + "\n".join(f"· {lb}" for lb in labels)
                    + "\n\n로그인 후 /reserve 로 다시 걸어야 한다."
                )
        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.warning("재시작 안내 전송 실패 (chat_id=%s): %s", chat_id, e)

    await _notify_admins_restart(app, restored, resumed, relogin)

    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


async def _notify_admins_restart(app: Application, restored, resumed, relogin):
    """관리자에게 재시작 요약을 보낸다.

    직전 실행의 흔적이 아예 없으면(최초 기동 등) 보고할 내용도 없으므로 건너뛴다.
    그게 아니면 재배포가 세션·헌팅을 제대로 넘겼는지 한 줄로 확인할 수 있게 한다.
    """
    hunts = sum(len(v) for v in resumed.values())
    if not (restored or relogin or hunts):
        return

    parts = []
    if restored:
        parts.append(f"세션 {len(restored)}개")
    if hunts:
        parts.append(f"헌팅 {hunts}개")
    summary = "🔄 <b>재시작됨</b> — " + (", ".join(parts) + " 복원" if parts else "복원된 세션 없음")
    if relogin:
        summary += f" / {len(relogin)}명 재로그인 필요"

    for admin_id in admin_chat_ids():
        try:
            await app.bot.send_message(admin_id, summary, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.warning("재시작 요약 전송 실패 (chat_id=%s): %s", admin_id, e)


# ---------------------------------------------------------------------------
# 공용
# ---------------------------------------------------------------------------

async def _korail_call(session: 'Session', fn, *args, **kwargs):
    """한 세션의 코레일 호출을 직렬화. requests.Session 이 thread-safe 하지 않아
    동시 헌팅 N개가 cookie jar 등을 손상시키는 걸 막는다."""
    async with session.lock:
        return await asyncio.to_thread(fn, *args, **kwargs)


async def _ensure_login(session: 'Session'):
    """logined 플래그가 꺼져 있으면 재로그인. 검사+로그인을 같은 lock 안에서
    수행해서 동시에 두 코루틴이 둘 다 login() 을 호출하는 경쟁을 막는다."""
    async with session.lock:
        if not session.korail.logined:
            await asyncio.to_thread(session.korail.login)


# ---------------------------------------------------------------------------
# 기본 명령어
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🚄 <b>korail-hunt</b>\n"
    "매진된 열차의 좌석이 풀리는지 대신 지켜보다가 자동으로 잡아주는 봇이다. "
    "예약은 각자 본인 코레일 계정으로 이뤄진다.\n\n"
    "<b>처음이라면 이 순서대로</b>\n"
    "1. /login — 코레일 아이디와 비밀번호로 로그인한다\n"
    "2. /reserve — 인원 · 날짜 · 시각 · 출발역 · 도착역을 고르면 열차 목록이 나온다\n"
    "3. 좌석이 있으면 바로 예약되고, 매진이면 헌팅(자동 재시도)을 걸 수 있다\n"
    "4. 좌석이 잡히면 알림이 온다\n\n"
    "💳 <b>결제는 봇이 하지 않는다.</b>\n"
    "봇은 좌석을 잡아둘 뿐이다. 예약 후 <b>코레일톡 앱 → 승차권 → 결제대기</b> 에서 "
    "직접 결제해야 하고, 구입기한(보통 10분)을 넘기면 예약은 자동으로 취소된다. "
    "이걸 놓쳐서 좌석을 날리는 경우가 제일 많다.\n\n"
    "<b>명령어</b>\n"
    "/login - 코레일 로그인 (제일 먼저)\n"
    "/reserve - 예약 시작 (인원 → 일시 → 역 → 열차 → 옵션)\n"
    "/reservations - 현재 예약과 진행 중인 헌팅 보기\n"
    "/hunt_stop - 진행 중인 헌팅 중단\n"
    "/logout - 로그아웃 (메모리에서 계정 삭제)\n"
    "/cancel - 진행 중인 입력 취소\n"
    "/start, /help - 이 안내 다시 보기"
)

ADMIN_HELP = (
    "\n\n👑 <b>관리자</b>\n"
    "/users - 승인된 사용자 목록 / 권한 해제"
)

# 프로필에 걸리는 소개문. 대화를 시작하기 전에 사용자가 보는 유일한 설명이라
# 여기서부터 "결제는 직접" 을 못박는다. 텔레그램 제한은 각각 512자 / 120자다.
BOT_DESCRIPTION = (
    "매진된 코레일 열차의 좌석이 풀리는지 대신 지켜보다가 자동으로 예약해주는 봇이다.\n\n"
    "각자 본인 코레일 계정으로 로그인해서 쓴다. 아이디와 비밀번호는 봇 메모리에만 "
    "두고 디스크에 저장하지 않는다.\n\n"
    "결제는 봇이 하지 않는다. 좌석이 잡히면 코레일톡 앱에서 직접 결제해야 하고, "
    "구입기한(보통 10분)을 넘기면 자동으로 취소된다.\n\n"
    "/start 로 시작하라."
)
BOT_SHORT_DESCRIPTION = (
    "매진 열차 좌석을 대신 노려 자동 예약한다. 결제는 코레일톡에서 직접 해야 한다."
)

# 텔레그램에 등록할 명령어 목록. 등록해두면 사용자가 '/' 만 쳐도 자동완성
# 메뉴가 뜬다. 없으면 새 사용자는 명령어를 미리 알고 있어야 한다.
BOT_COMMANDS = [
    BotCommand('login', '코레일 로그인'),
    BotCommand('reserve', '예약 시작'),
    BotCommand('reservations', '현재 예약'),
    BotCommand('hunt_stop', '헌팅 중단'),
    BotCommand('logout', '로그아웃'),
    BotCommand('cancel', '진행 취소'),
    BotCommand('help', '도움말'),
]
ADMIN_COMMANDS = BOT_COMMANDS + [BotCommand('users', '사용자 관리')]


def _help_for(chat_id):
    text = HELP_TEXT
    if handoff_key():
        text += (
            "\n\n🔒 아이디와 비밀번호는 봇 메모리에만 있고 디스크에 저장되지 않는다. "
            "재시작 시 세션은 넘겨지지만, 봇이 오래 멈춰 있었다면 다시 /login 해야 한다."
        )
    else:
        text += (
            "\n\n🔒 아이디와 비밀번호는 봇 메모리에만 있고 디스크에 저장되지 않는다. "
            "봇이 재시작되면 다시 /login 해야 한다."
        )
    if chat_id in admin_chat_ids():
        text += ADMIN_HELP
    return text


async def register_commands(app: Application):
    """'/' 자동완성 메뉴와 프로필 소개문을 채운다.

    관리자에게만 /users 를 추가로 노출한다. 봇 이름과 프로필 사진은 BotFather
    에서만 바꿀 수 있어 여기서 건드리지 않는다.
    """
    # 소개문 등록이 실패해도 명령어 메뉴는 올라가야 하므로 따로 감싼다.
    for setter, value in (
        ('set_my_description', BOT_DESCRIPTION),
        ('set_my_short_description', BOT_SHORT_DESCRIPTION),
    ):
        try:
            await getattr(app.bot, setter)(value)
        except TelegramError as e:
            logger.warning("봇 소개문 등록 실패 (%s): %s", setter, e)

    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except TelegramError as e:
        logger.warning("명령어 목록 등록 실패: %s", e)
        return
    for admin_id in admin_chat_ids():
        try:
            await app.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(admin_id),
            )
        except TelegramError as e:
            logger.warning("관리자 명령어 등록 실패 (chat_id=%s): %s", admin_id, e)


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _help_for(update.effective_chat.id), parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _help_for(update.effective_chat.id), parse_mode=ParseMode.HTML,
    )


@with_session
async def cmd_reservations(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session):
    korail: Korail = session.korail
    chat_id = update.effective_chat.id
    await _ensure_login(session)
    try:
        rsvs = await _korail_call(session, korail.reservations)
    except KorailError as e:
        await update.message.reply_text(
            f"❌ <b>예약 조회 실패</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if not rsvs:
        lines = ["🎫 <b>현재 예약 없음</b>", ""]
    else:
        lines = [f"🎫 <b>현재 예약 {len(rsvs)}건</b>", ""]
        lines += [f"· {html.escape(repr(r))}" for r in rsvs]
        lines += [
            "",
            "💳 결제는 코레일톡 앱 → 승차권 → 결제대기 에서 직접 해야 한다. "
            "구입기한을 넘기면 자동 취소된다.",
            "",
        ]
    await update.message.reply_text(
        "\n".join(lines + [format_status(context, chat_id)]),
        parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_hunt_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = _active_hunts(context, chat_id)
    if not active:
        await update.message.reply_text(
            "진행 중인 헌팅이 없다.\n/reserve 로 새로 걸 수 있다."
        )
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
        f"🔁 헌팅 {len(active)}개가 돌고 있다. 중단할 것을 고르라.",
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
        await query.edit_message_text("진행 중인 헌팅이 없다.")
        return

    if target == "all":
        for entry in active.values():
            entry['task'].cancel()
        await query.edit_message_text(f"⏹ 헌팅 {len(active)}개를 전부 중단한다.")
        return

    entry = active.get(target)
    if entry is None:
        await query.edit_message_text(f"[{target}] 을 찾을 수 없다 (이미 끝났을 수 있다).")
        return
    entry['task'].cancel()
    await query.edit_message_text(f"⏹ [{target}] 중단한다.")


# ---------------------------------------------------------------------------
# 관리자: 접근 승인
# ---------------------------------------------------------------------------

def _drop_user(context, chat_id):
    """세션과 헌팅을 정리한다. 승인 해제·거부 시 즉시 효력을 갖게 한다."""
    stopped = 0
    for entry in list(_active_hunts(context, chat_id).values()):
        entry['task'].cancel()
        stopped += 1
    sessions(context).pop(chat_id, None)
    return stopped


async def cb_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """관리자에게 간 'access:approve|deny:<chat_id>' 버튼 처리."""
    query = update.callback_query
    if update.effective_chat.id not in admin_chat_ids():
        await query.answer("관리자 전용이다.", show_alert=True)
        return
    await query.answer()

    _, action, target = query.data.split(":", 2)
    target = int(target)
    info = context.bot_data.setdefault(KEY_PENDING, {}).pop(target, {})
    data = users(context)
    # 이전에 남아 있던 반대편 기록을 지워서 approved/denied 가 동시에 성립하지 않게.
    info = info or data['approved'].get(target) or data['denied'].get(target) or {}
    data['approved'].pop(target, None)
    data['denied'].pop(target, None)

    if action == 'approve':
        data['approved'][target] = info
        save_users(context)
        await query.edit_message_text(
            f"✅ <b>승인됨</b>\n{_user_label(info, target)}", parse_mode=ParseMode.HTML
        )
        notice = (
            "✅ 접근이 승인됐다. 이제 봇을 쓸 수 있다.\n\n"
            "/login 으로 본인 코레일 계정을 등록한 뒤 /reserve 로 시작하라.\n"
            "예약은 전부 본인 계정으로 이뤄지고, 결제는 코레일톡 앱에서 직접 해야 한다.\n\n"
            "사용법은 /help 에 있다."
        )
    else:
        data['denied'][target] = info
        save_users(context)
        _drop_user(context, target)
        await query.edit_message_text(
            f"⛔ <b>거부됨</b>\n{_user_label(info, target)}", parse_mode=ParseMode.HTML
        )
        notice = "⛔ 접근 요청이 거부됐다. 봇을 쓸 수 없다."

    try:
        await context.bot.send_message(target, notice)
    except TelegramError as e:
        logger.warning("승인 결과 전송 실패 (chat_id=%s): %s", target, e)


@admin_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = users(context)
    approved = data['approved']
    if not approved:
        await update.message.reply_text(
            "승인된 사용자 없음.\n"
            "(관리자와 TELEGRAM_ALLOWED_CHAT_IDS 고정 허용은 이 목록에 포함되지 않는다)"
        )
        return

    live = sessions(context)
    rows, lines = [], [f"<b>승인된 사용자 {len(approved)}명</b>"]
    for chat_id, info in approved.items():
        mark = "🟢" if chat_id in live else "⚪"
        lines.append(f"{mark} {_user_label(info, chat_id)}")
        label = (info.get('name') or str(chat_id))[:24]
        rows.append([InlineKeyboardButton(f"⛔ {label} 해제", callback_data=f"revoke:{chat_id}")])
    lines.append("\n🟢 = 코레일 로그인 상태 · ⚪ = 승인만 된 상태")
    if data['denied']:
        lines.append(f"\n거부 목록 {len(data['denied'])}명")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


async def cb_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/users 의 'revoke:<chat_id>' 버튼. 승인 해제 + 세션·헌팅 즉시 정리."""
    query = update.callback_query
    if update.effective_chat.id not in admin_chat_ids():
        await query.answer("관리자 전용이다.", show_alert=True)
        return
    await query.answer()

    target = int(query.data.split(":", 1)[1])
    info = users(context)['approved'].pop(target, None)
    if info is None:
        await query.edit_message_text("이미 해제된 사용자다.")
        return
    save_users(context)
    stopped = _drop_user(context, target)

    text = f"⛔ <b>승인 해제</b>\n{_user_label(info, target)}"
    if stopped:
        text += f"\n헌팅 {stopped}개 중단, 세션 파기"
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(target, "⛔ 봇 사용 권한이 해제됐다.")
    except TelegramError as e:
        logger.warning("해제 통보 실패 (chat_id=%s): %s", target, e)


# ---------------------------------------------------------------------------
# Conversation: /login 흐름
# ---------------------------------------------------------------------------

@restricted
async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if get_session(context, chat_id) is not None:
        await update.message.reply_text(
            "이미 로그인되어 있다. /reserve 로 바로 예약할 수 있다.\n"
            "다른 계정으로 바꾸려면 /logout 후 다시 /login 하라."
        )
        return ConversationHandler.END
    # 진행 중이던 /reserve 대화의 잔여 상태를 정리한다.
    context.user_data.clear()
    await update.message.reply_text(
        "🔐 <b>코레일 로그인</b>\n\n"
        "코레일 홈페이지·코레일톡에서 쓰는 아이디를 입력하라. 셋 중 아무거나 된다.\n"
        "· 회원번호 8자리 (예: 12345678)\n"
        "· 이메일 (예: hong@example.com)\n"
        "· 휴대폰번호 (예: 010-1234-5678)\n\n"
        "비밀번호는 다음 단계에서 묻는다.\n\n"
        "그만두려면 /cancel",
        parse_mode=ParseMode.HTML,
    )
    return LOGIN_ID


async def login_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.user_data[KEY_LOGIN_ID] = normalize_korail_id(update.message.text)
    await context.bot.send_message(
        chat_id,
        "🔑 이어서 비밀번호를 입력하라.\n\n"
        "입력한 메시지는 봇이 곧바로 삭제한다. 비밀번호는 봇 메모리에만 보관되며 "
        "디스크나 로그에 남지 않는다.\n\n"
        "그만두려면 /cancel",
    )
    return LOGIN_PW


async def login_pw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    korail_pw = update.message.text

    # 무엇보다 먼저 비밀번호 메시지를 대화에서 지운다.
    try:
        await update.message.delete()
    except TelegramError as e:
        logger.warning("비밀번호 메시지 삭제 실패: %s", e)
        await context.bot.send_message(
            chat_id, "⚠️ 비밀번호 메시지를 삭제하지 못했다. 직접 삭제하라."
        )

    korail_id = context.user_data.pop(KEY_LOGIN_ID, None)
    if not korail_id:
        await context.bot.send_message(
            chat_id, "로그인 흐름이 끊겼다. /login 부터 다시 하라."
        )
        return ConversationHandler.END

    notice = await context.bot.send_message(chat_id, "⏳ 코레일에 로그인하는 중...")
    korail = PatchedKorail(korail_id, korail_pw, auto_login=False)
    try:
        ok = await asyncio.to_thread(korail.login)
    except Exception as e:  # 네트워크·파싱 실패 등. 예외 메시지에 비번은 담기지 않는다.
        logger.warning("로그인 예외 (chat_id=%s): %s", chat_id, e)
        await notice.edit_text(
            f"❌ <b>로그인 실패</b>\n<code>{html.escape(str(e))}</code>\n\n"
            "코레일 서버나 네트워크 문제일 수 있다. 잠시 뒤 /login 으로 다시 시도하라.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if not ok:
        await notice.edit_text(
            "❌ <b>로그인 실패</b> — 아이디나 비밀번호가 맞지 않는다.\n\n"
            "아이디는 셋 중 하나여야 한다:\n"
            "· 회원번호 8자리 (예: 12345678)\n"
            "· 이메일 (예: hong@example.com)\n"
            "· 휴대폰번호 (예: 010-1234-5678)\n\n"
            "'잘못 입력하셨습니다(회원번호)' 가 떴다면, 아이디가 위 형식 중 어느 것도 "
            "아니어서 회원번호로 처리된 것이다.\n"
            "코레일톡 앱에서 같은 아이디·비밀번호로 로그인되는지 먼저 확인해 보라.\n\n"
            "다시 하려면 /login",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    sessions(context)[chat_id] = Session(korail)
    save_state(context)
    await notice.edit_text(
        f"✅ <b>로그인 성공</b>\n"
        f"{html.escape(str(korail.name))} ({html.escape(str(korail.membership_number))})\n\n"
        f"/reserve 로 예약을 시작하라.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


@restricted
async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(KEY_LOGIN_ID, None)
    await update.message.reply_text("로그인을 취소했다. 다시 하려면 /login")
    return ConversationHandler.END


@restricted
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if get_session(context, chat_id) is None:
        await update.message.reply_text("로그인 상태가 아니다. /login 으로 로그인하라.")
        return
    stopped = _drop_user(context, chat_id)
    context.user_data.clear()
    save_state(context)
    msg = "👋 로그아웃했다. 메모리에서 계정 정보를 지웠다."
    if stopped:
        msg += f"\n진행 중이던 헌팅 {stopped}개도 함께 중단했다."
    msg += "\n\n이미 잡아둔 예약은 그대로 남아 있다. 결제는 코레일톡 앱에서 하라."
    await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# Conversation: /reserve 흐름
# ---------------------------------------------------------------------------

WEEKDAY_KO = ['월', '화', '수', '목', '금', '토', '일']
DATE_OFFSETS = [0, 1, 2, 3, 4, 5, 6, 7, 14, 21, 28]
DATE_ALIASES = {0: '오늘', 1: '내일', 2: '모레'}

# ReserveOption 은 'GENERAL_FIRST' 같은 코드값이라 그대로 보여주면 안 된다.
# 버튼에 적힌 말과 똑같이 되돌려줘야 뭘 골랐는지 헷갈리지 않는다.
OPTION_LABELS = {
    ReserveOption.GENERAL_FIRST: '일반 우선',
    ReserveOption.SPECIAL_FIRST: '특실 우선',
    ReserveOption.GENERAL_ONLY: '일반만',
    ReserveOption.SPECIAL_ONLY: '특실만',
}

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


def _count_keyboard():
    rows, row = [], []
    for n in range(PASSENGER_MIN, PASSENGER_MAX + 1):
        row.append(InlineKeyboardButton(f"{n}명", callback_data=f"cnt:{n}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("취소", callback_data="cancel")])
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


@with_session
async def conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session):
    context.user_data.clear()
    # 인원을 제일 먼저 받는다. 이후 검색이 곧바로 그 인원 기준으로 좌석을
    # 따져야 해서, 날짜·역보다 앞에 두는 게 흐름이 단순하다.
    await update.message.reply_text(
        "👥 <b>인원</b>을 고르라 (어른).\n언제든 /cancel 로 그만둘 수 있다.",
        reply_markup=_count_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_COUNT


async def conv_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text(CONV_CANCELLED)
        return ConversationHandler.END
    if not query.data.startswith("cnt:"):
        return ASK_COUNT
    adults = clamp_passenger_count(query.data.split(":", 1)[1])
    context.user_data[KEY_ADULTS] = adults
    await query.edit_message_text(
        f"👥 인원: {describe_passengers(build_passengers(adults))}\n\n"
        f"📅 <b>출발일</b>을 고르라.",
        reply_markup=_date_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_DATE


# 취소 문구는 모든 단계에서 같아야 헷갈리지 않는다.
CONV_CANCELLED = "예약을 취소했다. 다시 하려면 /reserve"


async def conv_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text(CONV_CANCELLED)
        return ConversationHandler.END
    if not query.data.startswith("date:"):
        return ASK_DATE
    ymd = query.data.split(":", 1)[1]
    context.user_data[KEY_DATE] = ymd
    await query.edit_message_text(
        f"📅 출발일: <b>{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}</b>\n\n"
        f"🕐 <b>출발 시각</b>을 고르라. 이 시각 이후로 출발하는 열차를 찾는다.",
        reply_markup=_time_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_TIME


async def conv_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text(CONV_CANCELLED)
        return ConversationHandler.END
    if not query.data.startswith("time:"):
        return ASK_TIME
    hhmmss = query.data.split(":", 1)[1]
    context.user_data[KEY_TIME] = hhmmss
    await query.edit_message_text(
        f"🕐 출발 시각: <b>{hhmmss[:2]}:00 이후</b>\n\n"
        f"🚉 <b>출발역</b>을 고르라. 목록에 없으면 '직접 입력' 을 눌러라.",
        reply_markup=_station_keyboard("dep"),
        parse_mode=ParseMode.HTML,
    )
    return ASK_DEP


async def conv_dep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text(CONV_CANCELLED)
        return ConversationHandler.END
    if query.data == "dep:_text":
        await query.edit_message_text("🚉 출발역 이름을 그대로 입력하라. 예: 서울")
        return ASK_DEP
    if not query.data.startswith("dep:"):
        return ASK_DEP
    station = query.data.split(":", 1)[1]
    context.user_data[KEY_DEP] = station
    await query.edit_message_text(
        f"🚉 출발역: <b>{html.escape(station)}</b>\n\n"
        f"🏁 <b>도착역</b>을 고르라.",
        reply_markup=_station_keyboard("arr"),
        parse_mode=ParseMode.HTML,
    )
    return ASK_ARR


async def conv_dep_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[KEY_DEP] = update.message.text.strip()
    await update.message.reply_text(
        f"🚉 출발역: <b>{html.escape(context.user_data[KEY_DEP])}</b>\n\n"
        f"🏁 <b>도착역</b>을 고르라.",
        reply_markup=_station_keyboard("arr"),
        parse_mode=ParseMode.HTML,
    )
    return ASK_ARR


async def conv_arr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text(CONV_CANCELLED)
        return ConversationHandler.END
    if query.data == "arr:_text":
        await query.edit_message_text("🏁 도착역 이름을 그대로 입력하라. 예: 부산")
        return ASK_ARR
    if not query.data.startswith("arr:"):
        return ASK_ARR
    station = query.data.split(":", 1)[1]
    context.user_data[KEY_ARR] = station
    await query.edit_message_text(
        f"🏁 도착역: <b>{html.escape(station)}</b>\n\n🔎 열차를 찾는 중...",
        parse_mode=ParseMode.HTML,
    )
    return await _show_trains(update, context)


async def conv_arr_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[KEY_ARR] = update.message.text.strip()
    return await _show_trains(update, context)


async def _show_trains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """열차 검색 결과를 출력하고 선택 키보드를 제시한다."""
    session = await _session_or_end(update, context)
    if session is None:
        return ConversationHandler.END
    korail: Korail = session.korail
    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]
    # 검색도 인원 기준이어야 한다. 1명으로 검색하고 여러 명으로 예약하면
    # 좌석이 있는 줄 알고 들어갔다가 예약 단계에서 매진으로 튕긴다.
    passengers = passengers_of(context)

    await _ensure_login(session)
    try:
        trains = await _korail_call(
            session, korail.search_train, dep, arr, d, t,
            passengers=passengers, include_no_seats=True,
        )
    except NoResultsError:
        trains = []
    except KorailError as e:
        await update.effective_message.reply_text(
            f"❌ <b>열차 검색 실패</b>\n<code>{html.escape(str(e))}</code>\n\n"
            f"역 이름이 정확한지 확인하고 /reserve 로 다시 시도하라.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    route = f"{html.escape(dep)} → {html.escape(arr)}"
    when = f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]} 이후"

    if not trains:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 헌팅 시작", callback_data="hunt"),
            InlineKeyboardButton("취소", callback_data="cancel"),
        ]])
        await update.effective_message.reply_text(
            f"🔎 <b>{route}</b>\n{when}\n\n"
            f"지금은 잡을 수 있는 열차가 없다 (매진이거나 운행이 없다).\n"
            f"[🔁 헌팅 시작] 을 누르면 좌석이 풀릴 때까지 대신 계속 조회한다.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return SELECT_TRAIN

    context.user_data[KEY_TRAINS] = trains
    text_lines = [f"🔎 <b>{route}</b>", when, ""]
    buttons = []
    for i, tr in enumerate(trains):
        marker = "" if tr.has_seat() else " ❌매진"
        text_lines.append(f"[{i + 1}] {html.escape(repr(tr))}{marker}")
        label = f"#{i + 1} 예약" if tr.has_seat() else f"#{i + 1} 헌팅"
        buttons.append([InlineKeyboardButton(label, callback_data=f"train:{i}")])
    text_lines += [
        "",
        "번호를 누르면 그 열차를 예약한다. 매진된 열차를 고르면 "
        "좌석이 풀릴 때까지 노리는 헌팅이 걸린다.",
    ]
    buttons.append([
        InlineKeyboardButton("🔁 전체 헌팅", callback_data="hunt"),
        InlineKeyboardButton("취소", callback_data="cancel"),
    ])
    await update.effective_message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )
    return SELECT_TRAIN


async def conv_train_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text(CONV_CANCELLED)
        return ConversationHandler.END

    if data == "hunt":
        # query.message.text 는 서식이 벗겨진 평문이라 parse_mode 없이 되돌려준다.
        await query.edit_message_text(query.message.text + "\n\n🔁 헌팅을 시작한다...")
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
            f"🚄 {html.escape(repr(train))}\n\n"
            f"💺 <b>좌석 옵션</b>을 고르라.\n"
            f"'우선' 은 그쪽을 먼저 보되 없으면 다른 쪽도 잡고, "
            f"'만' 은 그 등급만 잡는다.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return SELECT_OPTION

    return SELECT_TRAIN


async def conv_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text(CONV_CANCELLED)
        return ConversationHandler.END
    if not query.data.startswith("opt:"):
        return SELECT_OPTION

    option = getattr(ReserveOption, query.data.split(":", 1)[1])
    session = await _session_or_end(update, context)
    if session is None:
        return ConversationHandler.END
    korail: Korail = session.korail
    train_idx = context.user_data[KEY_SELECTED_TRAIN_IDX]
    train = context.user_data[KEY_TRAINS][train_idx]
    passengers = passengers_of(context)

    # 매진 열차면 예약 시도 건너뛰고 즉시 해당 열차 헌팅 시작.
    if not train.has_seat():
        await query.edit_message_text(
            f"🚄 {html.escape(repr(train))}\n\n"
            f"매진이라 바로 예약할 수 없다. 이 열차만 노리는 헌팅을 건다.",
            parse_mode=ParseMode.HTML,
        )
        await _start_train_hunt(update, context, train_idx, option)
        return ConversationHandler.END

    await query.edit_message_text(
        f"⏳ 예약하는 중...\n{html.escape(repr(train))}", parse_mode=ParseMode.HTML,
    )
    await _ensure_login(session)
    try:
        rsv = await _korail_call(session, korail.reserve, train, passengers, option, False)
    except SoldOutError:
        # 검색 후 예약 직전에 매진 — 같은 열차로 헌팅 fallback.
        await update.effective_message.reply_text(
            f"🚄 {html.escape(repr(train))}\n\n"
            f"예약을 넣기 직전에 매진됐다. 이 열차만 노리는 헌팅을 건다.",
            parse_mode=ParseMode.HTML,
        )
        await _start_train_hunt(update, context, train_idx, option)
        return ConversationHandler.END
    except KorailError as e:
        await update.effective_message.reply_text(
            f"❌ <b>예약 실패</b>\n<code>{html.escape(str(e))}</code>\n\n"
            f"/reserve 로 다시 시도하라.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if rsv is None:
        await update.effective_message.reply_text(
            "⚠️ 예약은 시도했지만 코레일 응답을 해석하지 못했다.\n"
            "/reservations 로 실제로 잡혔는지 확인하라."
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        format_reservation_success(rsv, passengers)
        + "\n\n"
        + format_status(context, update.effective_chat.id),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


@restricted
async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(CONV_CANCELLED)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# 헌팅 백그라운드
# ---------------------------------------------------------------------------
# bot_data[KEY_HUNT_TASKS] = {chat_id: {hunt_id: {'task': Task, 'label': str, 'spec': dict}}}
# hunt_id 는 chat 단위로 'h1', 'h2', ... 자동 발급. 한 chat 에서 동시 다수 헌팅 가능.
# spec 은 재시작 때 헌팅을 그대로 되살리기 위한 직렬화 가능한 조건 묶음이다.

HUNT_ALL = 'all'      # 조건에 맞는 아무 열차나
HUNT_TRAIN = 'train'  # 지정한 열차 한 대만

# ReserveOption 은 enum 이 아니라 문자열 상수 묶음이라 값이 곧 이름이다. 그래서
# JSON 에 그대로 실을 수 있지만, 복원할 때 아는 값인지 확인해야 엉뚱한 문자열이
# 코레일 예약 호출까지 흘러들지 않는다.
RESERVE_OPTIONS = frozenset(
    v for k, v in vars(ReserveOption).items()
    if not k.startswith('_') and isinstance(v, str)
)


def _chat_hunts(context, chat_id):
    return context.bot_data.setdefault(KEY_HUNT_TASKS, {}).setdefault(chat_id, {})


def _next_hunt_id(chat_hunts):
    n = 1
    while f"h{n}" in chat_hunts:
        n += 1
    return f"h{n}"


def _format_hunt_label(dep, arr, d, t, train=None, passengers=None):
    date_str = f"{d[4:6]}/{d[6:]}"
    # 인원이 다른 헌팅을 같은 구간에 여러 개 걸 수 있어서 라벨에 인원을 붙인다.
    psg = f" ({describe_passengers(passengers)})" if passengers else ""
    if train is not None:
        time_str = f"{train.dep_time[:2]}:{train.dep_time[2:4]}"
        return f"[{train.train_type_name} {train.train_no}] {dep}→{arr} {date_str} {time_str}{psg}"
    time_str = f"{t[:2]}:{t[2:4]}"
    return f"전체 {dep}→{arr} {date_str} {time_str}~{psg}"


def _active_hunts(context, chat_id):
    return {hid: e for hid, e in _chat_hunts(context, chat_id).items() if not e['task'].done()}


def _load_adults(raw):
    """예약 인원수. 값이 없거나 깨졌으면 1명으로 떨어진다.

    Passenger 객체를 통째로 직렬화하지 않고 인원수만 싣는다. 객체를 담으면
    핸드오프 포맷이 korail2 내부 구조에 묶이고, 정수 하나면 충분한 정보다.
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return PASSENGER_MIN
    # 레코드가 깨져도 예약 인원이 폭주하지 않게 코레일이 받는 범위로 자른다.
    return clamp_passenger_count(n)


def _hunt_spec(label, dep, arr, d, t, interval, target=None, option=None, adults=1):
    """헌팅을 되살리는 데 필요한 조건만 담은, JSON 으로 오갈 수 있는 dict.

    target 이 있으면 특정 열차 헌팅이다. 복원 쪽이 값의 유무로 추측하지 않도록
    종류를 kind 로 명시해 둔다.
    """
    spec = {
        'kind': HUNT_TRAIN if target is not None else HUNT_ALL,
        'label': label,
        'dep': dep,
        'arr': arr,
        'd': d,
        't': t,
        'interval': interval,
        # 인원을 잃으면 4명짜리 헌팅이 조용히 1명으로 예약된다. 조건의 일부다.
        'adults': _load_adults(adults),
    }
    if target is not None:
        # JSON 은 튜플을 리스트로 바꾼다. 복원할 때 튜플로 되돌려야 열차 대조가 맞는다.
        spec['target'] = list(target)
        spec['option'] = option
    return spec


def _dump_hunts(app):
    """진행 중인 헌팅 조건을 {chat_id: [spec, ...]} 로 모은다.

    조건에는 자격증명이 없지만 세션과 함께 암호화 페이로드로 나간다.
    """
    out = {}
    for chat_id, hunts in (app.bot_data.get(KEY_HUNT_TASKS) or {}).items():
        specs = []
        for hunt_id, entry in hunts.items():
            spec = entry.get('spec')
            # 이미 끝난 헌팅과, 조건을 모르는(구버전) 헌팅은 되살릴 수 없다.
            if not spec or entry['task'].done():
                continue
            specs.append(dict(spec, hunt_id=hunt_id))
        if specs:
            out[str(chat_id)] = specs
    return out


def _load_hunt_spec(raw):
    """핸드오프에서 읽은 헌팅 조건을 검증해 (hunt_id, spec) 으로 돌려준다.

    남이 만든 파일은 아니지만 키 변경·버전 차이로 형태가 어긋날 수 있어서,
    루프에 넘기기 전에 여기서 걸러낸다. 이상하면 예외를 던진다.
    """
    kind = raw.get('kind')
    if kind not in (HUNT_ALL, HUNT_TRAIN):
        raise ValueError(f"알 수 없는 헌팅 종류: {kind!r}")

    spec = {
        'kind': kind,
        'label': str(raw['label']),
        'dep': str(raw['dep']),
        'arr': str(raw['arr']),
        'd': str(raw['d']),
        't': str(raw['t']),
        'interval': float(raw['interval']),
        # 이 필드가 없던 시절의 레코드도 받아야 하므로 없으면 1명으로 본다.
        'adults': _load_adults(raw.get('adults')),
    }
    if kind == HUNT_TRAIN:
        target = raw['target']
        if not isinstance(target, (list, tuple)) or len(target) != 3:
            raise ValueError(f"열차 지정 형식이 잘못됨: {target!r}")
        spec['target'] = tuple(str(x) for x in target)
        option = raw['option']
        if option not in RESERVE_OPTIONS:
            raise ValueError(f"알 수 없는 좌석 옵션: {option!r}")
        spec['option'] = option
    return str(raw['hunt_id']), spec


def _resume_hunt(context, session, chat_id, hunt_id, spec):
    """조건 하나를 실제 task 로 되살린다. 사용자에게 보여줄 label 을 반환한다."""
    chat_hunts = _chat_hunts(context, chat_id)
    # 사용자가 /hunt_stop 에서 보던 번호를 그대로 유지한다. 만에 하나 겹치면
    # 덮어써서 기존 task 를 미아로 만드는 대신 새 번호를 발급한다.
    if hunt_id in chat_hunts:
        hunt_id = _next_hunt_id(chat_hunts)
    label = spec['label']

    # 인원을 Passenger 리스트로 되만들어 넘긴다. 이게 빠지면 4명으로 걸어둔
    # 헌팅이 재시작 후 조용히 1명짜리가 된다.
    passengers = build_passengers(spec['adults'])
    if spec['kind'] == HUNT_TRAIN:
        coro = _train_hunt_loop(
            context, session, chat_id, hunt_id, label, spec['target'],
            spec['dep'], spec['arr'], spec['d'], spec['t'],
            passengers, spec['option'], spec['interval'],
        )
    else:
        coro = _hunt_loop(
            context, session, chat_id, hunt_id, label,
            spec['dep'], spec['arr'], spec['d'], spec['t'],
            passengers, spec['interval'],
        )
    chat_hunts[hunt_id] = {'task': asyncio.create_task(coro), 'label': label, 'spec': spec}
    return label


async def restore_hunts(app: Application):
    """핸드오프로 넘어온 헌팅을 다시 띄운다. {chat_id: [label, ...]} 을 반환한다.

    세션이 복원된 chat 만 대상이다. 세션 없이는 코레일 호출 자체가 불가능하다.
    헌팅 하나가 깨졌다고 기동 전체가 멈추면 안 되므로 개별로 예외를 잡는다.
    """
    pending = app.bot_data.pop(KEY_RESUME_HUNTS, None) or {}
    resumed = {}

    for raw_id, raw_specs in pending.items():
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            logger.warning("헌팅 복원: chat_id 가 정수가 아니다: %r", raw_id)
            continue

        session = get_session(app, chat_id)
        if session is None:
            logger.info(
                "헌팅 %d개 복원 안 함 (chat_id=%s) — 세션이 없어 코레일 호출이 불가능하다",
                len(raw_specs or ()), chat_id,
            )
            continue

        for raw in raw_specs or ():
            try:
                hunt_id, spec = _load_hunt_spec(raw)
                label = _resume_hunt(app, session, chat_id, hunt_id, spec)
            except Exception as e:
                # 하나가 망가져도 나머지는 살린다. 조용히 넘어가면 안 되니 남긴다.
                logger.warning("헌팅 복원 실패 (chat_id=%s): %s", chat_id, e)
                continue
            resumed.setdefault(chat_id, []).append(label)

    if resumed:
        logger.info("헌팅 %d개 재개", sum(len(v) for v in resumed.values()))
    return resumed


async def _start_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = await _session_or_end(update, context)
    if session is None:
        return
    chat_id = update.effective_chat.id
    chat_hunts = _chat_hunts(context, chat_id)

    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]
    adults = clamp_passenger_count(context.user_data.get(KEY_ADULTS, PASSENGER_MIN))
    passengers = build_passengers(adults)
    interval = float(os.environ.get('TELEGRAM_HUNT_INTERVAL', '3'))

    hunt_id = _next_hunt_id(chat_hunts)
    label = _format_hunt_label(dep, arr, d, t, train=None, passengers=passengers)

    # 상태 요약에 방금 건 헌팅까지 들어가도록 등록을 먼저 한다. 등록 자체는
    # 동기라 안내를 보내기 전에 확정된다.
    # 인원은 헌팅이 도는 내내 고정이라 루프 시작 시점의 값을 넘긴다.
    # user_data 를 루프 안에서 다시 읽으면 다음 /reserve 가 값을 덮어쓴다.
    task = asyncio.create_task(
        _hunt_loop(context, session, chat_id, hunt_id, label, dep, arr, d, t,
                   passengers, interval)
    )
    chat_hunts[hunt_id] = {
        'task': task,
        'label': label,
        'spec': _hunt_spec(label, dep, arr, d, t, interval, adults=adults),
    }
    save_state(context)

    await update.effective_message.reply_text(
        f"🔁 <b>헌팅 시작</b> — [<code>{hunt_id}</code>] {html.escape(label)}\n"
        f"조건에 맞는 열차를 {interval:g}초마다 다시 조회한다. "
        f"좌석이 잡히면 바로 알린다. 그동안 다른 일을 봐도 된다.\n\n"
        + format_status(context, chat_id),
        parse_mode=ParseMode.HTML,
    )


async def _hunt_loop(context, session, chat_id, hunt_id, label, dep, arr, d, t,
                     passengers, interval):
    korail: Korail = session.korail
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(session)
                trains = await _korail_call(
                    session, korail.search_train_allday, dep, arr, d, t,
                    passengers=passengers,
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
                    session, korail.reserve, trains[0], passengers,
                    ReserveOption.GENERAL_FIRST, False,
                )
            except SoldOutError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                await bot.send_message(
                    chat_id,
                    f"❌ <b>[{hunt_id}] 헌팅 중 예약 실패</b>\n"
                    f"<code>{html.escape(str(e))}</code>\n\n"
                    f"헌팅을 멈춘다. /reserve 로 다시 걸어라.",
                    parse_mode=ParseMode.HTML,
                )
                return

            if rsv is None:
                await bot.send_message(
                    chat_id,
                    f"⚠️ [{hunt_id}] 예약은 시도했지만 코레일 응답을 해석하지 못했다.\n"
                    f"/reservations 로 실제로 잡혔는지 확인하라.",
                )
                return

            await bot.send_message(
                chat_id,
                f"🎉 <b>헌팅 성공</b> — [{hunt_id}] {html.escape(label)}\n"
                f"{attempts}회 만에 잡았다.\n\n"
                f"{format_reservation_success(rsv, passengers)}\n\n"
                f"{format_status(context, chat_id, exclude=hunt_id)}",
                parse_mode=ParseMode.HTML,
            )
            return
    except asyncio.CancelledError:
        await bot.send_message(
            chat_id, f"⏹ [{hunt_id}] 헌팅을 중단했다 ({attempts}회 시도).",
        )
        raise
    finally:
        _chat_hunts(context, chat_id).pop(hunt_id, None)
        save_state(context)


async def _start_train_hunt(update, context, train_idx, option):
    """특정 열차 한 대만 노린 헌팅. 검색 후 매진 떴을 때 진입."""
    session = await _session_or_end(update, context)
    if session is None:
        return
    chat_id = update.effective_chat.id
    chat_hunts = _chat_hunts(context, chat_id)

    train = context.user_data[KEY_TRAINS][train_idx]
    target = (train.train_no, train.dep_date, train.dep_time)
    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]
    adults = clamp_passenger_count(context.user_data.get(KEY_ADULTS, PASSENGER_MIN))
    passengers = build_passengers(adults)
    interval = float(os.environ.get('TELEGRAM_HUNT_INTERVAL', '3'))

    hunt_id = _next_hunt_id(chat_hunts)
    label = _format_hunt_label(dep, arr, d, t, train=train, passengers=passengers)

    # 상태 요약에 방금 건 헌팅까지 들어가도록 등록을 먼저 한다.
    task = asyncio.create_task(
        _train_hunt_loop(context, session, chat_id, hunt_id, label, target,
                         dep, arr, d, t, passengers, option, interval)
    )
    chat_hunts[hunt_id] = {
        'task': task,
        'label': label,
        'spec': _hunt_spec(label, dep, arr, d, t, interval,
                           target=target, option=option, adults=adults),
    }
    save_state(context)

    await update.effective_message.reply_text(
        f"🔁 <b>헌팅 시작</b> — [<code>{hunt_id}</code>] {html.escape(label)}\n"
        f"이 열차만 {interval:g}초마다 다시 조회한다 "
        f"(좌석 옵션: {html.escape(OPTION_LABELS.get(option, str(option)))}). "
        f"좌석이 풀리면 바로 알린다.\n\n"
        + format_status(context, chat_id),
        parse_mode=ParseMode.HTML,
    )


async def _train_hunt_loop(context, session, chat_id, hunt_id, label, target,
                           dep, arr, d, t, passengers, option, interval):
    korail: Korail = session.korail
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(session)
                trains = await _korail_call(
                    session, korail.search_train, dep, arr, d, t,
                    passengers=passengers, include_no_seats=True,
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
                rsv = await _korail_call(session, korail.reserve, match, passengers, option, False)
            except SoldOutError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                await bot.send_message(
                    chat_id,
                    f"❌ <b>[{hunt_id}] 헌팅 중 예약 실패</b>\n"
                    f"<code>{html.escape(str(e))}</code>\n\n"
                    f"헌팅을 멈춘다. /reserve 로 다시 걸어라.",
                    parse_mode=ParseMode.HTML,
                )
                return

            if rsv is None:
                await bot.send_message(
                    chat_id,
                    f"⚠️ [{hunt_id}] 예약은 시도했지만 코레일 응답을 해석하지 못했다.\n"
                    f"/reservations 로 실제로 잡혔는지 확인하라.",
                )
                return

            await bot.send_message(
                chat_id,
                f"🎉 <b>헌팅 성공</b> — [{hunt_id}] {html.escape(label)}\n"
                f"{attempts}회 만에 잡았다.\n\n"
                f"{format_reservation_success(rsv, passengers)}\n\n"
                f"{format_status(context, chat_id, exclude=hunt_id)}",
                parse_mode=ParseMode.HTML,
            )
            return
    except asyncio.CancelledError:
        await bot.send_message(
            chat_id, f"⏹ [{hunt_id}] 헌팅을 중단했다 ({attempts}회 시도).",
        )
        raise
    finally:
        _chat_hunts(context, chat_id).pop(hunt_id, None)
        save_state(context)


# ---------------------------------------------------------------------------
# 설정 점검
# ---------------------------------------------------------------------------
# .env 는 레포에 없어서 배포 파이프라인이 챙겨주지 못한다. 코드에 새 환경변수를
# 추가해도 서버 .env 는 그대로라, 기능이 에러 없이 꺼진 채로 도는 일이 생긴다.
# 실제로 관리자 승인과 세션 핸드오프가 그렇게 며칠간 비활성 상태였다.
# 여기서 기동 시 점검해 로그와 관리자 메시지로 알린다.

# (환경변수, 없을 때 무슨 일이 벌어지는가)
RECOMMENDED_ENV = [
    ('TELEGRAM_ADMIN_CHAT_IDS',
     '승인 절차가 동작하지 않는다. 새 사용자를 받을 수 없고 /users 도 못 쓴다.'),
    ('SESSION_HANDOFF_KEY',
     '재시작·재배포할 때마다 전원이 다시 /login 해야 한다.'),
]

# Docker 로 띄울 때 .env 에 빈 값으로 남아 있으면 이미지의 /app/data 설정을
# 덮어써서, 볼륨 밖에 파일이 생기고 재배포마다 사라진다.
PATH_ENV = ('BOT_STATE_FILE', 'BOT_USERS_FILE', 'BOT_HANDOFF_FILE')


def _writable(path):
    """해당 경로에 실제로 쓸 수 있는지 확인. 권한 문제를 기동 시 잡아낸다."""
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    probe = os.path.join(directory, '.write_probe')
    try:
        with open(probe, 'w'):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def check_config():
    """설정 문제 목록을 반환한다. 비어 있으면 정상."""
    problems = []

    for name, consequence in RECOMMENDED_ENV:
        if not (os.environ.get(name) or '').strip():
            problems.append(f"{name} 미설정 — {consequence}")

    for name in PATH_ENV:
        value = os.environ.get(name)
        if value is not None and not value.strip():
            problems.append(
                f"{name} 이 빈 값이다 — .env 에서 지우거나 주석 처리하라. "
                f"Docker 사용 시 컨테이너의 /app/data 설정을 덮어써서 "
                f"재배포마다 데이터가 사라진다."
            )

    for label, path in (('상태 파일', STATE_FILE), ('승인 목록', USERS_FILE)):
        if not _writable(path):
            problems.append(
                f"{label} 경로에 쓸 수 없다: {path} — 승인 목록과 재시작 안내가 "
                f"저장되지 않는다. Docker 라면 호스트 data 디렉터리 소유권을 "
                f"확인하라 (컨테이너는 uid 1000 으로 돈다)."
            )

    return problems


async def report_config(app: Application):
    """점검 결과를 로그와 관리자 채팅으로 알린다."""
    problems = check_config()
    if not problems:
        logger.info("설정 점검 통과")
        return

    for p in problems:
        logger.warning("설정 문제: %s", p)

    admins = admin_chat_ids()
    if not admins:
        # 관리자 미설정이 문제 중 하나일 테니 로그로 끝낼 수밖에 없다.
        return
    text = "⚠️ <b>설정 점검</b>\n\n" + "\n\n".join(f"· {html.escape(p)}" for p in problems)
    for admin_id in admins:
        try:
            await app.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.warning("설정 경고 전송 실패 (chat_id=%s): %s", admin_id, e)


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

    if not admin_chat_ids():
        logger.warning(
            "TELEGRAM_ADMIN_CHAT_IDS 가 비어 있다. 승인 절차가 동작하지 않으며, "
            "봇에 메시지를 보내면 본인 chat_id 를 안내한다."
        )

    app = (
        Application.builder()
        .token(token)
        .post_init(on_startup)
        .post_stop(snapshot_on_stop)
        .build()
    )
    app.bot_data[KEY_SESSIONS] = {}
    app.bot_data[KEY_HUNT_TASKS] = {}
    app.bot_data[KEY_PENDING] = {}

    # /login 대화. reserve 대화보다 먼저 등록해야 로그인 중 입력한 텍스트를
    # reserve 쪽 MessageHandler 가 가로채지 않는다.
    login_conv = ConversationHandler(
        entry_points=[CommandHandler('login', cmd_login)],
        states={
            LOGIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_id)],
            LOGIN_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pw)],
        },
        fallbacks=[CommandHandler('cancel', login_cancel)],
    )

    # 콜백 패턴을 명시해서 ConversationHandler 가 자기 화면의 콜백만 잡도록.
    # 그래야 '/hunt_stop' 의 stop:* 콜백이 대화 도중에도 안전히 외부 핸들러로 흐른다.
    # allow_reentry: /login 등으로 흐름이 끊겨도 /reserve 로 항상 새로 시작 가능.
    conv = ConversationHandler(
        allow_reentry=True,
        entry_points=[CommandHandler('reserve', conv_start)],
        states={
            ASK_COUNT: [CallbackQueryHandler(conv_count, pattern=r"^(cnt:|cancel$)")],
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
    app.add_handler(CommandHandler('logout', cmd_logout))
    app.add_handler(CommandHandler('reservations', cmd_reservations))
    app.add_handler(CommandHandler('hunt_stop', cmd_hunt_stop))
    app.add_handler(CommandHandler('users', cmd_users))
    app.add_handler(CallbackQueryHandler(cb_hunt_stop, pattern=r"^stop:"))
    app.add_handler(CallbackQueryHandler(cb_access, pattern=r"^access:(approve|deny):-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_revoke, pattern=r"^revoke:-?\d+$"))
    app.add_handler(login_conv)
    app.add_handler(conv)

    logger.info(
        "봇 시작 (관리자=%s, 고정 허용=%s)",
        sorted(admin_chat_ids()), sorted(allowed_chat_ids()),
    )
    app.run_polling()


if __name__ == '__main__':
    main()
