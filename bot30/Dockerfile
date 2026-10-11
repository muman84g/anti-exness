ARG BOT30_BASE_IMAGE=exness-bot:latest
FROM ${BOT30_BASE_IMAGE}
# Derived image only: Linux runner dependency does not modify other bot images.
COPY requirements-shadow.txt /opt/bot30-build/requirements-shadow.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir --target /opt/bot30-python -r /opt/bot30-build/requirements-shadow.txt
ENV PYTHONPATH=/opt/bot30-python:/app/bot30
ENV PYTHONDONTWRITEBYTECODE=1
COPY bot30_shadow.py shadow_state.py shadow_bridge.py portable_factory.py portable_control.py stream_harness.py source_bindings.json clock_contract_v002.json /app/bot30/
COPY runtime_manifest_v001.json /app/bot30/runtime_manifest_v001.json
COPY vendor_runtime/ /app/bot30/vendor_runtime/
COPY config/shadow_v001.json /app/bot30/config/shadow_v001.json
COPY BotBridge_s30_readonly.mq5 /app/bot30/BotBridge_s30_readonly.mq5
COPY shadow_v001.gitattributes /app/bot30/shadow_v001.gitattributes
# Existing /app/entrypoint.sh starts Wine/MT5, then Linux python3 bot30_shadow.py.
