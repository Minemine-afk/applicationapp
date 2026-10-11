# Always-on mcf-apply for a home machine (x86 or ARM, e.g. a Raspberry Pi 4/5).
# Includes a virtual screen you can view in a web browser (noVNC, port 6080), used to
# scan the Singpass QR code when MyCareersFuture asks you to log in again.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify fluxbox tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 6080
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
CMD ["mcf-apply", "serve"]
