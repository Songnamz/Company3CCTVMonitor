"""(Re)train the shirtless classifier.

Training data lives in training/features.pt as CLIP embeddings plus labels (no images are
kept). Every person flagged "no shirt" in an event that someone marked "Not shirtless" in
the web UI is added as a negative example, so recurring false alarms get learned away.

Run from the project directory, then rebuild the detector:
    docker compose run --rm -v ./data:/data:ro -v ./detector:/out detector python train_probe.py
    docker compose up -d --build detector
"""

import json
import os
import sqlite3
import sys

import torch
from PIL import Image

import detector

OUT = os.getenv("OUT_DIR", "/out")
DB = "/data/events.db"
L2 = 0.01
SCALE = 20.0


def train(X: torch.Tensor, y: torch.Tensor):
    w = torch.zeros(X.shape[1], requires_grad=True)
    b = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([w, b], max_iter=300)
    pos_weight = (len(y) - y.sum()) / y.sum()  # few positives: balance the classes

    def closure():
        opt.zero_grad()
        loss = torch.nn.functional.binary_cross_entropy_with_logits(X @ w * SCALE + b, y, pos_weight=pos_weight)
        loss = loss + L2 * (w * w).sum()
        loss.backward()
        return loss

    opt.step(closure)
    return w.detach(), b.detach()


def cross_validate(X, y, threshold):
    hits = false_alarms = 0
    for i in range(len(y)):
        keep = torch.arange(len(y)) != i
        w, b = train(X[keep], y[keep])
        p = torch.sigmoid(X[i] @ w * SCALE + b).item() >= threshold
        hits += p and y[i] == 1
        false_alarms += p and y[i] == 0
    print(f"leave-one-out @ {threshold}: caught {int(hits)}/{int(y.sum())} shirtless, "
          f"false alarms {int(false_alarms)}/{int(len(y) - y.sum())}")


def reviewed_negatives(models, seen: set[int]):
    """People flagged 'no shirt' in events a person marked as wrong."""
    if not os.path.exists(DB):
        return [], []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    feats, ids = [], []
    for eid, image, dets in con.execute(
            "SELECT id, image, detections FROM events WHERE reviewed = 1 AND detections IS NOT NULL"):
        if eid in seen or not image:
            continue
        path = f"/data/snapshots/{image}"
        if not os.path.exists(path):
            continue
        img = Image.open(path).convert("RGB")
        crops = [models._crop(img, d["box"]) for d in json.loads(dets) if d.get("shirtless")]
        if crops:
            feats.append(models.embed(crops))
            ids.append(eid)
    return feats, ids


def main():
    data = torch.load(os.path.join(OUT, "training", "features.pt"))
    X, y = data["X"], data["y"]
    seen = set(data.get("event_ids", []))
    models = detector.Models()
    new, ids = reviewed_negatives(models, seen)
    if new:
        Xn = torch.cat(new)
        X, y = torch.cat([X, Xn]), torch.cat([y, torch.zeros(len(Xn))])
        data.setdefault("colour", []).extend(["?"] * len(Xn))
        print(f"added {len(Xn)} negatives from {len(ids)} reviewed events")
    else:
        print("no new reviewed events")
    print(f"training on {len(y)} people ({int(y.sum())} shirtless)")
    if "--no-cv" not in sys.argv:
        cross_validate(X, y, detector.SHIRTLESS_THRESHOLD)
    w, b = train(X, y)
    torch.save({"w": w, "b": b, "scale": SCALE, "model": "ViT-B-16/laion2b_s34b_b88k",
                "n": len(y), "positives": int(y.sum())}, os.path.join(OUT, "shirt_probe.pt"))
    data.update(X=X, y=y, event_ids=sorted(seen | set(ids)))
    torch.save(data, os.path.join(OUT, "training", "features.pt"))
    print("saved shirt_probe.pt - now run: docker compose up -d --build detector")


if __name__ == "__main__":
    main()
