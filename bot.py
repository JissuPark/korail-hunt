"""
korail-hunt Telegram 봇.

chat 별로 각자의 코레일 계정을 쓴다. 자격증명은 **메모리에만** 보관하며 디스크에
쓰지 않는다. 따라서 봇을 재시작하면 모든 세션이 날아가고 각자 /login 을 다시 해야
한다. 재시작 시에는 마지막으로 로그인/헌팅 중이던 chat 에게 안내를 보낸다.

흐름:
  /login    → 코레일 아이디 → 비밀번호 (입력 즉시 메시지 삭제) → 세션 생성
  /reserve  → 출발일 → 출발시각 → 출발역 → 도착역 → 열차 선택 → 좌석옵션 → 예약
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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    Korail,
    KorailError,
    NoResultsError,
    PatchedKorail,
    ReserveOption,
    SoldOutError,
)

logger = logging.getLogger(__name__)

# ConversationHandler states — 두 대화가 독립이라 값이 겹치지 않게 분리한다.
ASK_DATE, ASK_TIME, ASK_DEP, ASK_ARR, SELECT_TRAIN, SELECT_OPTION = range(6)
LOGIN_ID, LOGIN_PW = range(100, 102)

# context.user_data 키
KEY_DATE = 'date'
KEY_TIME = 'time'
KEY_DEP = 'dep'
KEY_ARR = 'arr'
KEY_TRAINS = 'trains'
KEY_SELECTED_TRAIN_IDX = 'sel'
KEY_LOGIN_ID = 'login_id'

# context.bot_data 키
KEY_SESSIONS = 'sessions'  # chat_id -> Session (메모리 전용, 절대 영속화 금지)
KEY_HUNT_TASKS = 'hunt_tasks'  # chat_id -> {hunt_id: {'task', 'label'}}
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
            "관리자가 설정되어 있지 않다.\n"
            f"이 chat_id 를 .env 의 TELEGRAM_ADMIN_CHAT_IDS 에 적고 "
            f"봇을 재시작하라: <code>{chat_id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if chat_id in users(context)['denied']:
        await update.effective_message.reply_text("접근이 거부된 계정이다.")
        return

    pending = context.bot_data.setdefault(KEY_PENDING, {})
    if chat_id in pending:
        await update.effective_message.reply_text(
            "이미 승인 요청이 접수됐다. 관리자 승인을 기다려라."
        )
        return

    user = update.effective_user
    pending[chat_id] = _user_info(user)
    logger.info("접근 요청: chat_id=%s", chat_id)

    await update.effective_message.reply_text(
        "이 봇은 승인된 사용자만 쓸 수 있다.\n"
        "관리자에게 승인 요청을 보냈다. 승인되면 알림이 온다."
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
                "코레일 로그인이 필요하다. /login 을 먼저 실행하라."
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
            "세션이 사라졌다 (로그아웃 또는 봇 재시작). /login 후 다시 시도하라."
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
    logger.info("세션 %d개 핸드오프 기록", len(live))
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
    """post_init 훅. 승인 목록 적재 → 세션 복원 → 재시작 안내 순서."""
    load_users(app)
    restored = restore_sessions(app)
    await notify_restart(app, restored)
    # notify_restart 가 상태 파일을 소비했으므로, 복원된 세션 기준으로 다시 남긴다.
    # 이게 없으면 연속 재시작 시 두 번째부터 안내가 나가지 않는다.
    save_state(app)


async def notify_restart(app: Application, restored=frozenset()):
    """직전 실행에서 살아 있던 chat 에게 재시작을 알린다.

    세션이 복원된 사람에게는 재로그인을 요구하지 않는다. 헌팅은 어느 쪽이든
    복원되지 않으므로 다시 걸어야 한다.
    """
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            snapshot = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        logger.warning("상태 파일 읽기 실패: %s", e)
        return

    for raw_id, labels in snapshot.items():
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            logger.warning("상태 파일의 chat_id 가 정수가 아니다: %r", raw_id)
            continue

        if chat_id in restored:
            text = "🔄 봇이 재시작됐다.\n코레일 세션은 복원됐으니 다시 로그인할 필요 없다."
            tail = "\n\n다시 걸려면 /reserve"
        else:
            text = (
                "🔄 봇이 재시작됐다.\n"
                "자격증명을 메모리에만 두는 구조라 로그인 세션이 초기화됐다. "
                "/login 으로 다시 로그인하라."
            )
            tail = "\n\n로그인 후 /reserve 로 다시 걸어야 한다."
        if labels:
            text += "\n\n<b>중단된 헌팅</b>\n" + "\n".join(f"· {lb}" for lb in labels) + tail
        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.warning("재시작 안내 전송 실패 (chat_id=%s): %s", chat_id, e)

    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


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
    "korail-hunt 봇 — 각자 본인 코레일 계정으로 예약한다.\n\n"
    "/login - 코레일 로그인 (제일 먼저)\n"
    "/logout - 로그아웃 (메모리에서 계정 삭제)\n"
    "/reserve - 예약 시작 (일시 → 역 → 열차 → 옵션)\n"
    "/reservations - 현재 예약\n"
    "/hunt_stop - 헌팅 중단\n"
    "/cancel - 진행 취소\n"
    "/help - 도움말\n\n"
    "계정 정보는 메모리에만 있다. 봇이 재시작되면 다시 /login 해야 한다."
)

ADMIN_HELP = (
    "\n\n<관리자>\n"
    "/users - 승인된 사용자 목록 / 권한 해제"
)


def _help_for(chat_id):
    if chat_id in admin_chat_ids():
        return HELP_TEXT + ADMIN_HELP
    return HELP_TEXT


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_for(update.effective_chat.id))


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_for(update.effective_chat.id))


@with_session
async def cmd_reservations(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session):
    korail: Korail = session.korail
    await _ensure_login(session)
    try:
        rsvs = await _korail_call(session, korail.reservations)
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
            "✅ 접근이 승인됐다.\n\n"
            "/login 으로 본인 코레일 계정을 등록한 뒤 /reserve 로 시작하라.\n"
            "예약과 결제는 전부 본인 계정으로 이뤄진다."
        )
    else:
        data['denied'][target] = info
        save_users(context)
        _drop_user(context, target)
        await query.edit_message_text(
            f"⛔ <b>거부됨</b>\n{_user_label(info, target)}", parse_mode=ParseMode.HTML
        )
        notice = "⛔ 접근 요청이 거부됐다."

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
    lines.append("\n🟢 = 코레일 로그인 상태")
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
            "이미 로그인되어 있다. 계정을 바꾸려면 /logout 후 다시 /login 하라."
        )
        return ConversationHandler.END
    # 진행 중이던 /reserve 대화의 잔여 상태를 정리한다.
    context.user_data.clear()
    await update.message.reply_text(
        "코레일 아이디를 입력하라.\n"
        "회원번호(8자리) / 이메일 / 휴대폰번호(010-0000-0000) 중 하나.\n\n"
        "중단하려면 /cancel"
    )
    return LOGIN_ID


async def login_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.user_data[KEY_LOGIN_ID] = normalize_korail_id(update.message.text)
    await context.bot.send_message(
        chat_id,
        "비밀번호를 입력하라.\n"
        "입력한 메시지는 봇이 즉시 삭제한다. 비밀번호는 메모리에만 보관되며 "
        "디스크나 로그에 남지 않는다.",
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
        await context.bot.send_message(chat_id, "로그인 흐름이 끊겼다. /login 부터 다시 하라.")
        return ConversationHandler.END

    notice = await context.bot.send_message(chat_id, "로그인 중...")
    korail = PatchedKorail(korail_id, korail_pw, auto_login=False)
    try:
        ok = await asyncio.to_thread(korail.login)
    except Exception as e:  # 네트워크·파싱 실패 등. 예외 메시지에 비번은 담기지 않는다.
        logger.warning("로그인 예외 (chat_id=%s): %s", chat_id, e)
        await notice.edit_text(f"로그인 실패: {e}\n/login 으로 다시 시도하라.")
        return ConversationHandler.END

    if not ok:
        await notice.edit_text(
            "로그인 실패 — 아이디/비밀번호를 확인하라.\n\n"
            "아이디는 셋 중 하나여야 한다:\n"
            "· 회원번호 8자리 (예: 12345678)\n"
            "· 이메일 (예: hong@example.com)\n"
            "· 휴대폰번호 (예: 010-1234-5678)\n\n"
            "'잘못 입력하셨습니다(회원번호)' 가 뜬다면 아이디가 위 형식 중 어느 것도"
            " 아니어서 회원번호로 처리된 것이다.\n"
            "반복 실패 시 코레일톡 앱에서 직접 로그인해 계정 상태를 확인하라.\n\n"
            "/login 으로 다시 시도."
        )
        return ConversationHandler.END

    sessions(context)[chat_id] = Session(korail)
    save_state(context)
    await notice.edit_text(
        f"✅ 로그인 성공 — {korail.name} ({korail.membership_number})\n\n"
        f"/reserve 로 예약을 시작하라."
    )
    return ConversationHandler.END


@restricted
async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(KEY_LOGIN_ID, None)
    await update.message.reply_text("로그인 취소")
    return ConversationHandler.END


@restricted
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if get_session(context, chat_id) is None:
        await update.message.reply_text("로그인 상태가 아니다.")
        return
    stopped = _drop_user(context, chat_id)
    context.user_data.clear()
    save_state(context)
    msg = "로그아웃 완료. 메모리에서 계정 정보를 삭제했다."
    if stopped:
        msg += f" (헌팅 {stopped}개 중단)"
    await update.message.reply_text(msg)


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


@with_session
async def conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session):
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
    session = await _session_or_end(update, context)
    if session is None:
        return ConversationHandler.END
    korail: Korail = session.korail
    dep = context.user_data[KEY_DEP]
    arr = context.user_data[KEY_ARR]
    d = context.user_data[KEY_DATE]
    t = context.user_data[KEY_TIME]

    await _ensure_login(session)
    try:
        trains = await _korail_call(
            session, korail.search_train, dep, arr, d, t, include_no_seats=True,
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
    session = await _session_or_end(update, context)
    if session is None:
        return ConversationHandler.END
    korail: Korail = session.korail
    train_idx = context.user_data[KEY_SELECTED_TRAIN_IDX]
    train = context.user_data[KEY_TRAINS][train_idx]

    # 매진 열차면 예약 시도 건너뛰고 즉시 해당 열차 헌팅 시작.
    if not train.has_seat():
        await query.edit_message_text(f"{train!r}\n좌석 없음 — 이 열차 헌팅 시작")
        await _start_train_hunt(update, context, train_idx, option)
        return ConversationHandler.END

    await query.edit_message_text(f"예약 중... {train!r}")
    await _ensure_login(session)
    try:
        rsv = await _korail_call(session, korail.reserve, train, None, option, False)
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
    session = await _session_or_end(update, context)
    if session is None:
        return
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
        _hunt_loop(context, session, chat_id, hunt_id, label, dep, arr, d, t, interval)
    )
    chat_hunts[hunt_id] = {'task': task, 'label': label}
    save_state(context)


async def _hunt_loop(context, session, chat_id, hunt_id, label, dep, arr, d, t, interval):
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
                    session, korail.reserve, trains[0], None, ReserveOption.GENERAL_FIRST, False,
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
    interval = float(os.environ.get('TELEGRAM_HUNT_INTERVAL', '3'))

    hunt_id = _next_hunt_id(chat_hunts)
    label = _format_hunt_label(dep, arr, d, t, train=train)

    await update.effective_message.reply_text(
        f"[{hunt_id}] {label}\n옵션: {option}, 간격 {interval}s. /hunt_stop 으로 중단."
    )

    task = asyncio.create_task(
        _train_hunt_loop(context, session, chat_id, hunt_id, label, target,
                         dep, arr, d, t, option, interval)
    )
    chat_hunts[hunt_id] = {'task': task, 'label': label}
    save_state(context)


async def _train_hunt_loop(context, session, chat_id, hunt_id, label, target,
                           dep, arr, d, t, option, interval):
    korail: Korail = session.korail
    bot = context.bot
    attempts = 0
    try:
        while True:
            attempts += 1
            try:
                await _ensure_login(session)
                trains = await _korail_call(
                    session, korail.search_train, dep, arr, d, t, include_no_seats=True,
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
                rsv = await _korail_call(session, korail.reserve, match, None, option, False)
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
        save_state(context)


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
