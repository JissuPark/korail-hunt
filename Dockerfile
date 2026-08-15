# korail-hunt 텔레그램 봇.
# 텔레그램 long polling 방식이라 노출할 포트가 없다 (아웃바운드 전용).
FROM python:3.12-slim

# 코레일 API 가 KST 기준 날짜를 쓴다. 컨테이너 시계도 맞춰둔다.
ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 의존성 레이어를 먼저 굳혀서 소스만 바뀔 때 재설치를 피한다.
COPY setup.py README.rst ./
COPY korail2/ ./korail2/
RUN pip install --no-cache-dir -e ".[bot]"

COPY bot.py korail.py ./

# 런타임 상태(.bot_users.json, .bot_state.json, 핸드오프)는 전부 여기에 둔다.
# compose 에서 볼륨으로 걸어 컨테이너 교체에도 살아남게 한다.
# uid 를 호스트 첫 사용자와 맞춘다. /app/data 는 바인드 마운트라 호스트
# 디렉터리의 소유권이 이미지 설정을 덮어쓰고, uid 가 어긋나면 컨테이너가
# 아무것도 기록하지 못한다. 클라우드 이미지의 첫 사용자는 관례상 1000
# (rocky/ubuntu/ec2-user). 다르면 --build-arg APP_UID=... 로 맞춰라.
ARG APP_UID=1000
RUN mkdir -p /app/data && useradd -m -u ${APP_UID} bot && chown -R bot:bot /app
USER bot

ENV BOT_USERS_FILE=/app/data/.bot_users.json \
    BOT_STATE_FILE=/app/data/.bot_state.json \
    BOT_HANDOFF_FILE=/app/data/.bot_handoff.enc

# SIGTERM 이 python 에 바로 닿아야 post_stop 훅(세션 핸드오프)이 돈다.
CMD ["python", "bot.py"]
