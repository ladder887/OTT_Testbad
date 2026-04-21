FROM python:3.11-slim

WORKDIR /app

# Python 패키지
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# V5 엔진 (Account 기반)
COPY main_v5.py ./main.py

CMD ["python", "main.py"]
