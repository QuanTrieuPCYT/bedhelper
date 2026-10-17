FROM docker:cli AS docker-client

FROM python:3.14.8-alpine3.24 AS minifier

WORKDIR /src
RUN pip install --no-cache-dir python-minifier
COPY bot.py .
RUN pyminify --in-place \
             --remove-literal-statements \
             --rename-globals \
             --remove-asserts \
             --remove-debug \
             --prefer-single-line \
             .

FROM python:3.14.8-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    DOCKER_CONFIG=/tmp/docker-config

RUN apk add --no-cache ca-certificates \
    && mkdir -p /compose /usr/local/libexec/docker/cli-plugins

COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-client /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --from=minifier /src/bot.py ./bot.py

CMD ["python3.14", "/app/bot.py"]