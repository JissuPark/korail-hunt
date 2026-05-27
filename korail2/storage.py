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
        with self._lock:
            self._data.setdefault('users', {})[str(chat_id)] = {
                'korail_id': korail_id,
                'korail_pw': korail_pw,
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
