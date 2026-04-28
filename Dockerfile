FROM adryfish/llm-web-api:latest

COPY docker/patches/sitecustomize.py /app/sitecustomize.py
COPY docker/patches/run.py /app/run.py
