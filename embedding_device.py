"""Select a safe PyTorch device for sentence-transformer embeddings."""

import os


# PyTorch reads this setting during initialization. Unsupported MPS operations
# fall back to CPU instead of failing an embedding job.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


def resolve_embedding_device() -> str:
    """Prefer Apple Metal, then CUDA, and fall back to CPU."""
    try:
        import torch
    except ImportError:
        return "cpu"

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"
