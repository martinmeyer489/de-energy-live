FROM python:3.12-slim

# Runtime deps: matplotlib for the /chart.png endpoint; app code is bind-mounted.
RUN pip install --no-cache-dir "matplotlib>=3.8"

WORKDIR /app
CMD ["sh", "serve.sh"]
