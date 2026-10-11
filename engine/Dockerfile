# File-processing core. LibreOffice recalculates a copy; the engine patches
# cached values back into the original package and discards the LibreOffice save.
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends libreoffice-calc python3-uno \
  && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stetwise ./stetwise

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
USER nobody
# PORT defaults to 8080 inside stetwise.api. Vercel and Fly set PORT.
CMD ["python", "-m", "stetwise.api"]
