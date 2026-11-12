"""A tiny PyTorch model for local testing, with optional fault injection."""
from __future__ import annotations

import argparse

import torch
from torch import nn

IN_FEATURES = 16
VARIANTS = {
    "good": {},
    "slow": {"latency_ms": 120},
    "errors": {"error_rate": 0.15},
    "shifted": {"output_bias": 3.0},
}


def build() -> nn.Module:
    return nn.Sequential(nn.Linear(IN_FEATURES, 64), nn.ReLU(), nn.Linear(64, 64), nn.ReLU(), nn.Linear(64, 1))


def save(path: str, seed: int = 0, variant: str = "good") -> None:
    torch.manual_seed(seed)
    torch.save({"state_dict": build().state_dict(), "faults": VARIANTS[variant]}, path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--variant", default="good", choices=list(VARIANTS))
    args = ap.parse_args()
    save(args.out, args.seed, args.variant)
    print(args.out)