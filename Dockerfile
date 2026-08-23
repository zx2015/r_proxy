FROM python:3.12-slim

# 固定 UID/GID 而不是让系统分配：宿主上的数据目录按同一数字授权，
# 重建镜像或换机器后 db 文件的属主仍然对得上。
RUN groupadd --gid 10001 rproxy \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin rproxy

WORKDIR /src
COPY pyproject.toml README.md ./
COPY r_proxy ./r_proxy
RUN pip install --no-cache-dir ".[web]" \
    && rm -rf /src

WORKDIR /

# /config 只放 config.toml，/data 放三个 db 与 backups/。
# 两者都由 compose 以 bind mount 覆盖；这里建目录是为了不挂载时也能起得来。
RUN mkdir -p /config /data && chown rproxy:rproxy /config /data

ENV R_PROXY_CONFIG=/config/config.toml \
    PYTHONUNBUFFERED=1

# host 网络下 EXPOSE 不生效，仅作端口约定的说明。
EXPOSE 6060 6061

# 探活只测代理端口的 TCP 可连性：不依赖 Web 是否启用，也就不必往镜像里装 curl。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('R_PROXY_HEALTH_PORT', '6060'))), 3).close()"

USER rproxy

# exec 形式：进程本体即 PID 1，docker stop 的 SIGTERM 直接命中已有的优雅退出逻辑。
CMD ["r-proxy"]
