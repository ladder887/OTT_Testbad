FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY client_agent.py /app/client_agent.py

ENTRYPOINT ["python", "/app/client_agent.py"]
CMD ["idle"]
