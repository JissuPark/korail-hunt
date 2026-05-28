"""
AES-GCM 으로 암호화된 JSON 키-값 저장소.

bot 의 멀티 유저 자격증명 저장에 쓴다. 키(BOT_STORAGE_KEY)가 유출되면
저장 파일을 누구나 복호화할 수 있으므로 키는 별도 관리.
"""
import base64
import json
import os
import threading
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes


_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_LEN = 32  # AES-256


def generate_key():
    """새 BOT_STORAGE_KEY (urlsafe base64) 를 생성한다."""
    return base64.urlsafe_b64encode(get_random_bytes(_KEY_LEN)).decode('ascii')


class StorageKeyError(ValueError):
    pass


class EncryptedStorage:
    """thread-safe 한 atomic-write 암호화 JSON 저장소."""

    def __init__(self, path, key):
        self.path = Path(path)
        try:
            self._key = base64.urlsafe_b64decode(key)
        except Exception as e:
            raise StorageKeyError(f"BOT_STORAGE_KEY 는 urlsafe base64 여야 한다: {e}")
        if len(self._key) != _KEY_LEN:
            raise StorageKeyError(
                f"BOT_STORAGE_KEY 는 {_KEY_LEN}바이트 base64 여야 한다. "
                f"새 키: {generate_key()}"
            )
        self._lock = threading.Lock()
        self._data = self._load()

    # --- 내부: 직렬화 ----------------------------------------------------

    def _load(self):
        if not self.path.exists():
            return {}
        blob = self.path.read_bytes()
        if len(blob) < _NONCE_LEN + _TAG_LEN:
            raise StorageKeyError(f"저장 파일이 손상되었거나 비어 있다: {self.path}")
        nonce = blob[:_NONCE_LEN]
        tag = blob[_NONCE_LEN:_NONCE_LEN + _TAG_LEN]
        ciphertext = blob[_NONCE_LEN + _TAG_LEN:]
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        try:
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        except ValueError as e:
            raise StorageKeyError(
                f"저장 파일을 복호화할 수 없다. BOT_STORAGE_KEY 가 바뀌었나? ({e})"
            )
        return json.loads(plaintext.decode('utf-8'))

    def _save(self):
        nonce = get_random_bytes(_NONCE_LEN)
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        payload = json.dumps(self._data, ensure_ascii=False).encode('utf-8')
        ciphertext, tag = cipher.encrypt_and_digest(payload)
        # atomic 쓰기: 임시 파일에 쓰고 rename.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_bytes(nonce + tag + ciphertext)
        os.replace(tmp, self.path)

    # --- 공개 API: 사용자 자격증명 ---------------------------------------

    def set_user(self, chat_id, korail_id, korail_pw):
        """자격증명만 저장/갱신. device_info 는 보존."""
        with self._lock:
            users = self._data.setdefault('users', {})
            existing = users.get(str(chat_id), {})
            users[str(chat_id)] = {
                'korail_id': korail_id,
                'korail_pw': korail_pw,
                # device_info 는 보존 (재로그인 시 잃지 않게)
                'device_info': existing.get('device_info'),
            }
            self._save()

    def set_user_device(self, chat_id, *, user_agent=None, device_code=None, platform=None):
        """유저의 device 정보만 갱신. 자격증명은 건드리지 않는다.
        세 값 다 None 이면 device_info 를 지운다."""
        with self._lock:
            users = self._data.setdefault('users', {})
            entry = users.get(str(chat_id))
            if entry is None:
                raise KeyError(f"user {chat_id} not found")
            if user_agent is None and device_code is None and platform is None:
                entry['device_info'] = None
            else:
                entry['device_info'] = {
                    'user_agent': user_agent,
                    'device_code': device_code,
                    'platform': platform,
                }
            self._save()

    def get_user(self, chat_id):
        with self._lock:
            entry = self._data.get('users', {}).get(str(chat_id))
            return dict(entry) if entry else None

    def delete_user(self, chat_id):
        with self._lock:
            users = self._data.get('users', {})
            existed = str(chat_id) in users
            users.pop(str(chat_id), None)
            if existed:
                self._save()
            return existed

    def list_user_ids(self):
        with self._lock:
            return [int(k) for k in self._data.get('users', {}).keys()]

    # --- 공개 API: 결제 대기 ---------------------------------------------
    # 예약 후 결제 기한 알림을 위해 봇 재시작 시에도 살아남는 메타데이터.
    # 한 사용자에 여러 개 대기 가능, rsv_id 가 유일 키.

    def add_pending_payment(self, chat_id, rsv_id, *,
                            deadline_iso, repr_text, price, seat_count):
        with self._lock:
            payments = self._data.setdefault('pending_payments', {})
            user_payments = payments.setdefault(str(chat_id), {})
            user_payments[str(rsv_id)] = {
                'rsv_id': rsv_id,
                'deadline_iso': deadline_iso,
                'repr_text': repr_text,
                'price': price,
                'seat_count': seat_count,
            }
            self._save()

    def remove_pending_payment(self, chat_id, rsv_id):
        with self._lock:
            payments = self._data.get('pending_payments', {}).get(str(chat_id), {})
            existed = str(rsv_id) in payments
            payments.pop(str(rsv_id), None)
            if existed:
                self._save()
            return existed

    def list_pending_payments(self, chat_id):
        with self._lock:
            payments = self._data.get('pending_payments', {}).get(str(chat_id), {})
            return [dict(v) for v in payments.values()]

    def all_pending_payments(self):
        """[(chat_id, payment_dict), ...] — 봇 시작 시 알림 재스케줄용."""
        with self._lock:
            out = []
            for chat_id_str, user_payments in self._data.get('pending_payments', {}).items():
                for payment in user_payments.values():
                    out.append((int(chat_id_str), dict(payment)))
            return out

    # --- 공개 API: 헌팅 ---------------------------------------------------
    # 백그라운드 헌팅 task 의 조건. 봇 재시작 시 다시 spawn 한다.
    # hunt_id 는 chat 단위 'h1', 'h2', ... 형식.

    def add_hunt(self, chat_id, hunt_id, **fields):
        """fields: type, dep, arr, date, time, label, interval,
        그리고 train hunt 는 추가로 target(=[no, dep_date, dep_time]), option."""
        with self._lock:
            hunts = self._data.setdefault('hunts', {})
            user_hunts = hunts.setdefault(str(chat_id), {})
            user_hunts[str(hunt_id)] = dict(fields)
            self._save()

    def remove_hunt(self, chat_id, hunt_id):
        with self._lock:
            user_hunts = self._data.get('hunts', {}).get(str(chat_id), {})
            existed = str(hunt_id) in user_hunts
            user_hunts.pop(str(hunt_id), None)
            if existed:
                self._save()
            return existed

    def list_hunts(self, chat_id):
        """[{hunt_id, ...fields}, ...]"""
        with self._lock:
            user_hunts = self._data.get('hunts', {}).get(str(chat_id), {})
            return [dict(h, hunt_id=hid) for hid, h in user_hunts.items()]

    def all_hunts(self):
        """[(chat_id, hunt_id, fields), ...] — 봇 시작 시 재개용."""
        with self._lock:
            out = []
            for chat_id_str, user_hunts in self._data.get('hunts', {}).items():
                for hunt_id, hunt in user_hunts.items():
                    out.append((int(chat_id_str), hunt_id, dict(hunt)))
            return out

    def clear_user_hunts(self, chat_id):
        """logout/계정 정리 시 cascade 제거."""
        with self._lock:
            hunts = self._data.get('hunts', {})
            existed = str(chat_id) in hunts
            hunts.pop(str(chat_id), None)
            if existed:
                self._save()
            return existed
