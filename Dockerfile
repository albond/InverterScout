FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

LABEL org.opencontainers.image.title="InverterScout" \
      org.opencontainers.image.description="Self-hosted LuxPower inverter monitor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    INVERTERSCOUT_DATA_DIR=/app/data \
    SETUP_BIND_HOST=0.0.0.0

WORKDIR /app

RUN groupadd --gid 10001 inverterscout \
    && useradd --uid 10001 --gid inverterscout --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin inverterscout

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade "pip==26.2.1" \
    && python -m pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R inverterscout:inverterscout /app/data
USER inverterscout

EXPOSE 8080
VOLUME ["/app/data"]

CMD ["python", "-m", "inverterscout"]
