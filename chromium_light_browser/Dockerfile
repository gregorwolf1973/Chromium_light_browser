ARG BUILD_FROM
FROM $BUILD_FROM

# chromium + a virtual display (Xvfb) + a VNC server (x11vnc) + the noVNC
# web client. No window manager, no desktop: the browser window simply fills
# the virtual screen. python3/aiohttp run the controller that starts all of
# it on demand and puts it to sleep again.
RUN apk add --no-cache \
        chromium \
        xvfb \
        x11vnc \
        novnc \
        python3 \
        py3-aiohttp \
        su-exec \
        font-dejavu \
        font-noto-emoji \
    && adduser -D -h /home/browser -s /sbin/nologin browser

COPY app/ /app/

WORKDIR /app
CMD ["python3", "/app/server.py"]
