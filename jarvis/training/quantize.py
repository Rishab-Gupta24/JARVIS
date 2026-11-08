"""Shrink an f16 GGUF to 4-bit so Ollama can run it in 8 GB.

    .venv-train\\Scripts\\python -m jarvis.training.quantize in.f16.gguf out.q4_1.gguf

Why this exists: Ollama 0.40 only quantizes safetensors imports, and those go to its MLX
engine, which doesn't support Qwen2 ("unsupported MLX architecture"). GGUF files go to the
llama.cpp engine but must be quantized before import. llama.cpp's own llama-quantize would
need a C++ build, so this uses its Python `gguf` library (data/training/llama.cpp/gguf-py),
which can write the classic block formats but not the K-quants: weight matrices become Q4_1
(5 bits per weight, about the size of the stock q4_K_M), the output layer Q8_0 (the layer
most sensitive to rounding), norms and biases stay F32.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GGUF_PY = ROOT / "data" / "training" / "llama.cpp" / "gguf-py"
CHUNK_ROWS = 4096


def plan(tensor, gguf, weight_type, output_type):
    """The quant type for one tensor, or None to copy it as it is."""
    if tensor.tensor_type not in (gguf.GGMLQuantizationType.F16, gguf.GGMLQuantizationType.F32,
                                  gguf.GGMLQuantizationType.BF16):
        return None
    if len(tensor.data.shape) < 2 or tensor.name.endswith("_norm.weight"):
        return None
    qtype = output_type if tensor.name == "output.weight" else weight_type
    block, _ = gguf.GGML_QUANT_SIZES[qtype]
    return qtype if tensor.data.shape[-1] % block == 0 else None


def quantize_file(src: Path, dst: Path, weight: str = "Q4_1", output: str = "Q8_0") -> dict:
    sys.path.insert(0, str(GGUF_PY))
    import numpy as np
    import gguf

    weight_type, output_type = gguf.GGMLQuantizationType[weight], gguf.GGMLQuantizationType[output]
    reader = gguf.GGUFReader(str(src))
    arch = reader.fields[gguf.Keys.General.ARCHITECTURE].contents()
    writer = gguf.GGUFWriter(str(dst), arch=arch, endianess=reader.endianess)
    file_type = getattr(gguf.LlamaFileType, f"MOSTLY_{weight}")
    for field in reader.fields.values():
        if field.name == gguf.Keys.General.ARCHITECTURE or field.name.startswith("GGUF."):
            continue
        val_type = field.types[0]
        sub_type = field.types[-1] if val_type == gguf.GGUFValueType.ARRAY else None
        value = int(file_type) if field.name == gguf.Keys.General.FILE_TYPE else field.contents()
        writer.add_key_value(field.name, value, val_type, sub_type=sub_type)

    plans = [(t, plan(t, gguf, weight_type, output_type)) for t in reader.tensors]
    for t, qtype in plans:
        if qtype is None:
            writer.add_tensor_info(t.name, t.data.shape, t.data.dtype, t.data.nbytes, t.tensor_type)
        else:
            block, size = gguf.GGML_QUANT_SIZES[qtype]
            shape = (*t.data.shape[:-1], t.data.shape[-1] // block * size)
            writer.add_tensor_info(t.name, shape, np.dtype(np.uint8), int(np.prod(shape)), qtype)
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_ti_data_to_file()

    started, counts = time.time(), {}
    for i, (t, qtype) in enumerate(plans, 1):
        if qtype is None:
            writer.write_tensor_data(t.data, tensor_endianess=reader.endianess)
            counts["kept"] = counts.get("kept", 0) + 1
            continue
        rows = t.data.reshape(-1, t.data.shape[-1])
        parts = [gguf.quants.quantize(np.asarray(rows[r:r + CHUNK_ROWS], dtype=np.float32), qtype)
                 for r in range(0, rows.shape[0], CHUNK_ROWS)]
        block, size = gguf.GGML_QUANT_SIZES[qtype]
        data = np.concatenate(parts).reshape(*t.data.shape[:-1], t.data.shape[-1] // block * size)
        writer.write_tensor_data(data, tensor_endianess=reader.endianess)
        counts[qtype.name] = counts.get(qtype.name, 0) + 1
        if i % 40 == 0:
            print(f"  {i}/{len(plans)} tensors, {(time.time() - started) / 60:.1f} min", flush=True)
    writer.close()
    return {"tensors": counts, "gb": round(dst.stat().st_size / 1024 ** 3, 2),
            "minutes": round((time.time() - started) / 60, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--weight", default="Q4_1", help="Q4_0, Q4_1, Q5_0, Q5_1 or Q8_0")
    ap.add_argument("--output", default="Q8_0")
    args = ap.parse_args()
    print(quantize_file(Path(args.src), Path(args.dst), args.weight, args.output), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
