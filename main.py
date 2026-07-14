"""
kaggle-runner: FastAPI sidecar that wraps the official kaggle Python lib
so n8n can trigger Kaggle script runs via a simple HTTP POST.

Endpoints:
    POST /push    -> create/update + run a Kaggle script (kernel)
    GET  /status  -> poll a kernel's run status
    GET  /output  -> fetch a kernel's output files (logs + artifacts)
    GET  /health  -> liveness check

Env vars required:
    KAGGLE_USERNAME
    KAGGLE_KEY
"""

import json
import os
import tempfile
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# The kaggle lib reads credentials from env vars OR ~/.kaggle/kaggle.json.
# Setting env vars is enough; KaggleApi.authenticate() picks them up.
from kaggle.api.kaggle_api_extended import KaggleApi

app = FastAPI(title="kaggle-runner", version="0.1.0")


def _api() -> KaggleApi:
    api = KaggleApi()
    api.authenticate()
    return api


def _username() -> str:
    user = os.environ.get("KAGGLE_USERNAME")
    if not user:
        raise HTTPException(status_code=500, detail="KAGGLE_USERNAME not set")
    return user


# ---------------------------------------------------------------------------
# /push
# ---------------------------------------------------------------------------

class PushReq(BaseModel):
    slug: str = Field(..., description="Lowercase-dashed slug, e.g. 'rpg-scribe-bot'")
    title: str = Field(..., description="Human title; must slugify to `slug` (5-50 chars)")
    code: str = Field(..., description="Python source to run on Kaggle")
    enable_gpu: bool = True
    enable_internet: bool = True
    is_private: bool = True
    kernel_type: str = Field("script", description="'script' or 'notebook'")
    language: str = "python"
    dataset_sources: list[str] = []
    competition_sources: list[str] = []
    kernel_sources: list[str] = []
    model_sources: list[str] = []
    docker_image_pinning_type: str = "original"  # pin to creation-time env; set "latest" once Kaggle fixes #1546


@app.post("/push")
def push(req: PushReq) -> dict[str, Any]:
    username = _username()
    api = _api()

    code_file = "main.py" if req.kernel_type == "script" else "notebook.ipynb"

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, code_file), "w", encoding="utf-8") as f:
            f.write(req.code)

        meta = {
            "id": f"{username}/{req.slug}",
            "title": req.title,
            "code_file": code_file,
            "language": req.language,
            "kernel_type": req.kernel_type,
            # kaggle lib expects these as string booleans
            "is_private": str(req.is_private).lower(),
            "enable_gpu": str(req.enable_gpu).lower(),
            "enable_internet": str(req.enable_internet).lower(),
            "dataset_sources": req.dataset_sources,
            "competition_sources": req.competition_sources,
            "kernel_sources": req.kernel_sources,
            "model_sources": req.model_sources,
            "docker_image_pinning_type": req.docker_image_pinning_type,
        }
        with open(os.path.join(d, "kernel-metadata.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)

        try:
            result = api.kernels_push(d)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"kaggle push failed: {e}") from e

        # `result` is a KernelPushResponse; coerce to a plain dict for JSON return.
        return {
            "ref": getattr(result, "ref", None),
            "url": getattr(result, "url", None),
            "version_number": getattr(result, "versionNumber", None),
            "error": getattr(result, "error", None),
            "invalid_tags": getattr(result, "invalidTags", None),
        }


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

@app.get("/status")
def status(slug: str) -> dict[str, Any]:
    """Get current run status of `<username>/<slug>`."""
    username = _username()
    api = _api()
    try:
        r = api.kernels_status(f"{username}/{slug}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"status failed: {e}") from e
    # KernelStatusResponse -> dict
    return {
        "status": getattr(r, "status", None),
        "failure_message": getattr(r, "failureMessage", None),
    }


# ---------------------------------------------------------------------------
# /output
# ---------------------------------------------------------------------------

@app.get("/output")
def output(slug: str) -> dict[str, Any]:
    """Download a kernel's outputs to a temp dir and return file list + log text."""
    username = _username()
    api = _api()
    with tempfile.TemporaryDirectory() as d:
        try:
            api.kernels_output(f"{username}/{slug}", path=d)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"output fetch failed: {e}") from e

        files = []
        log_text = None
        for name in os.listdir(d):
            full = os.path.join(d, name)
            size = os.path.getsize(full)
            files.append({"name": name, "size": size})
            if name.endswith(".log") and size < 1_000_000:
                with open(full, encoding="utf-8", errors="replace") as f:
                    log_text = f.read()
        return {"files": files, "log": log_text}


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
