#!/usr/bin/env python3
"""
Korail 좌석 헌팅 스크립트.

환경변수 또는 프로젝트 루트의 `.env` 파일에 아래 키를 설정한 뒤 실행한다.

    KORAIL_ID, KORAIL_PW   (필수)
    DEP, ARV               (필수, 한글역명)
    DEP_DATE               (필수, yyyyMMdd)
    DEP_TIME               (필수, hhmmss)
    TRAIN_TYPE             (선택, TrainType 상수명. 기본 ALL)
    ADULT_COUNT            (선택, 기본 1)
    POLL_INTERVAL          (선택, 초 단위. 기본 2.0)
    PUSHOVER_APP_TOKEN     (선택, 알림용)
    PUSHOVER_USER_TOKEN    (선택, 알림용)

`.env` 로딩은 `python-dotenv` 가 설치돼 있을 때만 자동으로 일어난다.
설치하지 않은 환경에서는 OS 환경변수를 그대로 사용한다.
"""
import os
import sys
import time

# 사내 프록시/백신의 SSL 인스펙션 환경에서도 동작하도록 OS 인증서 저장소 사용.
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

from korail2 import (
    AdultPassenger,
    Korail,
    KorailError,
    NoResultsError,
    TrainType,
)


def _required_env(key):
    v = os.environ.get(key)
    if not v:
        sys.exit(f"환경변수 {key} 가 설정되어 있지 않다. .env 파일을 확인하라.")
    return v


def _parse_train_type(name):
    if not name:
        return TrainType.ALL
    if not hasattr(TrainType, name):
        sys.exit(f"알 수 없는 TRAIN_TYPE: {name!r}. TrainType 상수명을 사용하라.")
    return getattr(TrainType, name)


def sendnoti(msg, *, app_token=None, user_token=None):
    """알림 hook. Pushover/Slack 등으로 확장하려면 여기에 구현을 추가한다."""
    print(f"[NOTI] {msg}", file=sys.stderr)


def hunt(korail, dep, arr, dep_date, dep_time, passengers, train_type, poll_interval=2.0):
    """좌석이 잡힐 때까지 polling. 잡히면 결과 리스트를 반환한다."""
    while True:
        try:
            sys.stdout.write(f"Finding seat {dep} → {arr}              \r")
            sys.stdout.flush()
            trains = korail.search_train_allday(
                dep, arr, dep_date, dep_time,
                passengers=passengers, train_type=train_type,
            )
            print()
            for t in trains:
                print(t)
            return trains
        except NoResultsError:
            sys.stdout.write("No seats                                \r")
            sys.stdout.flush()
        except KorailError as e:
            print(f"\nKorailError: {e}", file=sys.stderr)
        except Exception as e:
            print(f"\nUnexpected: {e!r}", file=sys.stderr)
        time.sleep(poll_interval)


def main():
    korail_id = _required_env("KORAIL_ID")
    korail_pw = _required_env("KORAIL_PW")
    dep = _required_env("DEP")
    arr = _required_env("ARV")
    dep_date = _required_env("DEP_DATE")
    dep_time = _required_env("DEP_TIME")
    train_type = _parse_train_type(os.environ.get("TRAIN_TYPE"))
    adult_count = int(os.environ.get("ADULT_COUNT", "1"))
    poll_interval = float(os.environ.get("POLL_INTERVAL", "2.0"))

    pushover_app = os.environ.get("PUSHOVER_APP_TOKEN")
    pushover_user = os.environ.get("PUSHOVER_USER_TOKEN")

    passengers = [AdultPassenger(adult_count)]

    korail = Korail(korail_id, korail_pw, auto_login=False)
    if not korail.login():
        sys.exit("로그인 실패")

    trains = hunt(
        korail, dep, arr, dep_date, dep_time, passengers, train_type,
        poll_interval=poll_interval,
    )

    # 헌팅이 길어졌을 경우 세션이 만료되어 있을 수 있다.
    korail.login()
    try:
        seat = korail.reserve(trains[0], passengers=passengers)
    except KorailError as e:
        sendnoti(f"예약 실패: {e}", app_token=pushover_app, user_token=pushover_user)
        sys.exit(f"예약 실패: {e}")

    msg = f"예약 성공: {seat!r}"
    print(msg)
    sendnoti(msg, app_token=pushover_app, user_token=pushover_user)


if __name__ == "__main__":
    main()
