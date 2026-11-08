"""Combine the skill adapters into one with TIES merging, then compress it back to a LoRA.

    .venv-train\\Scripts\\python -m jarvis.training.merge python web games reverse

For every adapted layer: each skill's weight change dW = B @ A (times alpha / r) is trimmed
to its largest `density` share of entries, a sign is elected per entry from the summed
changes, and entries are averaged only over the skills that agree with it (TIES, Yadav
et al. 2023). That keeps one skill's update from cancelling another's. The merged dW is
compressed with a truncated SVD to rank `--rank` (B = U S, A = V^T), so the result is an
ordinary LoRA adapter that Ollama or PEFT can load.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = ROOT / "data" / "training" / "adapters"


def ties(deltas: list[torch.Tensor], weights: list[float], density: float) -> torch.Tensor:
    trimmed = []
    for d, w in zip(deltas, weights):
        k = max(1, int(d.numel() * density))
        cut = d.abs().flatten().kthvalue(d.numel() - k + 1).values  # keep the k largest magnitudes
        trimmed.append(torch.where(d.abs() >= cut, d, torch.zeros_like(d)) * w)
    stack = torch.stack(trimmed)
    sign = torch.sign(stack.sum(0))
    agree = (torch.sign(stack) == sign) & (stack != 0)
    return (stack * agree).sum(0) / agree.sum(0).clamp(min=1)


def merge(skills: list[str], weights: list[float], out: Path, rank: int = 64, density: float = 0.5,
          method: str = "ties", device: str = "cuda") -> dict:
    tensors = [load_file(str(ADAPTERS / s / "adapter_model.safetensors")) for s in skills]
    configs = [json.loads((ADAPTERS / s / "adapter_config.json").read_text()) for s in skills]
    scales = [c["lora_alpha"] / c["r"] for c in configs]
    modules = sorted(k[: -len(".lora_A.weight")] for k in tensors[0] if k.endswith(".lora_A.weight"))
    merged: dict[str, torch.Tensor] = {}
    kept = []
    for name in modules:
        deltas = []
        for t, s in zip(tensors, scales):
            a = t[f"{name}.lora_A.weight"].to(device, torch.float32)
            b = t[f"{name}.lora_B.weight"].to(device, torch.float32)
            deltas.append(s * (b @ a))
        if method == "ties":
            delta = ties(deltas, weights, density)
        else:  # plain weighted average
            delta = sum(w * d for w, d in zip(weights, deltas)) / sum(weights)
        u, sv, v = torch.svd_lowrank(delta, q=min(rank + 8, min(delta.shape)), niter=4)
        u, sv, v = u[:, :rank], sv[:rank], v[:, :rank]
        energy = float((sv ** 2).sum() / (delta ** 2).sum().clamp(min=1e-12))
        kept.append(energy)
        dtype = tensors[0][f"{name}.lora_A.weight"].dtype
        merged[f"{name}.lora_B.weight"] = (u * sv).to("cpu", dtype).contiguous()
        merged[f"{name}.lora_A.weight"] = v.T.to("cpu", dtype).contiguous()
    out.mkdir(parents=True, exist_ok=True)
    save_file(merged, str(out / "adapter_model.safetensors"), metadata={"format": "pt"})
    config = dict(configs[0], r=rank, lora_alpha=rank)  # scale 1: the SVD already holds the magnitude
    (out / "adapter_config.json").write_text(json.dumps(config, indent=2))
    for extra in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.json",
                  "merges.txt", "added_tokens.json"):
        src = ADAPTERS / skills[0] / extra
        if src.exists():
            shutil.copy2(src, out / extra)
    report = {"skills": skills, "weights": weights, "method": method, "density": density, "rank": rank,
              "layers": len(modules), "svd_energy_kept_min": round(min(kept), 4),
              "svd_energy_kept_mean": round(sum(kept) / len(kept), 4)}
    (out / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("skills", nargs="+")
    ap.add_argument("--weights", type=float, nargs="*", help="one per skill; default equal")
    ap.add_argument("--rank", type=int, default=64)
    ap.add_argument("--density", type=float, default=0.5)
    ap.add_argument("--method", choices=["ties", "linear"], default="ties")
    ap.add_argument("--out", default=str(ADAPTERS / "merged"))
    args = ap.parse_args()
    weights = args.weights or [1.0] * len(args.skills)
    print(json.dumps(merge(args.skills, weights, Path(args.out), args.rank, args.density, args.method), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
