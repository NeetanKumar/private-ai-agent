FROM python:3.12-slim
WORKDIR /app
COPY gateway/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY gateway /app/gateway
ENV GATEWAY_CONFIG=/app/infra/config.yaml
RUN useradd -r -u 10001 gw
USER gw
EXPOSE 8080
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
