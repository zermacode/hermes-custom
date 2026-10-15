#!/usr/bin/env python3
"""Flexible ComfyUI API workflow runner and router for Hermes Agent & CLI.

Default instance: RTX 3090 (http://127.0.0.1:8189)
Workflows directory: /home/agent/AI/comfy/workflows/api/

Routing rules:
  - Image request with an input image attached -> image_edit workflow (qwen-image-edit.json)
  - Image request with text only              -> image_text workflow (qwen-image-text.json)
  - Video request with an input image attached -> video_image workflow (h3_fastvideo_edit.json)
  - Video request with text only              -> video_text workflow (h3_fastvideo_text.json)

Video features:
  - Duration: defaults to 5 seconds if omitted; clamped to [1, 15] seconds.
  - Prompt enhancement: automatically uses the `h3-prompt-writing` skill
    (/home/agent/.agents/skills/h3-prompt-writing) to rewrite/enhance video prompts
    (T2VA, I2VA, FL2VA, Ref2VA) before sending to ComfyUI.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import logging
import mimetypes
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

COMFY_ROOT = Path(__file__).resolve().parent
WORKFLOWS_API_DIR = COMFY_ROOT / "workflows" / "api"
REGISTRY_PATH = WORKFLOWS_API_DIR / "workflows.json"
DEFAULT_OUTPUT_DIR = COMFY_ROOT / "output_api"

H3_SKILL_DIRS = (
    Path.home() / ".agents" / "skills" / "h3-prompt-writing",
    Path.home() / ".hermes" / "skills" / "creative" / "h3-prompt-writing",
    COMFY_ROOT.parent / "skills" / "creative" / "h3-prompt-writing",
)
DEFAULT_LLM_BASE_URL = os.environ.get("HERMES_LLM_BASE_URL", "http://speedy:8080/v1")
DEFAULT_LLM_MODEL = os.environ.get("HERMES_LLM_MODEL", "roleplay")

DEFAULT_VIDEO_DURATION = 5
MIN_VIDEO_DURATION = 1
MAX_VIDEO_DURATION = 15

DEFAULT_ASPECT_MAP = {
    "square": "1:1 (Square)",
    "1:1": "1:1 (Square)",
    "landscape": "16:9 (Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "portrait": "2:3 (Portrait Photo)",
    "2:3": "2:3 (Portrait Photo)",
    "9:16": "9:16 (Portrait Widescreen)",
    "3:2": "3:2 (Photo)",
    "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)",
    "21:9": "21:9 (Ultrawide)",
}


class ComfyAPIError(RuntimeError):
    """Raised when ComfyUI workflow preparation, submission, or execution fails."""

    def __init__(self, message: str, error_type: str = "api_error") -> None:
        super().__init__(message)
        self.error_type = error_type


def load_registry(registry_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Load workflows/api/workflows.json registry."""
    if not registry_path.exists():
        return {
            "default_instance": "3090",
            "instances": {
                "3090": {"url": "http://127.0.0.1:8189"},
                "5090": {"url": "http://127.0.0.1:8188"},
            },
            "routing": {
                "image_text": "qwen-image-text",
                "image_edit": "qwen-image-edit",
                "video_text": "h3_fastvideo_text",
                "video_image": "h3_fastvideo_edit",
            },
            "workflows": {},
        }
    with open(registry_path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_base_url(
    instance_or_url: Optional[str] = None,
    registry: Optional[dict[str, Any]] = None,
) -> str:
    """Resolve an instance alias ('3090', '5090') or full URL to a ComfyUI base URL."""
    reg = registry or load_registry()
    env_url = os.environ.get("COMFYUI_BASE_URL", "").strip()
    env_instance = os.environ.get("COMFYUI_INSTANCE", "").strip()
    target = (instance_or_url or env_url or env_instance or reg.get("default_instance") or "3090").strip()
    if target.startswith(("http://", "https://")):
        return target.rstrip("/")
    instances = reg.get("instances") or {}
    if target in instances:
        url = instances[target].get("url") if isinstance(instances[target], dict) else str(instances[target])
        return url.rstrip("/")
    return "http://127.0.0.1:8189"


def is_server_reachable(base_url: Optional[str] = None, timeout: float = 3.0) -> bool:
    """Return True if the ComfyUI server responds to /system_stats."""
    url = f"{resolve_base_url(base_url)}/system_stats"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _read_image_source_bytes(source: str) -> tuple[bytes, str, str]:
    """Read an image from a local path, file: URI, data: URI, or http(s) URL.
    Returns (raw_bytes, filename, content_type)."""
    src = source.strip()
    if src.startswith("data:"):
        header, _, b64_part = src.partition(",")
        mime = "image/png"
        if ";" in header and ":" in header:
            mime = header.split(":", 1)[1].split(";", 1)[0].strip() or "image/png"
        ext = mimetypes.guess_extension(mime) or ".png"
        raw = base64.b64decode(b64_part)
        return raw, f"input_{uuid.uuid4().hex[:10]}{ext}", mime

    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "Hermes-ComfyUI/1.0"})
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            raw = resp.read()
            mime = resp.headers.get_content_type() or "image/png"
        ext = mimetypes.guess_extension(mime) or ".png"
        parsed_name = Path(urllib.parse.urlparse(src).path).name
        if not parsed_name or "." not in parsed_name:
            parsed_name = f"input_{uuid.uuid4().hex[:10]}{ext}"
        return raw, parsed_name, mime

    if src.startswith("file://"):
        local_path = Path(urllib.parse.unquote(urllib.parse.urlparse(src).path))
    elif src.startswith("file:"):
        local_path = Path(src[5:])
    else:
        local_path = Path(src).expanduser()

    if not local_path.is_file():
        raise ComfyAPIError(f"Source image file not found: {local_path}", error_type="invalid_input_image")

    raw = local_path.read_bytes()
    mime, _ = mimetypes.guess_type(str(local_path))
    mime = mime or "image/png"
    ext = local_path.suffix or (mimetypes.guess_extension(mime) or ".png")
    unique_name = f"signal_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    return raw, unique_name, mime


def upload_image(base_url: str, image_source: str, timeout: float = 30.0) -> str:
    """Upload an image to ComfyUI's /upload/image endpoint and return the server filename."""
    raw_bytes, filename, content_type = _read_image_source_bytes(image_source)
    boundary = f"----ComfyUploadBoundary{uuid.uuid4().hex}"
    body_parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
        raw_bytes,
        f"\r\n--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(body_parts)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/upload/image",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise ComfyAPIError(f"Failed to upload input image to ComfyUI: {exc}", error_type="upload_failed") from exc

    name = payload.get("name")
    subfolder = payload.get("subfolder", "")
    if not name:
        raise ComfyAPIError(f"ComfyUI /upload/image returned no filename: {payload}", error_type="upload_failed")
    return f"{subfolder}/{name}" if subfolder else str(name)


def resolve_video_duration(
    duration: Optional[int | float],
    wf_meta: Optional[dict[str, Any]] = None,
) -> int:
    """Resolve video duration in seconds: defaults to 5s, clamped to [1, 15]s."""
    meta = wf_meta or {}
    default_dur = int(meta.get("default_duration", DEFAULT_VIDEO_DURATION))
    min_dur = int(meta.get("min_duration", MIN_VIDEO_DURATION))
    max_dur = int(meta.get("max_duration", MAX_VIDEO_DURATION))
    if duration is None:
        return default_dur
    try:
        val = int(round(float(duration)))
    except (TypeError, ValueError):
        return default_dur
    if val <= 0:
        return default_dur
    return max(min_dur, min(max_dur, val))


def _find_h3_skill_dir() -> Optional[Path]:
    for d in H3_SKILL_DIRS:
        if (d / "SKILL.md").is_file():
            return d
    return None


def is_already_h3_formatted(prompt: str) -> bool:
    """Return True if the prompt is already structured in MiniMax H3 format."""
    p = (prompt or "").strip()
    has_body = ("integrated_multimodal_description:" in p) or ("detailed_description:" in p)
    has_audio = "overall_soundscape:" in p
    return has_body and has_audio


def _ensure_i2va_header(prompt: str, has_image: bool) -> str:
    """Ensure I2VA prompts start with the required <Picture 1> alignment line when an image is attached."""
    p = prompt.strip()
    if not has_image:
        return p
    if "subject_definitions:" in p or "For the target video," in p or "How the reference pictures align" in p:
        return p
    header = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
    return f"{header}\n\n{p}"


def _strip_llm_wrappers(text: str) -> str:
    """Strip <think>...</think> blocks and outer markdown code fences from LLM output."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def enhance_h3_video_prompt(
    prompt: str,
    *,
    duration: int = DEFAULT_VIDEO_DURATION,
    image_sources: Optional[list[str]] = None,
    mode: Optional[str] = None,
    llm_base_url: str = DEFAULT_LLM_BASE_URL,
    llm_model: str = DEFAULT_LLM_MODEL,
    timeout: float = 60.0,
) -> str:
    """Enhance a video prompt using the `h3-prompt-writing` skill guide and local LLM.

    Supports:
      - T2VA (text-to-video, no input image)
      - I2VA (image-to-video, 1 input image anchored as <Picture 1> at 0.00s)
      - Ref2VA (reference-to-video, when mode=='ref2va' or multiple reference images)
    """
    raw = (prompt or "").strip()
    imgs = [s for s in (image_sources or []) if isinstance(s, str) and s.strip()]
    has_image = len(imgs) > 0

    if is_already_h3_formatted(raw):
        return _ensure_i2va_header(raw, has_image)

    skill_dir = _find_h3_skill_dir()
    if not skill_dir:
        logger.warning("h3-prompt-writing skill directory not found; using fallback H3 formatting")
        return _fallback_h3_prompt(raw, has_image=has_image)

    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    resolved_mode = (mode or "").strip().lower()
    if not resolved_mode:
        if len(imgs) > 1 or "<subject" in raw.lower() or "ref2va" in raw.lower():
            resolved_mode = "ref2va"
        elif has_image:
            resolved_mode = "i2va"
        else:
            resolved_mode = "t2va"

    ref_file = "ref-en.txt" if resolved_mode == "ref2va" else "base-en.txt"
    guide_path = skill_dir / "references" / ref_file
    guide_text = guide_path.read_text(encoding="utf-8") if guide_path.is_file() else ""

    system_prompt = (
        "You are an expert MiniMax H3 video prompt writer applying the `h3-prompt-writing` skill.\n"
        "Rewrite the user's video request into the exact MiniMax H3 prompt format specified in the skill guide below.\n\n"
        "CRITICAL RULES:\n"
        f"1. Target video duration is exactly {duration} seconds ({duration:.2f}s). All shot timestamps (if multiple shots are used) "
        f"MUST be strictly less than 00:{duration:02d}.000 (e.g. for a 5s video, keep it to 1 shot or cut around 00:02.500).\n"
        f"2. Input mode is `{resolved_mode.upper()}`.\n"
        "3. For T2VA: output ONLY `integrated_multimodal_description:`, `overall_soundscape:`, and `non_diegetic_music:`.\n"
        "4. For I2VA: begin with the exact line:\n"
        "   `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`\n"
        "   followed by a blank line, then `integrated_multimodal_description: [Shot 1] ...` (anchoring <Picture 1>'s appearance, "
        "   clothing, composition, and scene before developing the requested motion/dialogue), `overall_soundscape:`, and `non_diegetic_music:`.\n"
        "5. For Ref2VA: output the six sections in order: `subject_definitions:`, `summary:`, `retention_analysis:`, "
        "`detailed_description:`, `overall_soundscape:`, `non_diegetic_music:`.\n"
        "6. Output ONLY the final rewritten H3 prompt text—no markdown code blocks, no preamble, no explanation.\n\n"
        f"--- SKILL.MD ---\n{skill_md}\n\n"
        f"--- REFERENCE GUIDE ({ref_file}) ---\n{guide_text}"
    )

    user_text = (
        f"Mode: {resolved_mode.upper()}\n"
        f"Target Duration: {duration} seconds\n"
        f"Number of attached images: {len(imgs)}\n"
        f"User video request: {raw}\n\n"
        "Write the complete MiniMax H3 prompt now:"
    )

    user_content: Any = user_text
    if imgs:
        parts: list[dict[str, Any]] = []
        for idx, img_src in enumerate(imgs[:4], start=1):
            try:
                img_bytes, _fname, mime = _read_image_source_bytes(img_src)
                b64_str = base64.b64encode(img_bytes).decode("ascii")
                parts.append({"type": "text", "text": f"Attached <Picture {idx}>:"})
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_str}"}})
            except Exception as exc:
                logger.warning("Could not attach image %s for H3 prompt enhancement: %s", img_src, exc)
        parts.append({"type": "text", "text": user_text})
        user_content = parts

    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.6,
        "max_tokens": 1200,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        req = urllib.request.Request(
            f"{llm_base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
        content = resp_data["choices"][0]["message"].get("content") or ""
        enhanced = _strip_llm_wrappers(content)
        if is_already_h3_formatted(enhanced):
            return _ensure_i2va_header(enhanced, has_image and resolved_mode == "i2va")
        logger.warning("LLM output did not match H3 structure; wrapping in H3 fields: %s", enhanced[:200])
        return _fallback_h3_prompt(enhanced or raw, has_image=has_image)
    except Exception as exc:
        logger.warning("H3 LLM prompt enhancement failed (%s); falling back to structured template", exc)
        return _fallback_h3_prompt(raw, has_image=has_image)


def _fallback_h3_prompt(raw: str, *, has_image: bool = False) -> str:
    """Deterministic fallback wrapper if the local LLM is unreachable."""
    body = (
        f"integrated_multimodal_description: [Shot 1] Cinematic, {'beginning from <Picture 1>, ' if has_image else ''}"
        f"{raw}\n\n"
        "overall_soundscape: Natural ambient room tone and synchronized physical movement sounds matching the scene.\n\n"
        "non_diegetic_music: N/A"
    )
    return _ensure_i2va_header(body, has_image)


def resolve_workflow(
    *,
    media_type: str = "image",
    has_image: bool = False,
    workflow_name: Optional[str] = None,
    registry: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Select and load the appropriate workflow JSON and metadata.
    Returns (workflow_id, workflow_meta, workflow_graph)."""
    reg = registry or load_registry()
    workflows = reg.get("workflows") or {}
    routing = reg.get("routing") or {}

    if workflow_name:
        wf_id = workflow_name.removesuffix(".json")
    else:
        route_key = f"{media_type}_{'edit' if (media_type == 'image' and has_image) else ('image' if has_image else 'text')}"
        wf_id = routing.get(route_key)
        if not wf_id and media_type == "video":
            candidates = sorted(WORKFLOWS_API_DIR.glob("*video*.json"))
            for cand in candidates:
                stem = cand.stem.lower()
                if has_image and any(tag in stem for tag in ("i2v", "img", "image", "edit")):
                    wf_id = cand.stem
                    break
                if not has_image and any(tag in stem for tag in ("t2v", "text", "txt")):
                    wf_id = cand.stem
                    break
            if not wf_id and candidates:
                wf_id = candidates[0].stem

    if not wf_id:
        modality_desc = f"{media_type} ({'with input image' if has_image else 'text-only'})"
        raise ComfyAPIError(
            f"No ComfyUI workflow configured for {modality_desc}. "
            f"Add a workflow JSON to {WORKFLOWS_API_DIR} and register it in {REGISTRY_PATH.name}.",
            error_type="workflow_not_configured",
        )

    wf_meta = copy.deepcopy(workflows.get(wf_id) or {})
    wf_file = wf_meta.get("file") or f"{wf_id}.json"
    wf_path = Path(wf_file) if Path(wf_file).is_absolute() else (WORKFLOWS_API_DIR / wf_file)
    if not wf_path.is_file():
        raise ComfyAPIError(f"Workflow file not found: {wf_path}", error_type="workflow_not_found")

    with open(wf_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    return wf_id, wf_meta, graph


def _set_node_targets(graph: dict[str, Any], targets: list[dict[str, str]], value: Any) -> int:
    """Apply a value to a list of {'node': id, 'input': field} targets. Returns count applied."""
    applied = 0
    for target in targets:
        node_id = str(target.get("node", ""))
        input_key = str(target.get("input", ""))
        node = graph.get(node_id)
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict) and input_key:
            node["inputs"][input_key] = value
            applied += 1
    return applied


def _inject_parameters_heuristic(
    graph: dict[str, Any],
    *,
    prompt: Optional[str] = None,
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    duration: Optional[int | float] = None,
    aspect_ratio_label: Optional[str] = None,
    uploaded_images: Optional[list[str]] = None,
    applied_fields: set[str],
) -> None:
    """Fallback heuristic parameter injection by ComfyUI class_type for newly added workflows."""
    load_image_nodes: list[str] = []
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type", "")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        meta_title = str((node.get("_meta") or {}).get("title", "")).lower()

        if ctype == "LoadImage":
            load_image_nodes.append(node_id)

        if "prompt" not in applied_fields and prompt is not None:
            if ctype == "ComfySwitchNode" and isinstance(inputs.get("on_false"), str):
                inputs["on_false"] = prompt
            elif ctype in ("TextGenerate", "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo") and isinstance(inputs.get("prompt"), str):
                inputs["prompt"] = prompt
            elif ctype in ("CLIPTextEncode", "TextEncodeQwenImage21", "WanVideoTextEncode") and isinstance(inputs.get("text"), str):
                if "negative" not in meta_title:
                    inputs["text"] = prompt

        if "negative_prompt" not in applied_fields and negative_prompt is not None:
            if "negative_prompt" in inputs and isinstance(inputs["negative_prompt"], str):
                inputs["negative_prompt"] = negative_prompt

        if "seed" not in applied_fields and seed is not None:
            if "noise_seed" in inputs and isinstance(inputs["noise_seed"], int):
                inputs["noise_seed"] = seed
            elif "seed" in inputs and isinstance(inputs["seed"], int):
                inputs["seed"] = seed
            elif "sampling_mode.seed" in inputs and isinstance(inputs["sampling_mode.seed"], int):
                inputs["sampling_mode.seed"] = seed

        if "duration" not in applied_fields and duration is not None:
            if ctype in ("PrimitiveFloat", "PrimitiveInt") and "duration" in meta_title and "value" in inputs:
                inputs["value"] = duration

        if "aspect_ratio" not in applied_fields and aspect_ratio_label is not None:
            if ctype == "ResolutionSelector" and "aspect_ratio" in inputs:
                inputs["aspect_ratio"] = aspect_ratio_label

    if "image" not in applied_fields and uploaded_images and load_image_nodes:
        for idx, img_name in enumerate(uploaded_images):
            if idx < len(load_image_nodes):
                graph[load_image_nodes[idx]]["inputs"]["image"] = img_name


def prepare_workflow_graph(
    graph: dict[str, Any],
    wf_meta: dict[str, Any],
    *,
    prompt: str,
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    duration: Optional[int | float] = None,
    aspect_ratio: Optional[str] = None,
    uploaded_images: Optional[list[str]] = None,
    enhance_prompt: Optional[bool] = None,
    extra_node_overrides: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[dict[str, Any], int, str]:
    """Inject prompt, images, seed, duration, and aspect ratio into a deep copy of the workflow graph.
    Returns (prepared_graph, used_seed, resolved_aspect_label)."""
    prepared = copy.deepcopy(graph)
    nodes_cfg = wf_meta.get("nodes") or {}
    applied_fields: set[str] = set()

    used_seed = seed if isinstance(seed, int) else random.randint(100_000_000_000_000, 999_999_999_999_999)

    aspect_map = {**DEFAULT_ASPECT_MAP, **(wf_meta.get("aspect_ratio_map") or {})}
    aspect_key = (aspect_ratio or "landscape").strip().lower()
    aspect_label = aspect_map.get(aspect_key, aspect_ratio if aspect_ratio in aspect_map.values() else aspect_map["landscape"])

    if prompt:
        targets = nodes_cfg.get("prompt") or []
        if targets and _set_node_targets(prepared, targets, prompt) > 0:
            applied_fields.add("prompt")

    if enhance_prompt is not None:
        targets = nodes_cfg.get("enhance_prompt_switch") or []
        if targets:
            _set_node_targets(prepared, targets, bool(enhance_prompt))

    if negative_prompt is not None:
        targets = nodes_cfg.get("negative_prompt") or []
        if targets and _set_node_targets(prepared, targets, negative_prompt) > 0:
            applied_fields.add("negative_prompt")

    seed_targets = nodes_cfg.get("seed") or []
    if seed_targets and _set_node_targets(prepared, seed_targets, used_seed) > 0:
        applied_fields.add("seed")

    if duration is not None:
        dur_targets = nodes_cfg.get("duration") or []
        if dur_targets and _set_node_targets(prepared, dur_targets, duration) > 0:
            applied_fields.add("duration")

    if aspect_ratio is not None:
        ar_targets = nodes_cfg.get("aspect_ratio") or []
        if ar_targets and _set_node_targets(prepared, ar_targets, aspect_label) > 0:
            applied_fields.add("aspect_ratio")

    if uploaded_images:
        img_targets = nodes_cfg.get("image") or []
        if img_targets:
            for idx, target in enumerate(img_targets):
                img_val = uploaded_images[min(idx, len(uploaded_images) - 1)]
                _set_node_targets(prepared, [target], img_val)
            applied_fields.add("image")

    # Apply fallback heuristics for any field not explicitly mapped
    _inject_parameters_heuristic(
        prepared,
        prompt=prompt,
        negative_prompt=negative_prompt,
        seed=used_seed,
        duration=duration,
        aspect_ratio_label=aspect_label if aspect_ratio is not None else None,
        uploaded_images=uploaded_images,
        applied_fields=applied_fields,
    )

    # Apply any explicit per-node overrides
    if isinstance(extra_node_overrides, dict):
        for node_id, inputs_override in extra_node_overrides.items():
            if node_id in prepared and isinstance(prepared[node_id].get("inputs"), dict) and isinstance(inputs_override, dict):
                prepared[node_id]["inputs"].update(inputs_override)

    return prepared, used_seed, aspect_label


def submit_and_wait(
    base_url: str,
    prompt_graph: dict[str, Any],
    *,
    output_node_cfg: Optional[dict[str, Any]] = None,
    poll_interval: float = 1.0,
    timeout: float = 900.0,
) -> tuple[bytes, dict[str, Any]]:
    """Submit a workflow graph to ComfyUI /prompt, poll /history/{prompt_id}, and download output bytes.
    Returns (file_bytes, metadata_dict)."""
    base = base_url.rstrip("/")
    client_id = f"hermes-{uuid.uuid4().hex[:12]}"
    payload_bytes = json.dumps({"prompt": prompt_graph, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/prompt",
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            submit_resp = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise ComfyAPIError(f"ComfyUI rejected workflow (HTTP {exc.code}): {err_body[:500]}", error_type="invalid_workflow") from exc
    except Exception as exc:
        raise ComfyAPIError(f"Could not connect to ComfyUI at {base}: {exc}", error_type="connection_error") from exc

    prompt_id = submit_resp.get("prompt_id")
    if not prompt_id:
        raise ComfyAPIError(f"ComfyUI /prompt returned no prompt_id: {submit_resp}", error_type="api_error")

    deadline = time.monotonic() + timeout
    history_entry: Optional[dict[str, Any]] = None

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/history/{prompt_id}", timeout=10.0) as resp:
                hist_data = json.loads(resp.read().decode("utf-8"))
            if prompt_id in hist_data:
                entry = hist_data[prompt_id]
                status = entry.get("status") or {}
                status_str = status.get("status_str")
                if status_str == "error":
                    messages = status.get("messages") or []
                    raise ComfyAPIError(
                        f"ComfyUI execution failed for prompt {prompt_id}: {messages}",
                        error_type="execution_error",
                    )
                if status.get("completed") or entry.get("outputs"):
                    history_entry = entry
                    break
        except ComfyAPIError:
            raise
        except Exception:
            pass
        time.sleep(poll_interval)

    if history_entry is None:
        raise ComfyAPIError(f"ComfyUI job {prompt_id} timed out after {int(timeout)}s", error_type="timeout")

    outputs = history_entry.get("outputs") or {}
    file_info = _extract_output_file_info(outputs, output_node_cfg)
    if not file_info:
        raise ComfyAPIError(
            f"ComfyUI job {prompt_id} completed but produced no output media in {list(outputs.keys())}",
            error_type="empty_response",
        )

    query = urllib.parse.urlencode({
        "filename": file_info["filename"],
        "subfolder": file_info.get("subfolder", ""),
        "type": file_info.get("type", "output"),
    })
    view_url = f"{base}/view?{query}"
    try:
        with urllib.request.urlopen(view_url, timeout=60.0) as resp:
            media_bytes = resp.read()
    except Exception as exc:
        raise ComfyAPIError(f"Failed to download output from {view_url}: {exc}", error_type="download_error") from exc

    if not media_bytes:
        raise ComfyAPIError(f"Downloaded 0 bytes from {view_url}", error_type="empty_response")

    return media_bytes, {
        "prompt_id": prompt_id,
        "filename": file_info["filename"],
        "subfolder": file_info.get("subfolder", ""),
        "type": file_info.get("type", "output"),
        "view_url": view_url,
    }


def _extract_output_file_info(
    outputs: dict[str, Any],
    output_node_cfg: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Find the primary output file dict ({'filename', 'subfolder', 'type'}) from ComfyUI history outputs."""
    media_keys = ("images", "videos", "gifs", "audio")

    # 1. Check explicitly configured output node first
    if isinstance(output_node_cfg, dict):
        target_node = str(output_node_cfg.get("node", ""))
        target_field = str(output_node_cfg.get("field", ""))
        if target_node and target_node in outputs:
            node_out = outputs[target_node]
            if isinstance(node_out, dict):
                fields_to_check = [target_field] if target_field else list(media_keys)
                for key in fields_to_check:
                    items = node_out.get(key)
                    if isinstance(items, list) and items:
                        for item in items:
                            if isinstance(item, dict) and item.get("filename") and item.get("type") == "output":
                                return item
                        if isinstance(items[0], dict) and items[0].get("filename"):
                            return items[0]

    # 2. Scan all nodes for outputs with type == "output" (skips ImageCompare temp previews)
    for _node_id, node_out in outputs.items():
        if not isinstance(node_out, dict):
            continue
        for key in media_keys:
            items = node_out.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("filename") and item.get("type") == "output":
                        return item

    # 3. Last resort: any output item with a filename
    for _node_id, node_out in outputs.items():
        if not isinstance(node_out, dict):
            continue
        for key in media_keys:
            items = node_out.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("filename"):
                        return item
    return None


def run_comfy_workflow(
    prompt: str,
    *,
    media_type: str = "image",
    image_source: Optional[str] = None,
    reference_images: Optional[list[str]] = None,
    workflow_name: Optional[str] = None,
    duration: Optional[int | float] = None,
    aspect_ratio: Optional[str] = "landscape",
    negative_prompt: Optional[str] = None,
    seed: Optional[int] = None,
    enhance_prompt: Optional[bool] = None,
    instance: Optional[str] = None,
    output_path: Optional[Path | str] = None,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """High-level entry point to route, prepare, execute, and download a ComfyUI workflow."""
    registry = load_registry()
    base_url = resolve_base_url(instance, registry)

    all_images: list[str] = []
    if isinstance(image_source, str) and image_source.strip():
        all_images.append(image_source.strip())
    if reference_images:
        for ref in reference_images:
            if isinstance(ref, str) and ref.strip() and ref.strip() not in all_images:
                all_images.append(ref.strip())

    has_image = len(all_images) > 0
    wf_id, wf_meta, graph = resolve_workflow(
        media_type=media_type,
        has_image=has_image,
        workflow_name=workflow_name,
        registry=registry,
    )

    is_video = (media_type == "video") or (wf_meta.get("media_type") == "video")
    resolved_duration: Optional[int] = resolve_video_duration(duration, wf_meta) if is_video else None

    effective_prompt = prompt
    prompt_enhanced = False
    if is_video and wf_meta.get("enhance_prompt_skill") == "h3-prompt-writing" and enhance_prompt is not False:
        effective_prompt = enhance_h3_video_prompt(
            prompt,
            duration=resolved_duration or DEFAULT_VIDEO_DURATION,
            image_sources=all_images,
        )
        prompt_enhanced = True

    uploaded_names: list[str] = []
    for img_src in all_images:
        uploaded_names.append(upload_image(base_url, img_src))

    prepared_graph, used_seed, aspect_label = prepare_workflow_graph(
        graph,
        wf_meta,
        prompt=effective_prompt,
        negative_prompt=negative_prompt,
        seed=seed,
        duration=resolved_duration,
        aspect_ratio=aspect_ratio if not has_image else None,
        uploaded_images=uploaded_names,
        enhance_prompt=enhance_prompt if not is_video else None,
    )

    output_node_cfg = (wf_meta.get("nodes") or {}).get("output")
    media_bytes, meta = submit_and_wait(
        base_url,
        prepared_graph,
        output_node_cfg=output_node_cfg,
        timeout=timeout,
    )

    ext = Path(meta["filename"]).suffix or (".mp4" if is_video else ".png")
    saved_path: Optional[Path] = None
    if output_path is not None:
        out_p = Path(output_path)
        if out_p.is_dir() or str(output_path).endswith("/"):
            out_p.mkdir(parents=True, exist_ok=True)
            saved_path = out_p / f"{wf_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
        else:
            out_p.parent.mkdir(parents=True, exist_ok=True)
            saved_path = out_p
        saved_path.write_bytes(media_bytes)

    return {
        "success": True,
        "workflow": wf_id,
        "modality": "image" if has_image else "text",
        "media_type": "video" if is_video else "image",
        "base_url": base_url,
        "prompt": prompt,
        "effective_prompt": effective_prompt,
        "prompt_enhanced": prompt_enhanced,
        "duration": resolved_duration,
        "seed": used_seed,
        "aspect_ratio": aspect_label if not has_image else "source",
        "uploaded_images": uploaded_names,
        "prompt_id": meta["prompt_id"],
        "comfy_filename": meta["filename"],
        "view_url": meta["view_url"],
        "path": str(saved_path) if saved_path else None,
        "bytes": media_bytes,
        "extension": ext.lstrip("."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a ComfyUI API workflow (auto-routes text vs image edit / video).")
    parser.add_argument("--prompt", "-p", required=True, help="Text prompt or edit instruction")
    parser.add_argument("--image", "-i", help="Input image path or URL (triggers image edit / i2v workflow)")
    parser.add_argument("--media-type", choices=("image", "video"), default="image", help="Media type to generate")
    parser.add_argument("--workflow", "-w", help="Explicit workflow ID or JSON filename override")
    parser.add_argument("--duration", "-d", type=int, default=None, help="Video duration in seconds (default: 5, max: 15)")
    parser.add_argument("--aspect-ratio", "-a", default="landscape", help="Aspect ratio (landscape, square, portrait, 16:9, 1:1, 9:16, etc.)")
    parser.add_argument("--negative-prompt", "-n", help="Negative prompt override")
    parser.add_argument("--seed", "-s", type=int, help="Explicit random seed")
    parser.add_argument("--enhance-prompt", action="store_true", help="Enable in-workflow LLM prompt enhancement switch")
    parser.add_argument("--no-enhance-prompt", action="store_true", help="Disable automatic H3 video prompt enhancement")
    parser.add_argument("--instance", default="3090", help="ComfyUI instance ('3090', '5090', or full URL)")
    parser.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT_DIR) + "/", help="Output file or directory path")
    parser.add_argument("--timeout", type=float, default=900.0, help="Timeout in seconds")
    args = parser.parse_args()

    enhance_flag: Optional[bool] = None
    if args.no_enhance_prompt:
        enhance_flag = False
    elif args.enhance_prompt:
        enhance_flag = True

    try:
        res = run_comfy_workflow(
            prompt=args.prompt,
            media_type=args.media_type,
            image_source=args.image,
            workflow_name=args.workflow,
            duration=args.duration,
            aspect_ratio=args.aspect_ratio,
            negative_prompt=args.negative_prompt,
            seed=args.seed,
            enhance_prompt=enhance_flag,
            instance=args.instance,
            output_path=args.output,
            timeout=args.timeout,
        )
        res_printable = {k: v for k, v in res.items() if k != "bytes"}
        print(json.dumps(res_printable, indent=2))
        return 0
    except ComfyAPIError as exc:
        print(json.dumps({"success": False, "error": str(exc), "error_type": exc.error_type}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
