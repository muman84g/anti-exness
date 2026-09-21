FROM python:3.12-slim
RUN useradd --system --uid 65532 --create-home --shell /usr/sbin/nologin bot0
WORKDIR /app/bot0
COPY dashboard.py index.html ./
USER 65532:65532
EXPOSE 8230
ENTRYPOINT ["python", "/app/bot0/dashboard.py"]
