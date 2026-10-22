FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY . .
RUN useradd --create-home app && mkdir -p /data && chown -R app:app /app /data
USER app
ENV DB_PATH=/data/app.db
EXPOSE 8012
CMD ["uvicorn", "budget_gateway.app:app", "--host", "0.0.0.0", "--port", "8012"]
