FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DRY_RUN=true

WORKDIR /app

COPY requirements-test.txt requirements.txt* ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY configs/ ./configs/
COPY runbooks/ ./runbooks/

CMD ["python", "-m", "src.ops.daemon"]
