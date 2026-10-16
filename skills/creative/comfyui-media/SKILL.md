---
name: comfyui-media
description: Generate and edit images or videos using the local ComfyUI instances (RTX 3090 on port 8189, RTX 5090 on port 8188) via image_generate, video_generate, or /home/agent/AI/comfy/comfy_api.py.
---

# Local ComfyUI Image & Video Generation (RTX 3090 & RTX 5090)

Hermes is wired to the locally running ComfyUI instances (**RTX 3090** on `http://127.0.0.1:8189` default, **RTX 5090** on `http://127.0.0.1:8188`) as the default backend (`comfyui`) for both `image_generate` and `video_generate`.

## Automatic Output Directory Routing (SMB Mounts)

ComfyUI and `comfy_api.py` automatically route and save generated images and videos directly to the appropriate SMB mount based on the incoming request source:
- **Requests from `+12197418383` over Signal (Direct Message or `DuetPrivateStatus`)**:
  Saved directly by ComfyUI to `/home/agent/hermes-data-private/output` (`~/hermes-data-private/output`).
- **All other requests (public Signal groups like `ai_poop_group`, other users, CLI, Web UI)**:
  Saved directly by ComfyUI to `/home/agent/hermes-data/output` (`~/hermes-data/output`).

> **Important**: When invoking `/home/agent/AI/comfy/comfy_api.py` directly (for example with `--instance 5090`), **omit `--output` / `-o`** so ComfyUI's native SMB output path (`"path"` in the returned JSON) is used directly without creating extra copies in `~/.hermes/cache/`. Reference the returned `"path"` in your reply as `MEDIA:<path>`.

## Automatic Image Workflow Routing on Signal

When a user on Signal (direct chat or group chat) requests an image or edits an attached image:

1. **Image Attached -> Image Edit Workflow (`qwen-image-edit.json`)**
   - If the incoming Signal message has an image attached (`[Image attached at: /path/to/image.png]`), pass that file path as `image_url` to `image_generate(prompt="...", image_url="/path/to/image.png")`.
   - Even if `image_url` is omitted, the `comfyui` backend automatically detects images attached on the current turn (`_native_turn_images`) and routes to `/home/agent/AI/comfy/workflows/api/qwen-image-edit.json`.
2. **Text Only -> Text-to-Image Workflow (`qwen-image-text.json`)**
   - If no image is attached, call `image_generate(prompt="...", aspect_ratio="landscape" | "square" | "portrait")`.
   - Routes to `/home/agent/AI/comfy/workflows/api/qwen-image-text.json`.

## Automatic Video Workflow Routing & H3 Prompt Enhancement on Signal

When a user requests a video (with or without an attached image):

1. **Workflow Routing**:
   - **Text Only -> `h3_fastvideo_text.json`** (MiniMax H3 8-step FastVideo Text-to-Video + Audio).
   - **Image Attached -> `h3_fastvideo_edit.json`** (MiniMax H3 8-step FastVideo Image-to-Video + Audio). Pass `image_url="/path/to/image.png"` to `video_generate` (or rely on automatic turn-image detection).
2. **Video Duration (`duration`)**:
   - Default duration is **5 seconds** if the user does not specify one.
   - Maximum duration is **15 seconds** (clamped to `[1, 15]`).
   - Pass `duration=<seconds>` (e.g., `duration=5` or whatever the user requested up to `15`) to `video_generate`.
3. **Prompt Enhancement (`h3-prompt-writing`)**:
   - Video generation uses the `h3-prompt-writing` skill (`~/.agents/skills/h3-prompt-writing`, also installed in Hermes skills) to format prompts into the H3 multimodal structure (`integrated_multimodal_description:`, `overall_soundscape:`, `non_diegetic_music:`, and `<Picture 1>` alignment for image-to-video).
   - You can load `skill_view(name="h3-prompt-writing")` to craft the structured H3 prompt yourself, **or** pass a natural-language prompt to `video_generate` and `comfy_api.py` will automatically enhance it using the `h3-prompt-writing` guide before submitting to ComfyUI.
4. **Delivery to Signal**:
   - `image_generate` and `video_generate` automatically attach the generated image/video file from the SMB mount to your Signal reply—no extra commands needed.
