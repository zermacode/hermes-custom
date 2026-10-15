"""Local ComfyUI image generation and editing backend for Hermes Agent.

Routes automatically based on input modality:
  - If an image is passed via ``image_url`` / ``reference_image_urls`` OR attached to the
    current turn on Signal/messaging (``_native_turn_images``), runs ``qwen-image-edit.json``.
  - If text-only, runs ``qwen-image-text.json``.
Defaults to the local RTX 3090 ComfyUI instance (``http://127.0.0.1:8189``).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from agent import provider_media
from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    success_response,
)
from plugins.image_gen._common import load_image_gen_config

logger = logging.getLogger(__name__)

_GENERATED_IMAGE_KIND = f"{provider_media.GENERATED_SUBDIR}/images"


def _resolve_comfy_api_path() -> Path:
    env_path = os.environ.get("HERMES_COMFY_API")
    if env_path and Path(env_path).expanduser().is_file():
        return Path(env_path).expanduser()
    candidates = (
        Path.home() / "AI" / "comfy" / "comfy_api.py",
        Path.home() / "hermes-custom" / "comfy" / "comfy_api.py",
        Path.home() / ".hermes" / "comfy" / "comfy_api.py",
    )
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def _load_comfy_api():
    """Load comfy_api.py dynamically."""
    mod_name = "_hermes_comfy_api"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    api_path = _resolve_comfy_api_path()
    spec = importlib.util.spec_from_file_location(mod_name, str(api_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load ComfyUI runner at {api_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_turn_attached_images() -> list[str]:
    """Return local file paths of any images attached by the user in the current turn."""
    try:
        from tools.vision_tools_history_budget import _native_turn_images

        turn_imgs = _native_turn_images.get()
        if not turn_imgs:
            return []
        paths: list[str] = []
        for item in sorted(turn_imgs):
            if not isinstance(item, str) or not item.strip():
                continue
            cleaned = item.strip()
            if cleaned.startswith("file://"):
                cleaned = cleaned[7:]
            elif cleaned.startswith("file:"):
                cleaned = cleaned[5:]
            if Path(cleaned).is_file():
                paths.append(cleaned)
        return paths
    except Exception:
        return []


class ComfyUIImageGenProvider(ImageGenProvider):
    """Local ComfyUI image generation & editing provider (RTX 3090 default)."""

    name = "comfyui"
    display_name = "ComfyUI (Local 3090)"

    def _cfg(self) -> dict[str, Any]:
        return load_image_gen_config("comfyui")

    def _instance_or_url(self) -> str:
        cfg = self._cfg()
        return str(cfg.get("base_url") or cfg.get("instance") or "3090").strip()

    def is_available(self) -> bool:
        try:
            comfy_api = _load_comfy_api()
            return bool(comfy_api.is_server_reachable(self._instance_or_url()))
        except Exception:
            return _resolve_comfy_api_path().is_file()

    def list_models(self) -> list[dict[str, Any]]:
        try:
            comfy_api = _load_comfy_api()
            reg = comfy_api.load_registry()
            rows: list[dict[str, Any]] = [
                {
                    "id": "auto",
                    "display": "Auto (qwen-image-text / qwen-image-edit)",
                    "speed": "~20-40s",
                    "strengths": "Routes text-only to qwen-image-text and attached images to qwen-image-edit",
                    "price": "local (RTX 3090)",
                }
            ]
            for wf_id, meta in (reg.get("workflows") or {}).items():
                if isinstance(meta, dict) and meta.get("media_type", "image") == "image":
                    rows.append({
                        "id": wf_id,
                        "display": meta.get("display", wf_id),
                        "speed": meta.get("speed", "~30s"),
                        "strengths": meta.get("strengths", ""),
                        "price": "local (RTX 3090)",
                    })
            return rows
        except Exception:
            return [
                {
                    "id": "auto",
                    "display": "Auto (qwen-image-text / qwen-image-edit)",
                    "speed": "~20-40s",
                    "strengths": "Local RTX 3090 ComfyUI workflow router",
                    "price": "local",
                }
            ]

    def default_model(self) -> Optional[str]:
        return "auto"

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "ComfyUI (Local)",
            "badge": "free",
            "tag": "Local RTX 3090 ComfyUI — auto-routes text to qwen-image-text and attached images to qwen-image-edit",
            "env_vars": [],
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "max_reference_images": 4,
            "supports_upscale": False,
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        aspect = resolve_aspect_ratio(aspect_ratio)
        prompt_clean = (prompt or "").strip()
        if not prompt_clean:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=self.name,
                aspect_ratio=aspect,
            )

        try:
            comfy_api = _load_comfy_api()
        except Exception as exc:
            return error_response(
                error=f"Failed to load ComfyUI workflow engine: {exc}",
                error_type="internal_error",
                provider=self.name,
                prompt=prompt_clean,
                aspect_ratio=aspect,
            )

        # Collect explicit image inputs first; fall back to images attached on the current Signal/chat turn.
        primary_image = image_url.strip() if isinstance(image_url, str) and image_url.strip() else None
        extra_refs = normalize_reference_images(reference_image_urls) or []
        if not primary_image and not extra_refs:
            turn_images = _get_turn_attached_images()
            if turn_images:
                primary_image = turn_images[0]
                extra_refs = turn_images[1:]
                logger.info(
                    "ComfyUI image_gen auto-detected turn-attached image %s -> routing to image edit workflow",
                    primary_image,
                )

        cfg = self._cfg()
        explicit_model = kwargs.get("model") or cfg.get("model")
        workflow_override = (
            explicit_model
            if isinstance(explicit_model, str) and explicit_model.strip() not in ("", "auto")
            else None
        )

        try:
            res = comfy_api.run_comfy_workflow(
                prompt=prompt_clean,
                media_type="image",
                image_source=primary_image,
                reference_images=extra_refs,
                workflow_name=workflow_override,
                aspect_ratio=aspect,
                negative_prompt=kwargs.get("negative_prompt") or cfg.get("negative_prompt"),
                seed=kwargs.get("seed"),
                instance=self._instance_or_url(),
                timeout=float(cfg.get("timeout", 600.0)),
            )
            saved_path = provider_media.save_bytes(
                _GENERATED_IMAGE_KIND,
                res["bytes"],
                prefix="comfyui",
                extension=res.get("extension") or "png",
            )
        except Exception as exc:
            err_type = getattr(exc, "error_type", "api_error")
            logger.warning("ComfyUI image generation failed: %s", exc, exc_info=True)
            return error_response(
                error=str(exc),
                error_type=err_type,
                provider=self.name,
                model=workflow_override or ("qwen-image-edit" if primary_image else "qwen-image-text"),
                prompt=prompt_clean,
                aspect_ratio=aspect,
            )

        return success_response(
            image=str(saved_path),
            model=res["workflow"],
            prompt=prompt_clean,
            aspect_ratio=aspect,
            provider=self.name,
            modality=res["modality"],
            extra={
                "workflow": res["workflow"],
                "seed": res["seed"],
                "prompt_id": res["prompt_id"],
                "base_url": res["base_url"],
            },
        )


def register(ctx) -> None:
    """Register ComfyUI image generation backend."""
    ctx.register_image_gen_provider(ComfyUIImageGenProvider())
