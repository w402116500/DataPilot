# 阶段四专用的一次性分析镜像；常驻 API/Web/DataLink 镜像由阶段七单独维护。
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        fontconfig \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/matplotlib \
    && printf '%s\n' \
        'font.family: sans-serif' \
        'font.sans-serif: Noto Sans CJK JP, DejaVu Sans' \
        > /tmp/matplotlib/matplotlibrc \
    && fc-cache --force \
    && pip install --no-cache-dir \
    pandas==2.2.3 \
    numpy==2.2.0 \
    matplotlib==3.9.3 \
    scikit-learn==1.6.0 \
    scipy==1.14.1 \
    seaborn==0.13.2 \
    openpyxl==3.1.5 \
    && adduser --disabled-password --gecos "" --uid 10001 sandbox \
    && mkdir -p /workspace/work \
    && chown -R sandbox:sandbox /workspace /tmp/matplotlib

USER sandbox
WORKDIR /workspace/work
