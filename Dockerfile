FROM python:3.11-slim
WORKDIR /app
COPY bot.py requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8080
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8080"]
