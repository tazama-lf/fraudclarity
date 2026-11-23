FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FRAUDCLARITY_HOST=0.0.0.0 \
    FRAUDCLARITY_PORT=7860 \
    FRAUDCLARITY_MAX_PORT=7860 \
    OLLAMA_HOST=http://host.docker.internal:11434

WORKDIR /app

COPY requirements.txt ./requirements.txt

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

EXPOSE 7860

CMD ["python", "app.py"]