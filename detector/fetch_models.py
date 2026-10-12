"""Download model weights at image build time."""
import os
import shutil

import open_clip
from ultralytics import YOLO

model_dir = os.environ["MODEL_DIR"]
os.makedirs(model_dir, exist_ok=True)

for name in ("yolo11s.pt",):
    YOLO(name)  # downloads into the working directory
    shutil.move(name, os.path.join(model_dir, name))

open_clip.create_model_and_transforms("ViT-B-16", pretrained="laion2b_s34b_b88k")
print("models ready:", os.listdir(model_dir))
