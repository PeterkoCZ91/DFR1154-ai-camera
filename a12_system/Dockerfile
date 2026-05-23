# syntax=docker/dockerfile:1
ARG TARGETPLATFORM
ARG BUILDPLATFORM
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Prague

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        libjpeg62-turbo \
        liblapack-dev \
        libopenblas-dev \
        libpng16-16 \
        libx11-6 \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/a12-requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r /tmp/a12-requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY docker/pkg_resources /usr/local/lib/python3.12/site-packages/pkg_resources
COPY . /app/A12_System_v2

CMD ["python", "-m", "A12_System_v2"]
