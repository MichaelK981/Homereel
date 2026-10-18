FROM python:3.12-slim

# Debian's ffmpeg package ships with NVENC support already compiled in — it
# just needs the NVIDIA driver's libnvidia-encode.so at runtime, which the
# NVIDIA Container Toolkit injects into the container (see docker-compose.yml
# for the GPU reservation). No CUDA toolkit needed in the image itself.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
# Shipped so a `docker pull` carries the compose that runs this image:
#   docker create --name tmp michaelk98/homereel:latest
#   docker cp tmp:/srv/docker-compose.yml . && docker rm tmp
# It is the sanitised mirror of the live stack — placeholders, never real keys.
COPY docker-compose.yml ./

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
