#!/usr/bin/env python3
"""轻量级 MNIST CNN 训练 — 用于 deva GPU 链路验证。

特性:
- 真 MNIST 优先 (torchvision 下载); 下载失败自动用合成数据兜底, 保证链路一定跑通
- 打印 device / GPU 名 / 每 epoch loss + 显存占用 / 总耗时, 便于确认确实在 GPU 上跑
- 接受 --epochs / --batch-size / --lr / --no-cuda
- 结束保存模型到 outputs/mnist_cnn.pt

本地: python train_mnist.py --epochs 3
deva: deva run --env <env> --gpu 0 train_mnist.py -- --epochs 3
"""
import argparse
import os
import time

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset


def parse_args():
    p = argparse.ArgumentParser(description="轻量 MNIST CNN (GPU 链路验证)")
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--no-cuda", action="store_true", help="强制用 CPU (对照实验)")
    p.add_argument("--save", default="outputs/mnist_cnn.pt")
    return p.parse_args()


def get_device(no_cuda: bool) -> torch.device:
    use_cuda = (not no_cuda) and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    if use_cuda:
        print(f"[info] device=cuda  GPU={torch.cuda.get_device_name(0)}  "
              f"cuda_version={torch.version.cuda}", flush=True)
    else:
        if not no_cuda:
            print("[WARN] CUDA not available, falling back to CPU", flush=True)
        else:
            print("[info] device=cpu (--no-cuda)", flush=True)
    return device


def get_data(batch_size: int) -> DataLoader:
    try:
        from torchvision import transforms
        from torchvision.datasets import MNIST
        ds = MNIST(
            "data", train=True, download=True,
            transform=transforms.ToTensor(),
        )
        print(f"[info] MNIST loaded: {len(ds)} samples", flush=True)
    except Exception as e:
        print(f"[WARN] MNIST download failed ({e}), fallback to synthetic data", flush=True)
        # 合成 6000 张 28x28 单通道图 + 随机标签
        x = torch.randn(6000, 1, 28, 28)
        y = torch.randint(0, 10, (6000,))
        ds = TensorDataset(x, y)
    return DataLoader(ds, batch_size=batch_size, shuffle=True)


class SmallCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 16, 3, padding=1)
        self.conv2 = nn.Conv2d(16, 32, 3, padding=1)
        self.pool = nn.MaxPool2d(2)
        self.fc = nn.Linear(32 * 7 * 7, 10)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))   # 28 -> 14
        x = self.pool(F.relu(self.conv2(x)))   # 14 -> 7
        x = x.flatten(1)
        return self.fc(x)


def mem_report(device: torch.device) -> str:
    if device.type != "cuda":
        return "mem=n/a"
    alloc = torch.cuda.memory_allocated(0) / 1024 ** 2
    reserved = torch.cuda.memory_reserved(0) / 1024 ** 2
    return f"mem_alloc={alloc:.1f}MB mem_reserved={reserved:.1f}MB"


def train_one_epoch(model, loader, optimizer, device):
    model.train()
    total_loss, n = 0.0, 0
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        optimizer.zero_grad()
        loss = F.cross_entropy(model(x), y)
        loss.backward()
        optimizer.step()
        total_loss += loss.item() * x.size(0)
        n += x.size(0)
    return total_loss / max(n, 1)


def main():
    args = parse_args()
    device = get_device(args.no_cuda)
    loader = get_data(args.batch_size)

    model = SmallCNN().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    print(f"[info] model params={sum(p.numel() for p in model.parameters())/1e3:.1f}K "
          f"epochs={args.epochs} batch={args.batch_size}", flush=True)

    t0 = time.time()
    for epoch in range(1, args.epochs + 1):
        te = time.time()
        avg_loss = train_one_epoch(model, loader, optimizer, device)
        dt = time.time() - te
        print(f"[epoch {epoch}/{args.epochs}] loss={avg_loss:.4f} "
              f"time={dt:.1f}s {mem_report(device)}", flush=True)

    total = time.time() - t0
    print(f"TOTAL TIME: {total:.1f}s", flush=True)

    os.makedirs(os.path.dirname(args.save) or ".", exist_ok=True)
    torch.save(model.state_dict(), args.save)
    print(f"[info] model saved -> {args.save}", flush=True)


if __name__ == "__main__":
    main()
