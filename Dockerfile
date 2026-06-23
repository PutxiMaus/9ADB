FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV HOME=/data
ENV ADB_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        android-tools-adb \
        android-tools-fastboot \
        aapt \
        usbutils \
        iproute2 \
        iputils-ping \
        netcat-openbsd \
        nmap \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY templates /app/templates
COPY static /app/static
COPY tools /app/tools

EXPOSE 20009

CMD ["gunicorn", "--bind", "0.0.0.0:20009", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]
