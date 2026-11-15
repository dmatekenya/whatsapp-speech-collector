FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY gateway.py writeback.py /srv/
# Owner must verify that this unprivileged UID can READ /audio mount.
RUN groupadd --gid 1001 gateway && useradd --uid 1001 --gid 1001 --no-create-home gateway
USER 1001:1001
EXPOSE 8875
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8875", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
