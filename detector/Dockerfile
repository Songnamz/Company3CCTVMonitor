FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_DIR=/models \
    HF_HOME=/models/hf \
    YOLO_CONFIG_DIR=/tmp/ultralytics

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# CPU-only PyTorch (no CUDA - this server has no GPU)
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        torch==2.7.1 torchvision==0.22.1
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Bake the model weights into the image so startup needs no internet.
COPY fetch_models.py /tmp/
RUN python /tmp/fetch_models.py && chmod -R a+rX /models && rm -rf /tmp/ultralytics

# Weights are baked in; never call out to Hugging Face at runtime.
ENV HF_HUB_OFFLINE=1 \
    OMP_WAIT_POLICY=PASSIVE

WORKDIR /detector
COPY detector.py train_probe.py shirt_probe.pt ./
COPY training/ training/
CMD ["python", "detector.py"]
