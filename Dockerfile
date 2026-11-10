FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the libraries first, so this layer is reused until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY demo ./demo

# Run as a normal user, not root.
RUN useradd --system --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
