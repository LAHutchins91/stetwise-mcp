# File-processing core. LibreOffice is intentionally not installed: v1 does not
# recalculate or render. A later image can FROM this one and add soffice.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stetwise ./stetwise

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
USER nobody
CMD ["uvicorn", "stetwise.api:app", "--host", "0.0.0.0", "--port", "8080"]
