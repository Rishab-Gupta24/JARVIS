"""Train one LoRA adapter for one skill (QLoRA: frozen 4-bit base + small trainable adapter).

    .venv-train\\Scripts\\python -m jarvis.training.train --skill python
    .venv-train\\Scripts\\python -m jarvis.training.train --skill web --max-steps 3   # quick test

A plain PyTorch loop over transformers + PEFT + bitsandbytes. Unsloth / TRL / HF datasets
aren't used: they need pyarrow's dataset DLL, which Windows Smart App Control blocks here.
To fit a 7B model in 8 GB: 4-bit weights, the input embedding on the CPU, gradient
checkpointing, and the loss computed only on answer tokens, a few hundred at a time
(the full 152k-word vocabulary logits would otherwise take ~1 GB).

Every adapter uses the same rank and target layers so merge.py can combine them.
Needs the GPU to itself: stop Jarvis first (its models hold ~6 GB of the 8 GB).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("HF_HOME", str(ROOT / "data" / "hf"))  # model downloads stay in data/, not the profile
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

BASE = "unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit"  # standard bitsandbytes 4-bit checkpoint
SETS = ROOT / "data" / "training" / "sets"
ADAPTERS = ROOT / "data" / "training" / "adapters"
RANK = 16
TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
SYSTEM = "You are a careful senior software engineer. Answer with correct, complete code."


def load_rows(path: Path, limit: int | None = None, seed: int = 7) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    random.Random(seed).shuffle(rows)
    return rows[:limit] if limit else rows


def encode(tok, ex: dict, max_len: int) -> tuple[list[int], int] | None:
    """Token ids of the whole chat, and where the answer starts. None if too long (never cut)."""
    prompt = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ex["instruction"]}]
    head_text = tok.apply_chat_template(prompt, tokenize=False, add_generation_prompt=True)
    full_text = tok.apply_chat_template(prompt + [{"role": "assistant", "content": ex["response"]}], tokenize=False)
    head = tok(head_text, add_special_tokens=False)["input_ids"]
    full = tok(full_text, add_special_tokens=False)["input_ids"]
    if len(full) > max_len or full[:len(head)] != head:
        return None
    return full, len(head)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--max-examples", type=int, default=600, help="keeps the skills balanced and the run short")
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--max-steps", type=int, default=-1, help="optimizer steps; for quick tests")
    ap.add_argument("--seq", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=5e-5)  # 2e-4 wrecked general coding (7 Nov 2026)
    ap.add_argument("--accum", type=int, default=8)
    args = ap.parse_args()

    import torch
    import torch.nn.functional as F
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

    tok = AutoTokenizer.from_pretrained(args.base)
    train = [e for e in (encode(tok, r, args.seq) for r in load_rows(SETS / f"{args.skill}.jsonl")) if e]
    train = train[:args.max_examples]
    held = [e for e in (encode(tok, r, args.seq) for r in load_rows(SETS / f"{args.skill}.eval.jsonl")) if e]
    # small sets (games) get more passes so every skill gets a similar amount of training
    epochs = args.epochs if len(train) >= 400 else max(args.epochs, min(2.0, 400 / max(1, len(train))))  # 4 passes overfit

    device_map = {"model.embed_tokens": "cpu", "model.layers": 0, "model.norm": 0, "model.rotary_emb": 0,
                  "lm_head": 0}
    # The checkpoint's own 4-bit config wins over one passed in, so switch CPU offload on inside it:
    # that lets the (unquantized) input embedding live on the CPU.
    config = AutoConfig.from_pretrained(args.base)
    qc = config.quantization_config
    if isinstance(qc, dict):
        qc["llm_int8_enable_fp32_cpu_offload"] = True
    else:
        qc.llm_int8_enable_fp32_cpu_offload = True
    model = AutoModelForCausalLM.from_pretrained(args.base, config=config, device_map=device_map,
                                                 dtype=torch.bfloat16)
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True,
                                            gradient_checkpointing_kwargs={"use_reentrant": False})
    model = get_peft_model(model, LoraConfig(r=RANK, lora_alpha=RANK, lora_dropout=0.05, bias="none",
                                             target_modules=TARGETS, task_type="CAUSAL_LM"))
    causal = model.get_base_model()
    model.print_trainable_parameters()

    def loss_of(ids: list[int], answer_from: int) -> torch.Tensor:
        x = torch.tensor([ids])
        hidden = causal.model(input_ids=x).last_hidden_state[0]  # (T, H) on the GPU
        targets = torch.tensor(ids[answer_from:], device=hidden.device)
        h = hidden[answer_from - 1:len(ids) - 1]  # position t predicts token t+1
        total = torch.zeros((), device=hidden.device, dtype=torch.float32)
        for i in range(0, len(targets), 256):  # never the whole vocabulary for every position at once
            logits = causal.lm_head(h[i:i + 256]).float()
            total = total + F.cross_entropy(logits, targets[i:i + 256], reduction="sum")
        return total / len(targets)

    @torch.no_grad()
    def evaluate() -> float:
        model.eval()
        losses = [loss_of(ids, start).item() for ids, start in held]
        model.train()
        return sum(losses) / max(1, len(losses))

    params = [p for p in model.parameters() if p.requires_grad]
    import bitsandbytes as bnb

    optim = bnb.optim.AdamW8bit(params, lr=args.lr, weight_decay=0.0)
    steps = math.ceil(len(train) * epochs / args.accum)
    if args.max_steps > 0:
        steps = min(steps, args.max_steps)
    sched = get_cosine_schedule_with_warmup(optim, max(1, steps // 20), steps)

    before = evaluate()
    print(f"{args.skill}: {len(train)} examples x {epochs:.1f} epochs = {steps} steps; held-out loss {before:.4f}",
          flush=True)
    model.train()
    started, order, step, running, skipped = time.time(), [], 0, [], 0
    rng = random.Random(7)
    while step < steps:
        if not order:
            order = list(range(len(train)))
            rng.shuffle(order)
        for _ in range(args.accum):
            if not order:
                break
            ids, start = train[order.pop()]
            try:
                loss = loss_of(ids, start)
                (loss / args.accum).backward()
                running.append(loss.item())
            except torch.OutOfMemoryError:  # 8 GB is tight: skip the odd very long example, don't crash
                loss = None
                skipped += 1
                torch.cuda.empty_cache()
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        optim.step()
        sched.step()
        optim.zero_grad(set_to_none=True)
        step += 1
        if step % 5 == 0 or step == steps:
            mins = (time.time() - started) / 60
            print(f"  step {step}/{steps}  loss {sum(running) / max(1, len(running)):.4f}  "
                  f"{mins:.1f} min (~{mins / step * (steps - step):.0f} min left)  "
                  f"vram {torch.cuda.max_memory_allocated() / 1024 ** 3:.2f} GB", flush=True)
            running = []
    after = evaluate()
    out = ADAPTERS / args.skill
    model.save_pretrained(str(out))
    report = {"skill": args.skill, "base": args.base, "lr": args.lr, "examples": len(train), "epochs": round(epochs, 2),
              "steps": steps, "skipped_out_of_memory": skipped, "held_out": len(held), "eval_loss_before": round(before, 4),
              "eval_loss_after": round(after, 4), "perplexity_before": round(math.exp(before), 2),
              "perplexity_after": round(math.exp(after), 2), "minutes": round((time.time() - started) / 60, 1),
              "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1024 ** 3, 2)}
    (out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
