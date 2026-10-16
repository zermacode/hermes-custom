# Hermes Custom Setup (Signal + ComfyUI Image & Video Generation)

This repository contains the custom workflows, skills, configuration, and installer for running **Hermes Agent** on Signal with **ComfyUI** image and video generation on Tailscale host `nvidia` (defaulting to the RTX 3090 instance on `http://nvidia:8189`, with RTX 5090 on `http://nvidia:8188`).

Companion Hermes Agent fork: [`zermacode/hermes-agent`](https://github.com/zermacode/hermes-agent)

---

## Repository Structure

```text
hermes-custom/
├── comfy/
│   ├── comfy_api.py                      # ComfyUI workflow router, runner & H3 prompt enhancer
│   ├── launch_3090.sh                    # RTX 3090 launcher (defaults to ~/hermes-data/output)
│   ├── launch_5090.sh                    # RTX 5090 launcher (defaults to ~/hermes-data/output)
│   ├── custom_nodes/
│   │   └── hermes_output_router.py       # Dynamic per-request output directory router hook
│   └── workflows/api/
│       ├── workflows.json                # Declarative workflow & node registry (3090 default)
│       ├── qwen-image-text.json          # Text-to-Image workflow (Qwen Image)
│       ├── qwen-image-edit.json          # Image-Edit workflow (Qwen Image Edit)
│       ├── h3_fastvideo_text.json        # Text-to-Video+Audio workflow (MiniMax H3 8-step)
│       └── h3_fastvideo_edit.json        # Image-to-Video+Audio workflow (MiniMax H3 8-step)
├── skills/
│   └── creative/
│       ├── comfyui-media/SKILL.md        # Instructions for Hermes image/video routing & duration
│       └── h3-prompt-writing/            # H3 multimodal video/audio prompt enhancement guide
│           ├── SKILL.md
│           └── references/
│               ├── base-en.txt           # T2VA / I2VA / FL2VA guide
│               └── ref-en.txt            # Ref2VA guide
├── hermes/
│   ├── config.yaml                       # Default profile config (comfyui image_gen + video_gen)
│   ├── SOUL.md                           # Default profile persona
│   ├── .env.example                      # Template for ~/.hermes/.env (Signal settings)
│   └── profiles/limited/                 # Limited profile config & SOUL.md
├── plugins/
│   ├── image_gen/comfyui/                # Hermes ComfyUI image_gen plugin
│   └── video_gen/comfyui/                # Hermes ComfyUI video_gen plugin
├── install.sh                            # One-step installer for a new machine
└── update-hermes.sh                      # One-command upstream updater + gateway restart
```

---

## How Routing & Features Work

1. **Image Generation (`image_generate`)**:
   - **Text only** $\rightarrow$ `qwen-image-text.json`
   - **Image attached on Signal** $\rightarrow$ `qwen-image-edit.json`
2. **Video Generation (`video_generate`)**:
   - **Text only** $\rightarrow$ `h3_fastvideo_text.json`
   - **Image attached on Signal** $\rightarrow$ `h3_fastvideo_edit.json`
   - **Duration**: Defaults to **5 seconds** when omitted; clamped to **1–15 seconds** (injected into node `105:111`).
   - **H3 Prompt Enhancement**: Automatically uses the `h3-prompt-writing` skill + local LLM (`http://speedy:8080/v1`, model `roleplay`) to format prompts into H3's `integrated_multimodal_description:`, `overall_soundscape:`, and `non_diegetic_music:` sections (plus `<Picture 1>` visual alignment for Image-to-Video) before sending to ComfyUI.
3. **Output Directory Routing (SMB Mounts)**:
   - **Signal DM (or `DuetPrivateStatus`) from `+12197418383`** $\rightarrow$ ComfyUI writes directly to `~/hermes-data-private/output`.
   - **All other sources (public Signal groups like `ai_poop_group`, other users, CLI, Web UI)** $\rightarrow$ ComfyUI writes directly to `~/hermes-data/output`.

---

## Setting Up on a New Computer

1. **Install Hermes Agent and point `~/.hermes/hermes-agent` to your repo**:
   ```bash
   git clone git@github.com:zermacode/hermes-agent.git ~/.hermes/hermes-agent
   cd ~/.hermes/hermes-agent
   git remote add upstream git@github.com:NousResearch/hermes-agent.git
   ```

2. **Clone `hermes-custom` and run `install.sh`**:
   ```bash
   git clone git@github.com:zermacode/hermes-custom.git ~/hermes-custom
   cd ~/hermes-custom
   ./install.sh
   ```

3. **Verify `~/.hermes/.env` and start the gateway**:
   ```bash
   hermes gateway stop && hermes gateway ensure
   ```

---

## Pulling Official Upstream Hermes Updates

Whenever Nous Research releases updates to `hermes-agent`, you can simply run:

```bash
~/hermes-custom/update-hermes.sh
```

This fetches `NousResearch/hermes-agent`, rebases your custom ComfyUI/Signal commit on top, pushes to `zermacode/hermes-agent`, syncs `~/hermes-agent`, and restarts the Hermes gateway.