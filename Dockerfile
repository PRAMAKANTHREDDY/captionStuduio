FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CAPTION_STUDIO_HOST=0.0.0.0 \
    CAPTION_STUDIO_UPLOAD_DIR=/var/data/uploads \
    HF_HOME=/var/data/huggingface \
    TORCH_HOME=/var/data/torch

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchaudio \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . ./

EXPOSE 7860
CMD ["python", "caption_studio.py"]
