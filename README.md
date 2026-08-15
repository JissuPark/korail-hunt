korail-hunt
===========

코레일 승차권 예약·헌팅 텔레그램 봇.

매진된 열차를 주기적으로 조회하다 좌석이 풀리면 자동으로 예약한다.
여러 사람이 함께 쓸 수 있고, **각자 본인 코레일 계정으로** 예약·결제한다.

[carpedm20/korail2](https://github.com/carpedm20/korail2) 라이브러리를 포함하며,
코레일의 anti-bot(DynaPath) 우회가 적용된 `PatchedKorail` 을 쓴다.

Legal disclaimer
----------------

**Usage of korail for attacking targets without prior mutual consent is illegal.**
It's the end user's responsibility to obey all applicable local, state and federal
laws. Developers assume no liability and are not responsible for any misuse or
damage caused by this program. Only use for educational purposes.

폴링 간격을 과도하게 줄이면 계정이 차단될 수 있다. 승인 인원이 늘수록 한 IP에서
코레일로 나가는 요청도 늘어난다는 점을 감안하라.


사용법
------

| 명령어 | 설명 |
|---|---|
| `/start`, `/help` | 도움말 |
| `/login` | 코레일 로그인 (제일 먼저) |
| `/logout` | 로그아웃, 메모리에서 계정 삭제 |
| `/reserve` | 예약 시작 (일시 → 역 → 열차 → 좌석옵션) |
| `/reservations` | 현재 예약 목록 |
| `/hunt_stop` | 진행 중인 헌팅 중단 |
| `/cancel` | 진행 중인 대화 취소 |
| `/users` | (관리자) 승인된 사용자 목록 / 권한 해제 |

`/reserve` 에서 좌석이 없으면 **헌팅 시작** 버튼이 뜬다. 헌팅은 chat 별로 여러 개를
동시에 돌릴 수 있고 `h1`, `h2`... 로 구분된다. 예약에 성공하면 알림이 오며,
**결제는 코레일톡 앱 → 승차권 → 결제대기**에서 직접 해야 한다. 구입기한(보통 10분)을
넘기면 자동 취소된다.

### 사용자 추가

봇은 승인된 사람만 쓸 수 있다. 모르는 사람이 명령을 보내면 관리자에게 승인 버튼이
달린 알림이 가고, 승인하는 즉시 사용 가능해진다. **`.env` 수정도 재시작도 필요 없다.**

승인 목록은 `data/.bot_users.json` 에 남아 재배포를 견딘다.


아키텍처
--------

### chat 별 코레일 세션

`Session` 이 `Korail` 인스턴스와 **전용 `asyncio.Lock`** 을 함께 들고
`bot_data[sessions][chat_id]` 에 산다. 락이 계정별로 쪼개져 있어 다른 사용자의
헌팅이 내 조회를 막지 않는다. (`requests.Session` 이 thread-safe 하지 않아 같은
계정 안에서는 직렬화가 필요하다.)

### 자격증명은 메모리에만

코레일 아이디·비밀번호는 프로세스 메모리에만 존재한다. 디스크에도 로그에도 쓰지
않으며, 비밀번호 입력 메시지는 봇이 수신 즉시 삭제한다.

### 세션 핸드오프

메모리 전용이면 재배포마다 전원이 다시 로그인해야 한다. 이를 피하려고, **정상
종료 시에만** 세션을 AES-GCM 으로 암호화해 파일로 쓰고 기동 직후 복원한 뒤
즉시 삭제한다. 디스크 체류 시간은 재시작에 걸리는 수 초다.

- `SESSION_HANDOFF_KEY` 가 없으면 기능이 꺼진다 (명시적 옵트인)
- 변조는 GCM 인증 태그가, 방치된 파일은 TTL(기본 5분)이 거부한다
- 크래시·전원 차단이면 파일이 없으므로 안전한 쪽(재로그인)으로 떨어진다

키가 같은 서버에 있으니 서버가 털리면 무의미하다. 다만 root 권한 공격자는
`/proc/<pid>/mem` 으로 프로세스 메모리도 읽을 수 있어, 메모리 전용 대비 실질적인
노출 증가는 크지 않다.

> **헌팅은 복원되지 않는다.** 재시작하면 세션은 살아도 진행 중이던 헌팅은 끊기므로
> `/reserve` 로 다시 걸어야 한다.


배포
----

x86_64 서버에 Docker 로 띄운다. 이미지는 GHCR, 배포는 GitHub Actions.

```
push/PR       → CI      테스트 + 이미지 빌드 검증
tag v* / 수동 → Deploy  GHCR push → SSH → docker compose up -d
```

**배포를 push 에 걸지 않는다.** 배포는 곧 봇 재시작이고, 재시작은 진행 중인 헌팅을
끊기 때문이다. 배포는 Actions 탭에서 수동 실행하거나 `v*` 태그를 밀 때만 일어난다.

### 서버 준비

```bash
# Docker (Rocky Linux 9 기준)
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # 재로그인 필요

# 코드와 데이터 디렉터리
git clone https://github.com/JissuPark/korail-hunt.git ~/korail-hunt
cd ~/korail-hunt && mkdir -p data
cp .env.example .env && chmod 600 .env   # 아래 표대로 채운다
```

**`data/` 디렉터리 소유권이 컨테이너 uid 와 맞아야 한다.** 바인드 마운트는 호스트
디렉터리의 소유권을 그대로 적용하므로, 어긋나면 `Permission denied` 로 아무것도
기록되지 않는다 — 그런데 봇은 WARNING 만 남기고 계속 돌기 때문에 눈치채기 어렵다.
컨테이너는 uid 1000 으로 돈다 (`Dockerfile` 의 `APP_UID`).

```bash
ls -n data      # 첫 숫자가 1000 이어야 한다
sudo chown -R 1000:1000 data
```

### GitHub 설정

Secrets (Settings → Secrets and variables → Actions):

| 이름 | 값 |
|---|---|
| `OCI_HOST` | 서버 공인 IP |
| `OCI_USER` | SSH 계정 (`rocky`, `ubuntu` 등) |
| `OCI_SSH_KEY` | SSH 개인키 전문 (`-----BEGIN` 줄 포함) |

첫 배포 후 **GHCR 패키지를 public 으로 바꿔야 한다.** public 레포라도 패키지는
private 으로 생성되며, 그대로 두면 서버에서 `docker compose pull` 이 인증 오류로
실패한다. 프로필 → Packages → `korail-hunt` → Package settings → Change visibility.

### 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | @BotFather 토큰 |
| `TELEGRAM_ADMIN_CHAT_IDS` | 권장 | 관리자 chat_id (쉼표 구분). 없으면 승인 절차가 동작하지 않는다 |
| `SESSION_HANDOFF_KEY` | 권장 | 없으면 재시작마다 전원 재로그인 |
| `TELEGRAM_HUNT_INTERVAL` | | 헌팅 폴링 간격(초), 기본 3 |
| `TELEGRAM_ALLOWED_CHAT_IDS` | | 승인 없이 항상 허용. 보통 비워둔다 |
| `SESSION_HANDOFF_TTL` | | 핸드오프 유효시간(초), 기본 300 |

`BOT_STATE_FILE` / `BOT_USERS_FILE` / `BOT_HANDOFF_FILE` 은 **Docker 사용 시 `.env` 에
두지 마라.** 빈 값이라도 있으면 이미지의 `/app/data` 설정을 덮어써서 볼륨 밖에
파일이 생기고, 재배포마다 사라진다.

`KORAIL_ID` / `KORAIL_PW` 는 봇이 쓰지 않는다 (`korail.py` 스크립트와 통합 테스트 전용).

봇은 기동 시 위 설정을 점검해 문제가 있으면 로그와 **관리자 채팅으로** 알린다.
정상이면 로그에 `설정 점검 통과` 가 찍힌다.

### 첫 실행

`TELEGRAM_ADMIN_CHAT_IDS` 를 모르면 비워둔 채로 띄우고 봇에게 아무 메시지나 보내라.
본인 chat_id 를 알려준다. 그 값을 `.env` 에 넣고 재시작하면 된다.


운영
----

```bash
cd ~/korail-hunt
docker compose ps
docker compose logs -f
docker compose logs --tail=50 | grep -i "설정 점검\|승인된 사용자\|핸드오프"
```

정상 동작 중에는 로그가 **한 줄도 안 나온다.** PTB 가 폴링 로그를 과도하게 뱉어서
`httpx` / `telegram` 로거를 WARNING 으로 낮춰뒀기 때문이다. 로그가 멈춰 있다고
죽은 게 아니니, 살아 있는지는 `/help` 로 확인하는 편이 빠르다.

`httpx.ReadError` 가 간헐적으로 찍히는 것은 정상 범주다. 텔레그램 long polling 은
연결이 끊기는 일이 흔하고 PTB 가 알아서 재시도한다. 컨테이너가 재시작되지 않았다면
프로세스는 살아 있다.


개발
----

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[bot]"
.venv/bin/python -m unittest discover -s test -t .
```

`test_bot.py` 와 `test_unit.py` 는 네트워크 없이 돈다. `test_integration.py` 는 실제
코레일 자격증명이 필요해 CI 에서 제외한다.

로컬 실행은 `.env` 를 만든 뒤 `.venv/bin/python bot.py`.

korail2 라이브러리 API(검색·예약·취소·승객 유형 등) 문서는 [README.rst](README.rst)
와 [docs/](docs/) 에 있다.


트러블슈팅
----------

**로그인이 "잘못 입력하셨습니다(회원번호)" 로 실패한다**
아이디가 회원번호 8자리 / 이메일 / 휴대폰번호 셋 중 어느 형식도 아니면 회원번호로
처리된다. 휴대폰번호는 하이픈 없이 입력해도 봇이 자동으로 넣어준다.

**MACRO ERROR 가 뜬다**
코레일 anti-bot 에 걸린 것이다. `.env` 의 `KORAIL_LOGIN_VERSION` 을 최신 코레일톡
앱 빌드 날짜에 가깝게(`YYMMDDNNN`) 올리고, `KORAIL_USER_AGENT` 를 실제 기기 값으로
맞춰본다. 폴링 간격을 늘리는 것도 방법이다.

**승인 목록이 재배포 때마다 사라진다**
`data/` 소유권 또는 `.env` 의 `BOT_*_FILE` 문제다. 위 [배포](#배포) 절 참고.
기동 로그의 `설정 점검` 결과를 먼저 확인하라.

**GitHub Actions 배포가 이미지 pull 에서 실패한다**
GHCR 패키지가 private 이다. 위 [GitHub 설정](#github-설정) 참고.
