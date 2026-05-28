"""
EncryptedStorage 단위 테스트.
"""
import base64
import os
import tempfile
import threading
import unittest
from pathlib import Path

from korail2.storage import EncryptedStorage, StorageKeyError, generate_key


class EncryptedStorageTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "store.enc"
        self.key = generate_key()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _open(self, key=None):
        return EncryptedStorage(self.path, key or self.key)

    def test_generate_key_is_valid(self):
        key = generate_key()
        raw = base64.urlsafe_b64decode(key)
        self.assertEqual(len(raw), 32)

    def test_round_trip_single_user(self):
        s = self._open()
        s.set_user(12345, 'foo@bar.com', 'secret')
        s2 = self._open()
        entry = s2.get_user(12345)
        self.assertEqual(entry['korail_id'], 'foo@bar.com')
        self.assertEqual(entry['korail_pw'], 'secret')
        self.assertIsNone(entry['device_info'])

    def test_round_trip_multiple_users(self):
        s = self._open()
        s.set_user(111, 'a', 'pw_a')
        s.set_user(222, 'b', 'pw_b')
        s.set_user(333, 'c', 'pw_c')
        s2 = self._open()
        self.assertEqual(set(s2.list_user_ids()), {111, 222, 333})
        entry = s2.get_user(222)
        self.assertEqual(entry['korail_id'], 'b')
        self.assertEqual(entry['korail_pw'], 'pw_b')

    def test_get_user_returns_none_when_missing(self):
        s = self._open()
        self.assertIsNone(s.get_user(99999))

    def test_delete_user(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        self.assertTrue(s.delete_user(12345))
        self.assertIsNone(s.get_user(12345))
        # 두번째 delete 는 False
        self.assertFalse(s.delete_user(12345))

    def test_delete_persists(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        s.delete_user(12345)
        s2 = self._open()
        self.assertIsNone(s2.get_user(12345))

    def test_overwrite_user(self):
        s = self._open()
        s.set_user(12345, 'old', 'old_pw')
        s.set_user(12345, 'new', 'new_pw')
        entry = s.get_user(12345)
        self.assertEqual(entry['korail_id'], 'new')
        self.assertEqual(entry['korail_pw'], 'new_pw')

    def test_korean_credentials_round_trip(self):
        s = self._open()
        s.set_user(12345, '한글ID', '비밀번호123!@#')
        s2 = self._open()
        entry = s2.get_user(12345)
        self.assertEqual(entry['korail_id'], '한글ID')
        self.assertEqual(entry['korail_pw'], '비밀번호123!@#')

    def test_get_user_returns_copy_not_reference(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        got = s.get_user(12345)
        got['korail_id'] = 'tampered'
        # 내부 상태는 그대로
        self.assertEqual(s.get_user(12345)['korail_id'], 'foo')

    def test_wrong_key_raises(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        wrong_key = generate_key()
        with self.assertRaises(StorageKeyError):
            EncryptedStorage(self.path, wrong_key)

    def test_invalid_key_format_raises(self):
        with self.assertRaises(StorageKeyError):
            EncryptedStorage(self.path, "not-base64!!!")

    def test_wrong_key_length_raises(self):
        # 16바이트 키 (AES-128 길이) — 우리는 32바이트만 허용
        short_key = base64.urlsafe_b64encode(os.urandom(16)).decode('ascii')
        with self.assertRaises(StorageKeyError):
            EncryptedStorage(self.path, short_key)

    def test_file_is_actually_encrypted(self):
        s = self._open()
        s.set_user(12345, 'foo', 'super_secret_password')
        blob = self.path.read_bytes()
        # 평문 비번이 파일에 그대로 나오면 안 됨
        self.assertNotIn(b'super_secret_password', blob)
        self.assertNotIn(b'foo', blob)

    def test_concurrent_writes_are_serialized(self):
        s = self._open()

        def worker(i):
            for _ in range(20):
                s.set_user(i, f'id{i}', f'pw{i}')

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 다섯 명 모두 살아있고 마지막 값이 일관돼야 함
        self.assertEqual(set(s.list_user_ids()), {0, 1, 2, 3, 4})
        for i in range(5):
            entry = s.get_user(i)
            self.assertEqual(entry['korail_id'], f'id{i}')
            self.assertEqual(entry['korail_pw'], f'pw{i}')

    def test_atomic_write_no_tmp_left_behind(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        self.assertFalse(tmp.exists(), ".tmp 파일이 남아 있으면 atomic write 가 깨진 것")

    # --- device_info ---

    def test_set_user_device_persists(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        s.set_user_device(12345, user_agent='Dalvik/2.1.0 (test)', device_code='AD', platform='android')
        s2 = self._open()
        entry = s2.get_user(12345)
        self.assertEqual(entry['device_info'], {
            'user_agent': 'Dalvik/2.1.0 (test)',
            'device_code': 'AD',
            'platform': 'android',
        })

    def test_set_user_device_preserved_across_credential_update(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        s.set_user_device(12345, user_agent='ua1', device_code='AD', platform='android')
        # 비번 바꿔도 device_info 살아있어야 함
        s.set_user(12345, 'foo', 'new_pw')
        entry = s.get_user(12345)
        self.assertEqual(entry['korail_pw'], 'new_pw')
        self.assertEqual(entry['device_info']['user_agent'], 'ua1')

    def test_set_user_device_clear_with_all_none(self):
        s = self._open()
        s.set_user(12345, 'foo', 'bar')
        s.set_user_device(12345, user_agent='ua', device_code='AD', platform='android')
        s.set_user_device(12345)  # 다 None
        self.assertIsNone(s.get_user(12345)['device_info'])

    def test_set_user_device_unknown_user_raises(self):
        s = self._open()
        with self.assertRaises(KeyError):
            s.set_user_device(99999, user_agent='x')

    # --- pending_payments ---

    def test_add_and_list_pending_payment(self):
        s = self._open()
        s.add_pending_payment(123, 'PNR1', deadline_iso='2026-05-30T14:05:00+09:00',
                              repr_text='[KTX] 서울→부산', price=59800, seat_count=1)
        payments = s.list_pending_payments(123)
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]['rsv_id'], 'PNR1')
        self.assertEqual(payments[0]['price'], 59800)

    def test_pending_payment_persists(self):
        s = self._open()
        s.add_pending_payment(123, 'PNR1', deadline_iso='2026-05-30T14:05:00+09:00',
                              repr_text='x', price=100, seat_count=1)
        s2 = self._open()
        self.assertEqual(len(s2.list_pending_payments(123)), 1)

    def test_remove_pending_payment(self):
        s = self._open()
        s.add_pending_payment(123, 'PNR1', deadline_iso='2026-05-30T14:05:00+09:00',
                              repr_text='x', price=100, seat_count=1)
        self.assertTrue(s.remove_pending_payment(123, 'PNR1'))
        self.assertEqual(s.list_pending_payments(123), [])
        self.assertFalse(s.remove_pending_payment(123, 'PNR1'))

    def test_overwrite_same_rsv_id(self):
        s = self._open()
        s.add_pending_payment(123, 'PNR1', deadline_iso='t1', repr_text='old', price=1, seat_count=1)
        s.add_pending_payment(123, 'PNR1', deadline_iso='t2', repr_text='new', price=2, seat_count=1)
        payments = s.list_pending_payments(123)
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]['repr_text'], 'new')

    def test_multiple_users_isolated(self):
        s = self._open()
        s.add_pending_payment(111, 'A', deadline_iso='t1', repr_text='x', price=1, seat_count=1)
        s.add_pending_payment(222, 'B', deadline_iso='t2', repr_text='y', price=2, seat_count=1)
        self.assertEqual(len(s.list_pending_payments(111)), 1)
        self.assertEqual(len(s.list_pending_payments(222)), 1)
        self.assertEqual(s.list_pending_payments(333), [])

    def test_all_pending_payments_returns_chat_id_pairs(self):
        s = self._open()
        s.add_pending_payment(111, 'A', deadline_iso='t1', repr_text='x', price=1, seat_count=1)
        s.add_pending_payment(111, 'B', deadline_iso='t2', repr_text='y', price=2, seat_count=1)
        s.add_pending_payment(222, 'C', deadline_iso='t3', repr_text='z', price=3, seat_count=1)
        all_p = s.all_pending_payments()
        self.assertEqual(len(all_p), 3)
        chat_ids = {chat_id for chat_id, _ in all_p}
        self.assertEqual(chat_ids, {111, 222})
        # chat_id 는 int 로 반환
        for chat_id, _ in all_p:
            self.assertIsInstance(chat_id, int)

    def test_list_pending_payments_returns_copies(self):
        s = self._open()
        s.add_pending_payment(123, 'PNR1', deadline_iso='t', repr_text='x', price=1, seat_count=1)
        payments = s.list_pending_payments(123)
        payments[0]['rsv_id'] = 'TAMPERED'
        # 내부 상태는 그대로
        self.assertEqual(s.list_pending_payments(123)[0]['rsv_id'], 'PNR1')


if __name__ == '__main__':
    unittest.main()
