"""Checks this node's local Ollama store to answer the one question the
whole role-detection design rests on: "do I have model X pulled locally?"
See docs/ARCHITECTURE.md, "Role determination -- self-declared, not
elected".

Reads Ollama's on-disk manifest/blob layout directly rather than calling
`ollama list`/`ollama show`, because resolving a model name to the actual
GGUF blob path llama-server needs for `-m` requires the manifest's layer
digests anyway -- `ollama list` doesn't expose those. Verified against a
real manifest on this machine (see docs/LOG.md) rather than guessed:

    ~/.ollama/models/manifests/registry.ollama.ai/library/<name>/<tag>
        -> JSON, layers[].mediaType == "application/vnd.ollama.image.model"
           is the GGUF blob, layers[].digest "sha256:<hex>" maps to
    ~/.ollama/models/blobs/sha256-<hex>   (dash, not colon, in the filename)
"""
import json
from pathlib import Path

OLLAMA_HOME = Path.home() / ".ollama"
MANIFESTS_DIR = OLLAMA_HOME / "models" / "manifests" / "registry.ollama.ai" / "library"
BLOBS_DIR = OLLAMA_HOME / "models" / "blobs"

GGUF_LAYER_MEDIA_TYPE = "application/vnd.ollama.image.model"


def _manifest_path(model: str) -> Path:
    # "name" or "name:tag" -- Ollama defaults to "latest" same as Docker.
    if ":" in model:
        name, tag = model.split(":", 1)
    else:
        name, tag = model, "latest"
    return MANIFESTS_DIR / name / tag


def has_model(model: str) -> bool:
    return _manifest_path(model).exists()


def resolve_blob_path(model: str) -> Path | None:
    """Returns the local GGUF blob path for `model`, or None if the model
    isn't pulled locally or its manifest doesn't have a model layer."""
    manifest_path = _manifest_path(model)
    if not manifest_path.exists():
        return None

    manifest = json.loads(manifest_path.read_text())
    for layer in manifest.get("layers", []):
        if layer.get("mediaType") == GGUF_LAYER_MEDIA_TYPE:
            digest = layer["digest"].replace(":", "-", 1)
            blob_path = BLOBS_DIR / digest
            return blob_path if blob_path.exists() else None
    return None


def list_local_models() -> list[str]:
    """All model names with a manifest on this node, "name:tag" form."""
    if not MANIFESTS_DIR.exists():
        return []
    models = []
    for name_dir in MANIFESTS_DIR.iterdir():
        if not name_dir.is_dir():
            continue
        for tag_file in name_dir.iterdir():
            if tag_file.is_file():
                models.append(f"{name_dir.name}:{tag_file.name}")
    return models
