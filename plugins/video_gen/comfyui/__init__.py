"""Local ComfyUI video generation backend for Hermes Agent.

Routes text-to-video and image-to-video requests through ``/home/agent/AI/comfy/comfy_api.py``
and ``/home/agent/AI/comfy/workflows/api/workflows.json``, defaulting to RTX 3090
(``http://127.0.0.1:8189``).

Output directory routing:
  - Signal DM (or DuetPrivateStatus) from +12197418383 -> ~/hermes-data-private/output
  - All other requests -> ~/hermes-data/output
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from agent.video_gen_provider import (
    COMMON_ASPECT_RATIOS,
    COMMON_RESOLUTIONS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    VideoGenProvider,
    error_response,
    save_bytes_video,
    success_response,
)

logger = logging.getLogger(__name__)

_COMFY_API_MTIME: Optional[float] = None


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
    """Load comfy_api.py dynamically, reloading if the file changed on disk."""
    global _COMFY_API_MTIME
    mod_name = "_hermes_comfy_api"
    api_path = _resolve_comfy_api_path()
    try:
        mtime = api_path.stat().st_mtime
    except OSError:
        mtime = None
    if mod_name in sys.modules and _COMFY_API_MTIME == mtime:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(api_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load ComfyUI runner at {api_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    _COMFY_API_MTIME = mtime
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


def _load_video_gen_comfy_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        vcfg = cfg.get("video_gen") if isinstance(cfg, dict) else None
        sub = vcfg.get("comfyui") if isinstance(vcfg, dict) else None
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


class ComfyUIVideoGenProvider(VideoGenProvider):
    """Local ComfyUI video generation provider (RTX 3090 default)."""

    name = "comfyui"
    display_name = "ComfyUI (Local 3090)"

    def _cfg(self) -> dict[str, Any]:
        return _load_video_gen_comfy_config()

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
        rows: list[dict[str, Any]] = [
            {
                "id": "auto",
                "display": "Auto (ComfyUI Video Router)",
                "speed": "~60-180s",
                "strengths": "Routes text-to-video and image-to-video workflows from workflows/api/workflows.json",
                "price": "local (RTX 3090)",
                "tier": "local",
                "modalities": ["text", "image"],
            }
        ]
        try:
            comfy_api = _load_comfy_api()
            reg = comfy_api.load_registry()
            for wf_id, meta in (reg.get("workflows") or {}).items():
                if isinstance(meta, dict) and meta.get("media_type") == "video":
                    rows.append({
                        "id": wf_id,
                        "display": meta.get("display", wf_id),
                        "speed": meta.get("speed", "~90s"),
                        "strengths": meta.get("strengths", ""),
                        "price": "local (RTX 3090)",
                        "tier": "local",
                        "modalities": ["image"] if "image" in str(meta.get("modality", "")) else ["text", "image"],
                    })
        except Exception:
            pass
        return rows

    def default_model(self) -> Optional[str]:
        return "auto"

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "ComfyUI (Local)",
            "badge": "free",
            "tag": "Local RTX 3090 ComfyUI — extensible text-to-video & image-to-video workflows",
            "env_vars": [],
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": list(COMMON_ASPECT_RATIOS),
            "resolutions": list(COMMON_RESOLUTIONS),
            "max_duration": 15,
            "min_duration": 1,
            "supports_audio": False,
            "audio_always_on": True,
            "supports_negative_prompt": True,
            "supports_seed": True,
            "supports_upscale": False,
            "max_reference_images": 2,
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        prompt_clean = (prompt or "").strip()
        if not prompt_clean:
            return error_response(
                error="prompt is required",
                error_type="invalid_request",
                provider=self.name,
                aspect_ratio=aspect_ratio,
            )

        try:
            comfy_api = _load_comfy_api()
        except Exception as exc:
            return error_response(
                error=f"Failed to load ComfyUI workflow engine: {exc}",
                error_type="internal_error",
                provider=self.name,
                prompt=prompt_clean,
                aspect_ratio=aspect_ratio,
            )

        primary_image = image_url.strip() if isinstance(image_url, str) and image_url.strip() else None
        extra_refs = list(reference_image_urls or [])
        if not primary_image and not extra_refs:
            turn_images = _get_turn_attached_images()
            if turn_images:
                primary_image = turn_images[0]
                extra_refs = turn_images[1:]

        cfg = self._cfg()
        explicit_model = model or cfg.get("model")
        workflow_override = (
            explicit_model
            if isinstance(explicit_model, str) and explicit_model.strip() not in ("", "auto")
            else None
        )
        instance = str(kwargs.get("instance") or self._instance_or_url()).strip()

        try:
            res = comfy_api.run_comfy_workflow(
                prompt=prompt_clean,
                media_type="video",
                image_source=primary_image,
                reference_images=extra_refs,
                workflow_name=workflow_override,
                aspect_ratio=aspect_ratio,
                duration=duration,
                negative_prompt=negative_prompt or cfg.get("negative_prompt"),
                seed=seed,
                instance=instance,
                timeout=float(cfg.get("timeout", 900.0)),
            )
            direct_path = res.get("path")
            if direct_path and Path(direct_path).is_file():
                saved_path = Path(direct_path)
            else:
                saved_path = save_bytes_video(
                    res["bytes"],
                    prefix="comfyui",
                    extension=res.get("extension") or "mp4",
                )
        except Exception as exc:
            err_type = getattr(exc, "error_type", "api_error")
            logger.warning("ComfyUI video generation failed: %s", exc, exc_info=True)
            return error_response(
                error=str(exc),
                error_type=err_type,
                provider=self.name,
                model=workflow_override or "auto",
                prompt=prompt_clean,
                aspect_ratio=aspect_ratio,
            )

        effective_duration = round(float(res.get("duration") or duration or 5))
        return success_response(
            video=str(saved_path),
            model=res["workflow"],
            prompt=prompt_clean,
            modality=res["modality"],
            aspect_ratio=aspect_ratio,
            duration=effective_duration,
            provider=self.name,
            extra={
                "workflow": res["workflow"],
                "seed": res["seed"],
                "duration": res.get("duration", effective_duration),
                "prompt_id": res["prompt_id"],
                "base_url": res["base_url"],
                "output_dir": res.get("output_dir"),
                "private": res.get("private", False),
                "effective_prompt": res.get("effective_prompt", prompt_clean),
                "media_tag": f"MEDIA:{saved_path}",
            },
        )


def register(ctx) -> None:
    """Register ComfyUI video generation backend."""
    ctx.register_video_gen_provider(ComfyUIVideoGenProvider())
