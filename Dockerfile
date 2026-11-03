# Web demo image. The dataset in data/mentalhealthbench/ is copied in from your
# local folder; it is not in git. Results and the cache live on a Fly volume.
FROM python:3.13-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["python3", "-m", "mhb.web"]
