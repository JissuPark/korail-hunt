"""
korail-hunt Telegram 봇 (멀티 유저).

각 Telegram 사용자가 본인 코레일 자격증명으로 로그인하고, 본인의 헌팅
리스트를 독립적으로 관리한다.

흐름:
  /login          → 코레일 ID/PW 입력 (저장됨, 봇 재시작 해도 유지)
  /reserve        → 출발일 → 출발시각 → 출발역 → 도착역 → 열차 선택 → 좌석옵션 → 예약
  /reservations   → 본인 예약 목록
  /hunts          → 진행 중인 본인 헌팅
  /hunt_stop      → 본인 헌팅 중단
  /logout         → 자격증명 삭제 + 모든 헌팅 중단
  /whoami         → 현재 로그인 정보
  /cancel         → 대화 취소
  /help           → 도움말

실행:
  pip install -e ".[bot]"
  .env 설정 (TELEGRAM_BOT_TOKEN, TELEGRAM_AUTHORIZED_CHAT_IDS, BOT_STORAGE_KEY)
  python bot.py
"""
import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import wraps
from typing import Optional


def escape_html(s):
    return html.escape(str(s))

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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
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
    KorailError,
    NoResultsError,
    PatchedKorail,
    ReserveOption,
    SoldOutError,
)
from korail2.storage import EncryptedStorage, StorageKeyError, generate_key

logger = logging.getLogger(__name__)

# ConversationHandler states — /reserve
ASK_DATE, ASK_TIME, ASK_DEP, ASK_ARR, SELECT_TRAIN, SELECT_OPTION = range(6)
# ConversationHandler states — /login (다른 conv 와 안 겹치게 100번대)
LOGIN_ASK_ID, LOGIN_ASK_PW = range(100, 102)

# context.user_data 키
KEY_DATE = 'date'
KEY_TIME = 'time'
KEY_DEP = 'dep'
KEY_ARR = 'arr'
KEY_TRAINS = 'trains'
KEY_SELECTED_TRAIN_IDX = 'sel'
KEY_LOGIN_ID = 'login_id'

# context.bot_data 키
KEY_SESSIONS = 'sessions'          # chat_id -> UserSession
KEY_STORAGE = 'storage'            # EncryptedStorage
KEY_HUNT_TASKS = 'hunt_tasks'      # chat_id -> {hunt_id -> {task,label}}


# ---------------------------------------------------------------------------
# 유저 세션
# ---------------------------------------------------------------------------

@dataclass
class UserSession:
    """한 Telegram 사용자에 대한 코레일 세션. lock 은 본 인스턴스 호출 직렬화."""
    chat_id: int
    korail: PatchedKorail
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _get_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """현재 메모리에 있는 세션 반환. 없으면 storage 에서 만들어 캐싱.
    저장된 device_info 가 있으면 함께 적용한다."""
    sessions = context.bot_data.setdefault(KEY_SESSIONS, {})
    if chat_id in sessions:
        return sessions[chat_id]
    storage: EncryptedStorage = context.bot_data[KEY_STORAGE]
    creds = storage.get_user(chat_id)
    if creds is None:
        return None
    korail = PatchedKorail(creds['korail_id'], creds['korail_pw'], auto_login=False)
    session = UserSession(chat_id=chat_id, korail=korail)
    _apply_device_info(session, creds.get('device_info'))
    sessions[chat_id] = session
    return session


async def _korail_call(session: UserSession, fn, *args, **kwargs):
    """세션 단위 lock 으로 직렬화. requests.Session 동시 접근 방지."""
    async with session.lock:
        return await asyncio.to_thread(fn, *args, **kwargs)


async def _ensure_login(session: UserSession):
    async with session.lock:
        if not session.korail.logined:
            await asyncio.to_thread(session.korail.login)


def _apply_device_info(session: UserSession, device_info: dict):
    """저장된 device_info 를 Korail 세션에 적용. UA 만 덮어쓴다.

    device_code 는 의도적으로 무시한다 — PatchedKorail 의 DynaPath 우회 토큰이
    Android('AD') 에 고정돼 있어, 여기서 'iOS' 등으로 바꾸면 서버가 토큰과 device
    불일치를 잡아 ERR299907 ('사용 불가한 창구/device') 로 거부한다.
    """
    if not device_info:
        return
    ua = device_info.get('user_agent')
    if ua:
        session.korail._session.headers['User-Agent'] = ua


def _webapp_keyboard(label="📱 기기 정보 자동 감지"):
    """기기 감지 WebApp 버튼이 달린 reply keyboard. URL 미설정 시 None."""
    url = os.environ.get('TELEGRAM_WEBAPP_URL')
    if not url:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=label, web_app=WebAppInfo(url=url))]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ---------------------------------------------------------------------------
# 파서 / 포매터 (변경 없음)
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


# ---------------------------------------------------------------------------
# Device 정보 파서 (Telegram WebApp 에서 받아오는 UA 처리)
# ---------------------------------------------------------------------------

DEVICE_ANDROID = 'AD'
DEVICE_IOS = 'iOS'

# Linux; (U;) Android <ver>; <model>(; Build/<build>)?(; wv)? )
_ANDROID_UA_RE = re.compile(
    r'Linux;\s*(?:U;\s*)?Android\s+(?P<android>[\d.]+)[;\s]\s*(?P<model>[^;)]+?)'
    r'(?:\s+Build/(?P<build>[^;)]+))?(?:\s*;\s*wv)?\s*\)',
)

# 안드로이드 메이저 버전별 합리적 default build 문자열 (UA 에 Build/ 가 없을 때 채워넣음)
_DEFAULT_ANDROID_BUILDS = {
    '15': 'AP3A.240905.015.A2',
    '14': 'UP1A.231005.007',
    '13': 'TP1A.220624.014',
    '12': 'SP1A.210812.016',
    '11': 'RP1A.200720.011',
    '10': 'QP1A.190711.020',
}


@dataclass(frozen=True)
class DeviceInfo:
    """Korail 세션에 적용할 device 설정. dalvik_ua=None 이면 기본값 유지."""
    platform: str                # 'android' / 'ios' / 'unknown'
    dalvik_ua: Optional[str]
    device_code: str             # 'AD' 또는 'iOS'

    @property
    def is_usable(self):
        """Korail 에 실제로 적용할 만한 정보가 있는지."""
        return self.dalvik_ua is not None or self.device_code == DEVICE_IOS


def parse_device_info(webview_ua: str, platform: str = '') -> DeviceInfo:
    """Telegram WebApp 의 navigator.userAgent + Telegram.WebApp.platform 을
    Korail 호환 device 정보로 변환한다.

    Android → UA 에서 모델/버전 추출해서 Dalvik UA 재조립.
    iOS → device='iOS' 만 세팅, UA 는 기본값 유지 권장.
    그 외(desktop/web) → unknown, 호출자가 default UA 사용.
    """
    platform = (platform or '').lower()
    ua = webview_ua or ''
    ua_lower = ua.lower()

    if platform == 'ios' or 'iphone' in ua_lower or 'ipad' in ua_lower:
        return DeviceInfo(platform='ios', dalvik_ua=None, device_code=DEVICE_IOS)

    if platform in ('android', 'android_x') or 'android' in ua_lower:
        m = _ANDROID_UA_RE.search(ua)
        if not m:
            return DeviceInfo(platform='android', dalvik_ua=None, device_code=DEVICE_ANDROID)
        android_ver = m.group('android')
        model = m.group('model').strip()
        major = android_ver.split('.')[0]
        build = m.group('build') or _DEFAULT_ANDROID_BUILDS.get(major, 'UP1A.231005.007')
        dalvik = f"Dalvik/2.1.0 (Linux; U; Android {android_ver}; {model} Build/{build})"
        return DeviceInfo(platform='android', dalvik_ua=dalvik, device_code=DEVICE_ANDROID)

    return DeviceInfo(platform='unknown', dalvik_ua=None, device_code=DEVICE_ANDROID)


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
# 인증 (chat_id 화이트리스트)
# ---------------------------------------------------------------------------

def authorized_chat_ids():
    """허용된 chat_id 집합 반환. 비어 있으면 빈 set (= 아무도 허용 안 함)."""
    raw = os.environ.get('TELEGRAM_AUTHORIZED_CHAT_IDS') or os.environ.get('TELEGRAM_AUTHORIZED_CHAT_ID') or ''
    out = set()
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            logger.warning("TELEGRAM_AUTHORIZED_CHAT_IDS 에 정수가 아닌 값: %r", part)
    return out


def restricted(func):
    """허용된 chat_id 에서 온 업데이트만 처리한다."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        allowed = authorized_chat_ids()
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not allowed:
            # 미설정 시 본인 chat_id 안내
            await update.effective_message.reply_text(
                "TELEGRAM_AUTHORIZED_CHAT_IDS 가 설정되어 있지 않다.\n"
                f"본인 chat_id 를 .env 에 추가한 뒤 봇 재시작: <code>{chat_id}</code>",
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END
        if chat_id not in allowed:
            logger.warning("인증 실패: %s (허용: %s)", chat_id, allowed)
            await update.effective_message.reply_text("권한 없음")
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper


def needs_login(func):
    """세션 없으면 /login 안내. 핸들러는 session 인자를 추가로 받는다."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = update.effective_chat.id
        session = _get_session(context, chat_id)
        if session is None:
            await update.effective_message.reply_text(
                "먼저 /login 으로 코레일 자격증명을 등록하라."
            )
            return ConversationHandler.END
        return await func(update, context, session, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 기본 명령어
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "korail-hunt 봇 (멀티 유저)\n"
    "\n"
    "<b>자격증명</b>\n"
    "/login - 코레일 ID/PW 등록\n"
    "/logout - 자격증명 삭제 + 모든 헌팅 중단\n"
    "/whoami - 현재 로그인 정보\n"
    "\n"
    "<b>예약</b>\n"
    "/reserve - 예약 시작\n"
    "/reservations - 현재 예약\n"
    "\n"
    "<b>헌팅</b>\n"
    "/hunts - 진행 중인 헌팅\n"
    "/hunt_stop - 헌팅 중단\n"
    "\n"
    "<b>기기 정보 (anti-bot 우회)</b>\n"
    "/setdevice - 본인 휴대폰 UA 자동 감지 (WebApp)\n"
    "/cleardevice - 저장된 기기 정보 삭제\n"
    "\n"
    "/cancel - 진행 중인 대화 취소\n"
    "/help - 도움말"
)


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


@restricted
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = _get_session(context, chat_id)
    if session is None:
        await update.message.reply_text(
            f"chat_id: <code>{chat_id}</code>\n로그인 안 됨. /login 으로 등록.",
            parse_mode=ParseMode.HTML,
        )
        return
    k = session.korail
    if not k.logined:
        await update.message.reply_text(
            f"chat_id: <code>{chat_id}</code>\n"
            f"코레일 ID: <code>{k.korail_id}</code>\n"
            f"로그인 상태: 미로그인 (다음 호출에서 자동 로그인)",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.message.reply_text(
        f"chat_id: <code>{chat_id}</code>\n"
        f"코레일 ID: <code>{k.korail_id}</code>\n"
        f"이름: {k.name}\n"
        f"회원번호: {k.membership_number}",
        parse_mode=ParseMode.HTML,
    )


@restricted
@needs_login
async def cmd_reservations(update: Update, context: ContextTypes.DEFAULT_TYPE, session: UserSession):
    await _ensure_login(session)
    try:
        rsvs = await _korail_call(session, session.korail.reservations)
    except KorailError as e:
        await update.message.reply_text(f"조회 실패: {e}")
        return
    if not rsvs:
        await update.message.reply_text("예약 없음")
        return
    text = "현재 예약:\n" + "\n".join(repr(r) for r in rsvs)
    await update.message.reply_text(text)


@restricted
async def cmd_hunts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = _active_hunts(context, chat_id)
    if not active:
        await update.message.reply_text("진행 중인 헌팅 없음")
        return
    lines = [f"진행 중인 헌팅 ({len(active)}개):"]
    for hid, entry in active.items():
        lines.append(f"  [{hid}] {entry['label']}")
    lines.append("\n/hunt_stop 으로 중단")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /login ConversationHandler
# ---------------------------------------------------------------------------

@restricted
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    existing = _get_session(context, chat_id)
    if existing:
        await update.message.reply_text(
            f"이미 로그인 됨: {existing.korail.korail_id}\n"
            "다른 계정으로 바꾸려면 /logout 후 다시 /login.",
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "코레일 ID 입력 (회원번호 8자리 / 이메일 / 010-XXXX-XXXX).\n"
        "취소: /cancel"
    )
    return LOGIN_ASK_ID


async def login_ask_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    korail_id = update.message.text.strip()
    if not korail_id or korail_id.startswith('/'):
        await update.message.reply_text(
            "올바른 ID 가 아니다. 회원번호(8자리) / 이메일 / 010-XXXX-XXXX 중 하나를 입력하라.\n"
            "취소: /cancel"
        )
        return LOGIN_ASK_ID
    context.user_data[KEY_LOGIN_ID] = korail_id
    # ID 는 비밀이 아니라 굳이 삭제하지 않는다. 삭제하면 대화 흐름이
    # 봇 혼자 진행하는 것처럼 보여 사용자가 혼란스러워한다.
    await update.message.reply_text(
        f"ID 받음: <code>{korail_id}</code>\n\n"
        f"비밀번호 입력 (보안상 다음 메시지는 즉시 삭제됨).\n"
        f"취소: /cancel",
        parse_mode=ParseMode.HTML,
    )
    return LOGIN_ASK_PW


async def login_ask_pw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    korail_pw = update.message.text
    if korail_pw.startswith('/'):
        # 명령어를 비번으로 받지 않도록 방어
        await update.message.reply_text("비밀번호 입력 단계. /cancel 로 취소, 아니면 비밀번호 입력.")
        return LOGIN_ASK_PW
    # 비번 메시지 즉시 삭제 (best-effort; private chat 에서는 가능)
    try:
        await update.message.delete()
    except Exception:
        logger.warning("비번 메시지 삭제 실패 — 사용자가 수동으로 지워야 함")

    korail_id = context.user_data.pop(KEY_LOGIN_ID, None)
    if not korail_id:
        await update.effective_message.reply_text("세션 만료. /login 다시 시도.")
        return ConversationHandler.END

    await update.effective_message.reply_text("코레일 로그인 시도 중...")

    korail = PatchedKorail(korail_id, korail_pw, auto_login=False)
    try:
        ok = await asyncio.to_thread(korail.login)
    except Exception as e:
        logger.exception("로그인 중 예외")
        await update.effective_message.reply_text(f"❌ 로그인 중 오류: {e}")
        return ConversationHandler.END

    if not ok:
        await update.effective_message.reply_text(
            "❌ 로그인 실패. 자격증명 또는 클라이언트 버전(.env 의 KORAIL_LOGIN_VERSION) 확인."
        )
        return ConversationHandler.END

    # 저장 + 세션 캐시
    storage: EncryptedStorage = context.bot_data[KEY_STORAGE]
    storage.set_user(update.effective_chat.id, korail_id, korail_pw)
    session = UserSession(chat_id=update.effective_chat.id, korail=korail)
    context.bot_data.setdefault(KEY_SESSIONS, {})[update.effective_chat.id] = session

    kb = _webapp_keyboard()
    extra = (
        "\n\n💡 매크로 감지 우회 정확도를 높이려면 아래 [기기 정보 자동 감지] 버튼을 한 번 눌러라. "
        "본인 휴대폰의 User-Agent 를 그대로 사용한다."
        if kb else ""
    )
    await update.effective_message.reply_text(
        f"✅ 로그인 성공: {korail.name} ({korail.membership_number})\n"
        f"자격증명이 암호화되어 저장됐다. /reserve 로 시작."
        f"{extra}",
        reply_markup=kb,
    )
    return ConversationHandler.END


@restricted
async def cmd_setdevice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """저장된 기기 정보를 갱신하거나 새로 받는다."""
    chat_id = update.effective_chat.id
    if _get_session(context, chat_id) is None:
        await update.message.reply_text("먼저 /login 하라.")
        return
    kb = _webapp_keyboard()
    if kb is None:
        await update.message.reply_text(
            "TELEGRAM_WEBAPP_URL 환경변수가 설정되어 있지 않다. "
            "관리자가 봇 .env 에 WebApp 페이지 URL 을 추가해야 한다."
        )
        return
    storage: EncryptedStorage = context.bot_data[KEY_STORAGE]
    current = (storage.get_user(chat_id) or {}).get('device_info')
    current_str = (
        f"현재 저장: {current['platform']} / {current['device_code']} / "
        f"<code>{current['user_agent'] or '(기본 UA)'}</code>"
        if current else "현재 저장된 기기 정보 없음 (env 기본값 사용 중)"
    )
    await update.message.reply_text(
        f"{current_str}\n\n"
        f"아래 버튼을 누르면 본인 휴대폰 정보를 다시 받아온다.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_cleardevice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """저장된 기기 정보를 지우고 env 기본값으로 돌아간다."""
    chat_id = update.effective_chat.id
    session = _get_session(context, chat_id)
    if session is None:
        await update.message.reply_text("먼저 /login 하라.")
        return
    storage: EncryptedStorage = context.bot_data[KEY_STORAGE]
    try:
        storage.set_user_device(chat_id)  # 다 None → 클리어
    except KeyError:
        pass
    # 메모리 세션도 갱신 — 다음 세션 생성 시 env 기본값으로
    context.bot_data.get(KEY_SESSIONS, {}).pop(chat_id, None)
    await update.message.reply_text(
        "기기 정보 삭제됨. 다음 호출부터 env 기본 UA 사용. /setdevice 로 다시 등록 가능.",
        reply_markup=ReplyKeyboardRemove(),
    )


@restricted
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """WebApp 에서 sendData() 로 보낸 device 정보를 받는다."""
    chat_id = update.effective_chat.id
    session = _get_session(context, chat_id)
    if session is None:
        await update.message.reply_text(
            "먼저 /login 하라.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    raw = update.effective_message.web_app_data.data
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        await update.message.reply_text(f"WebApp 데이터 파싱 실패: {raw[:80]}")
        return

    if payload.get('type') != 'device_info':
        await update.message.reply_text(f"알 수 없는 WebApp 메시지 타입: {payload.get('type')!r}")
        return

    info = parse_device_info(payload.get('user_agent', ''), payload.get('platform', ''))

    # iOS / 데스크톱 / Android 파싱 실패: 적용 가능한 Dalvik UA 가 없다.
    # DynaPath 우회가 Android 전용이라 iOS 의 device_code 변경은 역효과 (ERR299907).
    if not info.dalvik_ua:
        await update.message.reply_text(
            f"감지 결과: <b>{info.platform}</b>\n\n"
            f"이 플랫폼에서는 적용할 UA 를 만들 수 없다 (DynaPath 우회는 Android 전용). "
            f"기본 UA 그대로 사용하라.\n"
            f"받은 UA: <code>{escape_html(payload.get('user_agent', '')[:200])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # 메모리 세션에 즉시 적용 (UA 만)
    _apply_device_info(session, {
        'user_agent': info.dalvik_ua,
        'device_code': info.device_code,  # 저장은 하지만 _apply 가 무시함
        'platform': info.platform,
    })
    # 저장
    storage: EncryptedStorage = context.bot_data[KEY_STORAGE]
    storage.set_user_device(
        chat_id,
        user_agent=info.dalvik_ua,
        device_code=info.device_code,
        platform=info.platform,
    )
    # 세션 재로그인 강제 (UA 가 바뀌었으니)
    session.korail.logined = False

    await update.message.reply_text(
        f"✅ 기기 정보 저장됨\n"
        f"플랫폼: <b>{info.platform}</b>\n"
        f"UA: <code>{escape_html(info.dalvik_ua)}</code>\n\n"
        f"다음 코레일 호출부터 적용된다.\n"
        f"(Device 필드는 DynaPath 호환을 위해 'AD' 로 유지됩니다.)",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )


@restricted
async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(KEY_LOGIN_ID, None)
    await update.message.reply_text("로그인 취소")
    return ConversationHandler.END


@restricted
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # 헌팅 모두 중단
    active = _active_hunts(context, chat_id)
    for entry in active.values():
        entry['task'].cancel()
    # 메모리 세션 제거
    context.bot_data.get(KEY_SESSIONS, {}).pop(chat_id, None)
    # 저장소에서 자격증명 제거
    storage: EncryptedStorage = context.bot_data[KEY_STORAGE]
    removed = storage.delete_user(chat_id)
    msg = "로그아웃 완료. 자격증명 삭제." if removed else "로그아웃 (저장된 자격증명 없음)."
    if active:
        msg += f"\n진행 중이던 헌팅 {len(active)}개 중단 요청."
    await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# /hunt_stop
# ---------------------------------------------------------------------------

@restricted
async def cmd_hunt_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = _active_hunts(context, chat_id)
    if not active:
        await update.message.reply_text("진행 중인 헌팅 없음")
        return

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
# /reserve ConversationHandler
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
    chat_id = update.effective_chat.id
    if _get_session(context, chat_id) is None:
        await update.message.reply_text("먼저 /login 하라.")
        return ConversationHandler.END
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
    session = _get_session(context, update.effective_chat.id)
    if session is None:
        await update.effective_message.reply_text("로그인 만료. /login 다시 하라.")
        return ConversationHandler.END

    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]

    await _ensure_login(session)
    try:
        trains = await _korail_call(
            session, session.korail.search_train, dep, arr, d, t, include_no_seats=True,
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

    session = _get_session(context, update.effective_chat.id)
    if session is None:
        await query.edit_message_text("로그인 만료. /login 다시 하라.")
        return ConversationHandler.END

    option = getattr(ReserveOption, query.data.split(":", 1)[1])
    train_idx = context.user_data[KEY_SELECTED_TRAIN_IDX]
    train = context.user_data[KEY_TRAINS][train_idx]

    if not train.has_seat():
        await query.edit_message_text(f"{train!r}\n좌석 없음 — 이 열차 헌팅 시작")
        await _start_train_hunt(update, context, train_idx, option)
        return ConversationHandler.END

    await query.edit_message_text(f"예약 중... {train!r}")
    await _ensure_login(session)
    try:
        rsv = await _korail_call(session, session.korail.reserve, train, None, option, False)
    except SoldOutError:
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
    session = _get_session(context, chat_id)
    if session is None:
        await update.effective_message.reply_text("로그인 만료. /login 다시.")
        return
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
        _hunt_loop(context, session, chat_id, hunt_id, label, dep, arr, d, t, interval)
    )
    chat_hunts[hunt_id] = {'task': task, 'label': label}


async def _hunt_loop(context, session, chat_id, hunt_id, label, dep, arr, d, t, interval):
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(session)
                trains = await _korail_call(
                    session, session.korail.search_train_allday, dep, arr, d, t,
                )
            except NoResultsError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                logger.warning("hunt[%s] search: %s", hunt_id, e)
                session.korail.logined = False
                await asyncio.sleep(interval)
                continue
            except Exception:
                logger.exception("hunt[%s] unexpected", hunt_id)
                await asyncio.sleep(interval)
                continue

            try:
                rsv = await _korail_call(
                    session, session.korail.reserve, trains[0], None, ReserveOption.GENERAL_FIRST, False,
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
    chat_id = update.effective_chat.id
    session = _get_session(context, chat_id)
    if session is None:
        await update.effective_message.reply_text("로그인 만료. /login 다시.")
        return
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
        _train_hunt_loop(context, session, chat_id, hunt_id, label, target, dep, arr, d, t, option, interval)
    )
    chat_hunts[hunt_id] = {'task': task, 'label': label}


async def _train_hunt_loop(context, session, chat_id, hunt_id, label, target, dep, arr, d, t, option, interval):
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(session)
                trains = await _korail_call(
                    session, session.korail.search_train, dep, arr, d, t, include_no_seats=True,
                )
            except NoResultsError:
                await asyncio.sleep(interval)
                continue
            except KorailError as e:
                logger.warning("train hunt[%s] search: %s", hunt_id, e)
                session.korail.logined = False
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
                rsv = await _korail_call(session, session.korail.reserve, match, None, option, False)
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

def _init_storage():
    key = os.environ.get('BOT_STORAGE_KEY')
    if not key:
        suggested = generate_key()
        raise SystemExit(
            f"BOT_STORAGE_KEY 환경변수가 필요하다. .env 에 다음 줄 추가:\n"
            f"BOT_STORAGE_KEY={suggested}\n"
            f"이 키가 바뀌면 저장된 자격증명을 복호화할 수 없으니 분실 주의."
        )
    path = os.environ.get('BOT_STORAGE_PATH', 'bot_storage.enc')
    try:
        return EncryptedStorage(path, key)
    except StorageKeyError as e:
        raise SystemExit(str(e))


def _setup_logging():
    """콘솔 + (BOT_LOG_FILE 설정 시) 파일 로깅. pythonw 백그라운드 실행 시
    stderr 가 사라져도 파일로는 남게 한다."""
    handlers = [logging.StreamHandler()]
    log_file = os.environ.get('BOT_LOG_FILE')
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode='a', encoding='utf-8'))
    logging.basicConfig(
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        level=logging.INFO,
        handlers=handlers,
    )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)


def main():
    _setup_logging()
    try:
        _main_body()
    except SystemExit as e:
        # pythonw 백그라운드 실행 시 stderr 가 버려져서 SystemExit 메시지가
        # 사라진다. 로거를 거치게 해 bot.log 에 반드시 남게 한다.
        code = e.code if isinstance(e.code, str) else str(e.code or '')
        if code and code != '0':
            logger.error("봇 종료: %s", code)
        raise
    except Exception:
        logger.exception("봇 비정상 종료")
        raise


def _main_body():
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수가 필요하다 (.env 확인)")

    allowed = authorized_chat_ids()
    if not allowed:
        logger.warning(
            "TELEGRAM_AUTHORIZED_CHAT_IDS 가 비어 있다 — 아무도 봇을 쓸 수 없다. "
            "첫 사용자가 메시지를 보내면 본인 chat_id 가 안내된다."
        )

    storage = _init_storage()

    app = Application.builder().token(token).build()
    app.bot_data[KEY_STORAGE] = storage
    app.bot_data[KEY_SESSIONS] = {}
    app.bot_data[KEY_HUNT_TASKS] = {}

    # /login conv. 텍스트 입력 위주. allow_reentry 로 사용자가 도중에 /login
    # 다시 쳐도 깔끔하게 재시작.
    login_conv = ConversationHandler(
        entry_points=[CommandHandler('login', login_start)],
        states={
            LOGIN_ASK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_ask_id)],
            LOGIN_ASK_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_ask_pw)],
        },
        fallbacks=[CommandHandler('cancel', login_cancel)],
        allow_reentry=True,
    )

    # /reserve conv. 콜백 위주. allow_reentry 로 /reserve 다시 치면 재시작.
    reserve_conv = ConversationHandler(
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
        allow_reentry=True,
    )

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(CommandHandler('whoami', cmd_whoami))
    app.add_handler(CommandHandler('logout', cmd_logout))
    app.add_handler(CommandHandler('reservations', cmd_reservations))
    app.add_handler(CommandHandler('hunts', cmd_hunts))
    app.add_handler(CommandHandler('hunt_stop', cmd_hunt_stop))
    app.add_handler(CommandHandler('setdevice', cmd_setdevice))
    app.add_handler(CommandHandler('cleardevice', cmd_cleardevice))
    app.add_handler(CallbackQueryHandler(cb_hunt_stop, pattern=r"^stop:"))
    # WebApp 에서 sendData 로 들어오는 메시지
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(login_conv)
    app.add_handler(reserve_conv)

    logger.info("봇 시작 (인증 chat_ids=%s, 저장된 사용자 %d명)", allowed, len(storage.list_user_ids()))
    app.run_polling()


if __name__ == '__main__':
    main()
