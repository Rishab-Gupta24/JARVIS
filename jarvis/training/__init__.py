"""Fine-tuning a local coding model: datasets from trusted repos, one LoRA adapter per
skill, merged into one, benchmarked against the untouched base. Training itself runs in
the separate `.venv-train` (PyTorch + Unsloth); building datasets needs only the stdlib."""
