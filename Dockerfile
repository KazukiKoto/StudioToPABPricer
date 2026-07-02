FROM python:3.11-slim

# curl is required by pab_pricer to fetch LEGO.com pages: their Cloudflare
# bot-detection blocks Python's requests/urllib3 by TLS fingerprint but
# allows curl, so pricing shells out to the curl binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p outputs

EXPOSE 8000

CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
