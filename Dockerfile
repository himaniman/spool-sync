FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY spool_sync ./spool_sync
ENV PYTHONUNBUFFERED=1 SPOOL_DATA=/data
VOLUME /data
ENTRYPOINT ["python", "-m", "spool_sync"]
CMD ["run"]
