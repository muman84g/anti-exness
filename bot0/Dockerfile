FROM python:3.12-slim
RUN useradd --system --uid 65532 --create-home --shell /usr/sbin/nologin bot0
WORKDIR /app/bot0
COPY bot0/auth_config.py bot0/dashboard.py bot0/healthcheck.py bot0/index.html ./
USER 65532:65532
EXPOSE 8230
ENTRYPOINT ["python", "/app/bot0/dashboard.py"]
