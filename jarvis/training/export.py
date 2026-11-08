"""Bake an adapter into full weights and import them into Ollama, shrunk to 4-bit.

    .venv-train\\Scripts\\python -m jarvis.training.export --adapter data/training/adapters/merged --name jarvis-coder
    .venv-train\\Scripts\\python -m jarvis.training.export --name jarvis-coder-base       # no adapter: the fair baseline

Ollama 0.40 no longer loads LoRA adapters, so: each 4-bit (NF4) weight of the base checkpoint
is unpacked with bitsandbytes, the adapter's change B @ A * (alpha / r) is added, and the result
is written as bf16 safetensors, one tensor at a time (never the whole 15 GB model in memory).
Ollama 0.40 sends safetensors imports to its MLX engine, which can't run Qwen2, so the folder is
converted to GGUF with llama.cpp's converter (data/training/llama.cpp), shrunk to Q4_1 by
quantize.py, and imported as a ready GGUF. The baseline goes through exactly the same steps
without an adapter, so the benchmark compares like with like.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("HF_HOME", str(ROOT / "data" / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

BASE = "unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit"
EXPORTS = ROOT / "data" / "training" / "export"
OLLAMA = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
QUANT_SUFFIXES = (".absmax", ".quant_map", ".nested_absmax", ".nested_quant_map",
                  ".quant_state.bitsandbytes__nf4", ".quant_state.bitsandbytes__fp4")
SHARD_BYTES = 2 * 1024 ** 3
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "added_tokens.json",
                   "special_tokens_map.json", "generation_config.json")


def adapter_deltas(adapter: Path | None):
    """module name -> (A, B, scale) from a PEFT adapter folder."""
    if adapter is None:
        return {}
    from safetensors.torch import load_file

    cfg = json.loads((adapter / "adapter_config.json").read_text())
    scale = cfg["lora_alpha"] / cfg["r"]
    t = load_file(str(adapter / "adapter_model.safetensors"))
    out = {}
    for key in t:
        if key.endswith(".lora_A.weight"):
            module = key[: -len(".lora_A.weight")].removeprefix("base_model.model.")
            out[module] = (t[key], t[key.replace("lora_A", "lora_B")], scale)
    return out


def bake(adapter: Path | None, out: Path, device: str = "cpu") -> dict:
    import torch
    from bitsandbytes.functional import QuantState, dequantize_4bit
    from huggingface_hub import snapshot_download
    from safetensors import safe_open
    from safetensors.torch import save_file

    src = Path(snapshot_download(BASE, local_files_only=True))
    deltas = adapter_deltas(adapter)
    used = set()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    weight_map, shard, size, n = {}, {}, 0, 0

    def flush():
        nonlocal shard, size, n
        if shard:
            n += 1
            name = f"model-{n:05d}.safetensors"
            save_file(shard, str(out / name), metadata={"format": "pt"})
            weight_map.update({k: name for k in shard})
            shard, size = {}, 0

    with safe_open(str(src / "model.safetensors"), "pt") as f:
        keys = set(f.keys())
        for name in sorted(keys):
            if name.endswith(QUANT_SUFFIXES):
                continue
            if f"{name}.quant_state.bitsandbytes__nf4" in keys:
                aux = {s.lstrip("."): f.get_tensor(name + s) for s in QUANT_SUFFIXES if name + s in keys}
                qs = QuantState.from_dict(aux, device=torch.device(device))
                w = dequantize_4bit(f.get_tensor(name).to(device), qs).to(torch.float32)
            else:
                w = f.get_tensor(name).to(device, torch.float32)
            module = name.removesuffix(".weight")
            if name.endswith(".weight") and module in deltas:
                a, b, scale = deltas[module]
                w += scale * (b.to(device, torch.float32) @ a.to(device, torch.float32))
                used.add(module)
            shard[name] = w.to("cpu", torch.bfloat16).contiguous()
            size += shard[name].numel() * 2
            if size >= SHARD_BYTES:
                flush()
    flush()
    if deltas and used != set(deltas):
        raise RuntimeError(f"adapter layers not found in the base: {sorted(set(deltas) - used)[:5]}")
    total = sum((out / s).stat().st_size for s in set(weight_map.values()))
    (out / "model.safetensors.index.json").write_text(json.dumps(
        {"metadata": {"total_size": total}, "weight_map": weight_map}, indent=2))
    config = json.loads((src / "config.json").read_text())
    config.pop("quantization_config", None)
    config["torch_dtype"] = "bfloat16"
    (out / "config.json").write_text(json.dumps(config, indent=2))
    for name in TOKENIZER_FILES:
        if (src / name).exists():
            shutil.copy2(src / name, out / name)
    return {"layers_with_adapter": len(used), "tensors": len(weight_map), "gb": round(total / 1024 ** 3, 1)}


LLAMA_CPP = ROOT / "data" / "training" / "llama.cpp"


def to_gguf(folder: Path, out: Path) -> None:
    """bf16 safetensors folder -> f16 GGUF, with llama.cpp's own converter."""
    env = {**os.environ, "PYTHONPATH": str(LLAMA_CPP / "gguf-py"), "PYTHONIOENCODING": "utf-8"}
    subprocess.run([sys.executable, str(LLAMA_CPP / "convert_hf_to_gguf.py"), str(folder), "--outtype", "f16",
                    "--outfile", str(out)], check=True, env=env)


def ollama_create(gguf_file: Path, name: str, like: str = "qwen2.5-coder:7b") -> None:
    """Import a quantized GGUF with the same chat template, system prompt and parameters as the stock model."""
    stock = subprocess.run([str(OLLAMA), "show", "--modelfile", like], capture_output=True, text=True,
                           check=True, encoding="utf-8").stdout
    keep = [line for line in stock.splitlines() if not line.startswith(("FROM ", "#"))]
    modelfile = gguf_file.with_suffix(".Modelfile")
    modelfile.write_text(f"FROM {gguf_file.as_posix()}\n" + "\n".join(keep) + "\n", encoding="utf-8")
    subprocess.run([str(OLLAMA), "create", name, "-f", str(modelfile)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", help="PEFT adapter folder; leave out for the baseline")
    ap.add_argument("--name", required=True, help="Ollama model name, e.g. jarvis-coder")
    ap.add_argument("--device", default="cpu", help="cpu (works while training uses the GPU) or cuda")
    ap.add_argument("--quant", default="Q4_1", help="weight format, see quantize.py")
    ap.add_argument("--skip-bake", action="store_true", help="reuse the bf16 folder from an earlier run")
    ap.add_argument("--keep", action="store_true", help="keep the bf16 folder and GGUF files afterwards")
    args = ap.parse_args()
    folder = EXPORTS / args.name
    f16, q4 = EXPORTS / f"{args.name}.f16.gguf", EXPORTS / f"{args.name}.{args.quant.lower()}.gguf"
    if not args.skip_bake:
        print(json.dumps(bake(Path(args.adapter) if args.adapter else None, folder, args.device)), flush=True)
    if not f16.exists():
        to_gguf(folder, f16)
    from jarvis.training.quantize import quantize_file

    if not q4.exists():
        print(json.dumps(quantize_file(f16, q4, args.quant)), flush=True)
    ollama_create(q4, args.name)
    if not args.keep:
        for p in [*folder.glob("*.safetensors"), f16, q4]:
            p.unlink(missing_ok=True)
    print(f"Ollama model {args.name} ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
