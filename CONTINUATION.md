# korail-hunt 작업 인계 노트

마지막 업데이트: 2026-05-29
활성 브랜치: `multi-user` (HEAD: `189620a`)

다음 세션을 시작할 때 이 파일 먼저 읽고 [§5 다음 작업](#5-다음-작업--우선순위-순) 으로 넘어가면 됩니다.

---

## 1. 한 줄 요약

원본 `carpedm20/korail2` + JissuPark 의 fix-login/DynaPath 우회 위에
**텔레그램 멀티유저 봇 + 헌팅 + 결제 알림 + 인원수 + 헌팅/세션 영속화** 를 얹은 상태.

다음 마일스톤: **회사 PC → Oracle Cloud 이전 + 자동결제 (간편결제 PIN)**.

## 2. 브랜치 상태

| 브랜치 | 상태 | 내용 |
|---|---|---|
| `master` | 원본 + DynaPath 우회 | k-skill 기반, 단일 사용자 봇 |
| `modernize-for-py3` | 머지 대기 | Py3 모더나이즈, 공유 세션 버그 수정, 단위 테스트 61개 |
| `telegram-bot` | 머지 대기 | 인터랙티브 봇 (단일 사용자) |
| `multi-user` | **활성, 최신** | 멀티유저 + 그 이후 모든 작업 |

`multi-user` 가 모든 최신 기능 포함. 정리 시점에 master 로 PR 후 머지 권장.
브랜치 4개 모두 GitHub 에 push 돼 있음.

## 3. multi-user 에 들어간 주요 기능 (commit 순)

| commit | 내용 |
|---|---|
| `9053461` | 멀티유저 세션, 암호화 저장소, 복수 chat_ids |
| `2efbdf1` | Telegram WebApp 으로 본인 휴대폰 UA 자동 감지 |
| `924382d` | WebApp 컨텍스트 체크 완화 + UA 복사 fallback |
| `65cc0d6` | WebApp UI 즉시 렌더 (새로고침 필요 문제 fix) |
| `9fbe54e` | ERR299907 fix: device_code 'iOS' 적용 안 함 (DynaPath 는 Android 전용) |
| `0d4868b` | truststore 로 OS 인증서 사용 (사내 SSL 검사 우회) |
| `a8bdd0a` | 로그인 실패 사유 로깅, KORAIL_LOGIN_VERSION env 추가 |
| `07a5a17` | 모던 UA 기본값 + KORAIL_USER_AGENT 오버라이드 |
| `3a861ba` | Accept-Language + KORAIL_DEVICE 오버라이드 |
| `46169de` | P058 자동 재로그인 + 1회 재시도 |
| `816378f` | 결제 알림 10/5/3/1 분 전 (JobQueue) |
| `d5577ef` | 헌팅 task 봇 재시작 후 자동 재개 |
| `189620a` | hunt_stop 항상 버튼 + 인원수 선택 단계 |

운영 관련:
- `bot-control.ps1` — Windows PowerShell 백그라운드 실행/관리 (`start|stop|restart|status|logs`)
- 로그 UTF-8 처리, SystemExit 메시지 로깅, 즉시 종료 감지
- `docs/device_detect.html` — GitHub Pages 에서 서빙

## 4. 현재 가장 큰 미해결 문제

**새벽 1~4시 취소표 — 알림 못 듣고 결제 10분 안에 못 함 → 놓침**

제약:
- 회사 PC 에서 운영 중 (개인정보 회피)
- 사용자가 새벽에 PC 옆에 못 있음

해결 방향: **자동결제 풀 자동화 (깨울 필요 없게)** + **클라우드 이전 (회사 PC 분리)**.

상세 분석은 직전 대화 turn 참조. 핵심:
1. 분리된 `payment_service` 프로세스 + Playwright headless
2. letskorail.com 의 간편결제 PIN 만 사용 (카드 데이터 회피)
3. Oracle Cloud Always Free 에 올려 24/7

## 5. 다음 작업 — 우선순위 순

### A. 🔴 [블로커 / 사용자 액션] letskorail.com 간편결제 검증

다음 가는 길이 갈리는 결정점.

확인 절차:
1. letskorail.com 로그인
2. 마이페이지 → 결제수단 / 회원정보 메뉴 확인
3. **간편결제 + 6자리 PIN 등록 가능한지** 확인
4. 등록 가능하면 카드 등록 + PIN 설정
5. 결제 시 OTP 요구하는지도 시범 결제로 확인

결과:
- ✅ **간편결제 가능** → C 단계로 풀 자동화 (카드 데이터 안 다룸)
- ❌ **없거나 OTP 강제** → Phase 1 (반자동: 봇이 결제 페이지까지만, 사용자가 카드 입력) 만 가능

### B. ⚪ Oracle Cloud Always Free 이전

A 결과와 무관하게 진행 가능. 회사 PC 에서 봇을 빼는 것 자체가 목적.

절차:
1. Oracle Cloud 계정 (신용카드 등록은 필요, 과금 없음)
2. ARM Ampere VM 1개 (Always Free): Ubuntu 22.04, 4 OCPU / 24 GB RAM
3. 한국 region (Seoul) 선택해서 코레일까지 latency 짧게
4. SSH 키 등록, `apt install python3-venv tmux git`
5. `git clone https://github.com/JissuPark/korail-hunt.git`
6. `git checkout multi-user`
7. venv 만들고 `pip install -e ".[bot]"`
8. **현재 PC 의 `.env` 와 `bot_storage.enc` 옮기기 (BOT_STORAGE_KEY 가 같아야 복호화됨!)**
9. systemd unit 작성 (`/etc/systemd/system/korail-bot.service`):
   ```ini
   [Unit]
   Description=korail-hunt bot
   After=network-online.target

   [Service]
   User=ubuntu
   WorkingDirectory=/home/ubuntu/korail-hunt
   ExecStart=/home/ubuntu/korail-hunt/venv/bin/python bot.py
   Restart=always
   RestartSec=10
   StandardOutput=append:/home/ubuntu/korail-hunt/bot.log
   StandardError=append:/home/ubuntu/korail-hunt/bot.log

   [Install]
   WantedBy=multi-user.target
   ```
10. `systemctl enable --now korail-bot`
11. (선택) Claude Code CLI 도 같이 설치 → SSH 후 tmux 에서 작업

이전 확인 체크리스트:
- [ ] /whoami 로 본인 계정 살아있는지
- [ ] /reservations 로 기존 예약 보이는지
- [ ] /hunts 로 진행 중이던 헌팅 복원됐는지

### C. 🟡 [A 결과 좋으면] 자동결제 서비스 추가

architecture:
```
payment_service/
├── service.py       # aiohttp app
├── browser.py       # async Playwright driver
├── selectors.py     # letskorail.com 셀렉터 (분리: UI 바뀌면 여기만 손)
└── store.py         # 별도 Fernet 키로 PIN 저장
.env 추가:
  PAYMENT_SERVICE_ENABLED=1
  PAYMENT_SERVICE_PORT=18802
  PAYMENT_SERVICE_KEY=<HMAC 키>
  PAYMENT_STORAGE_KEY=<PIN 암호화용 별도 키>
```

봇 쪽 변경:
- 결제 알림 메시지 + 예약 성공 메시지에 `[💳 자동결제]` inline 버튼
- 탭 → 봇이 HMAC 로 POST `http://127.0.0.1:18802/pay`
- 서비스가 Playwright 로 letskorail.com 자동화 → 간편결제 PIN 입력 → 결제 완료
- 결제 결과 → 봇 → 텔레그램으로 사용자

자동 트리거 모드:
- `.env` 에 `PAYMENT_AUTO_TRIGGER=1` 설정 시 예약 직후 자동결제 시작 (새벽 모드)
- 사용자 깨움 없이 알아서 진행

작업 분량: ~3-5일

### D. 🟢 운영 안정성

- 캡차 등장 → 스크린샷 텔레그램 전송 → 사용자 응답 받아 입력
- transient 에러 2~3회 재시도
- selector 헬스체크 cron 매일
- 결제 성공 / 실패 통계 로깅

## 6. 결정 대기 항목

- [ ] **A**: letskorail.com 간편결제 가능 여부 확인
- [ ] **B**: Oracle Cloud 이전 진행 결정
- [ ] **C**: 자동결제 Phase 2 까지 풀로 갈지, 아니면 Phase 1 (반자동) 만 갈지
- [ ] **B-부수**: Claude Code 도 같이 클라우드에 둘지 (SSH + tmux 로 멀티 PC 사용)

## 7. 운영 노트

### 환경변수 인벤토리 (.env)

**필수** (없으면 봇 안 뜸):
| 변수 | 설명 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather 발급. **과거 한 번 노출됐던 토큰은 폐기 완료**. |
| `TELEGRAM_AUTHORIZED_CHAT_IDS` | 허용 chat_id 콤마구분. 단수 `_CHAT_ID` 도 호환. |
| `BOT_STORAGE_KEY` | 32바이트 base64. **잃으면 `bot_storage.enc` 복호화 불가**. |

**자주 손대는** (매크로 우회 튜닝):
| 변수 | 기본값 | 비고 |
|---|---|---|
| `KORAIL_LOGIN_VERSION` | `250601002` | 매크로 우회 안 되면 최신화 |
| `KORAIL_USER_AGENT` | Galaxy S24 / Android 13 | 본인 폰 UA 로 바꾸면 안전 |
| `KORAIL_DEVICE` | `AD` | PatchedKorail 은 'AD' 고정, env 의미 없음 |

**기타**:
| 변수 | 비고 |
|---|---|
| `BOT_STORAGE_PATH` | 기본 `bot_storage.enc` |
| `TELEGRAM_WEBAPP_URL` | GitHub Pages URL |
| `TELEGRAM_HUNT_INTERVAL` | 기본 3초 |
| `BOT_LOG_FILE` | `bot-control.ps1` 이 자동 설정 |

### 파일 인벤토리

이전 시 반드시 같이 옮길 것:
- `.env` (절대 git 에 올리지 말 것 — `.gitignore` 됨)
- `bot_storage.enc` (사용자 자격증명 + 헌팅 + 결제 대기. BOT_STORAGE_KEY 없으면 못 풂)

자동 생성되는 것 (이전 불필요):
- `bot.log` — 봇 로그
- `.bot.pid` — PowerShell 컨트롤 스크립트가 관리

### 새 PC 셋업

```bash
git clone https://github.com/JissuPark/korail-hunt.git
cd korail-hunt
git checkout multi-user
python -m venv venv

# Linux/macOS
source venv/bin/activate
# Windows
.\venv\Scripts\Activate.ps1

pip install -e ".[bot]"

# 기존 .env 와 bot_storage.enc 복사
# Linux 라면:
sudo apt install -y chromium-browser  # Playwright 쓸 거면 별도 설치
# Windows 라면:
# truststore 가 시스템 인증서 처리

python bot.py
# 또는 (Windows 백그라운드):
.\bot-control.ps1 start
# 또는 (Linux systemd):
sudo systemctl start korail-bot
```

## 8. 보안 메모

- ⚠️ 과거 노출된 봇 토큰 `8822139252:AAF...` — 폐기 완료
- 장기적으로 `BOT_STORAGE_KEY` + 봇 토큰 정기 교체 권장
- 공개 fork 라 git history 에 자격증명 들어간 게 없는지 한 번 훑기 권장
- `bot_storage.enc` 백업하면 BOT_STORAGE_KEY 도 같이 안전한 곳에

## 9. 사용 가능한 봇 명령어 (현재 상태)

| 명령 | 기능 |
|---|---|
| `/start`, `/help` | 도움말 |
| `/login` | 코레일 자격증명 등록 |
| `/logout` | 자격증명 + 헌팅 모두 삭제 |
| `/whoami` | 현재 로그인 정보 |
| `/setdevice` | 본인 폰 UA 자동 감지 (WebApp) |
| `/cleardevice` | 저장된 기기 정보 삭제 |
| `/reserve` | 예약 흐름 시작 |
| `/reservations` | 현재 예약 목록 |
| `/payments` | 결제 대기 + 알림 끄기 |
| `/hunts` | 진행 중인 헌팅 목록 |
| `/hunt_stop` | 헌팅 중단 (버튼 선택) |
| `/cancel` | 진행 중 대화 취소 |

## 10. 자주 본 에러 reference

| 에러 코드/메시지 | 의미 | 대응 |
|---|---|---|
| `MACRO ERROR` | 코레일 매크로 감지 | KORAIL_LOGIN_VERSION / KORAIL_USER_AGENT 갱신, /setdevice 로 본인 폰 UA 적용 |
| `ERR299907` | 사용 불가한 창구/device | device_code 불일치. DynaPath 는 'AD' 고정이라 'iOS' 적용하면 안 됨 — 코드에서 무시 처리됨 |
| `P058` Need to Login | 서버 세션 만료 | _korail_call 안에서 자동 재로그인 + 1회 재시도 |
| `P100`, `WRG000000` 등 | 검색 결과 없음 | NoResultsError — 헌팅 루프에서 continue |
| `SSL CERTIFICATE_VERIFY_FAILED` | 사내 프록시 SSL 검사 | truststore 가 OS 인증서 사용, 자동 처리됨 |

---

## 다음 세션 시작 프롬프트 예시

새 PC 에서 Claude Code 띄운 뒤:

```
CONTINUATION.md 읽고 §5-A (letskorail.com 간편결제 확인) 부터 시작하자.
나는 (했음/아직 안 함) 상태야. 다음 어떻게 가야 하지?
```

또는 클라우드 이전부터:

```
CONTINUATION.md §5-B Oracle Cloud 이전 시작. 단계별로 안내해줘.
```
