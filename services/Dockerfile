# Image commune aux services ; SERVICE choisit lequel lancer (gtfs, realtime, departures, gateway).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY services/requirements.txt ./services/requirements.txt
RUN pip install --no-cache-dir -r services/requirements.txt
COPY filbleu ./filbleu
ARG SERVICE
COPY services/__init__.py ./services/__init__.py
COPY services/${SERVICE} ./services/${SERVICE}
ENV SERVICE=${SERVICE} PORT=8000
CMD uvicorn services.${SERVICE}.app:app --host 0.0.0.0 --port ${PORT}
