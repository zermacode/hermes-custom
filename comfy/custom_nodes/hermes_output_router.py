"""ComfyUI custom node hook that routes saved outputs per request for Hermes Agent.

Routing rules:
  - filename_prefix starting with '__hermes_private__/' -> saves to ~/hermes-data-private/output
  - filename_prefix starting with '__hermes_public__/'  -> saves to ~/hermes-data/output
  - Default (any other request / Web UI)                -> saves to ~/hermes-data/output

Also registers 'private_output' and 'public_output' directory types with folder_paths
so ComfyUI's /view endpoint can serve files from either mount when requested.
"""

from __future__ import annotations

import logging
import os

import folder_paths

logger = logging.getLogger(__name__)

PRIVATE_PREFIX = "__hermes_private__/"
PUBLIC_PREFIX = "__hermes_public__/"

PRIVATE_OUTPUT_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("HERMES_PRIVATE_OUTPUT_DIR", "~/hermes-data-private/output"))
)
PUBLIC_OUTPUT_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("HERMES_PUBLIC_OUTPUT_DIR", "~/hermes-data/output"))
)


def _install_hooks() -> None:
    if getattr(folder_paths, "_hermes_output_router_installed", False):
        return

    os.makedirs(PUBLIC_OUTPUT_DIR, exist_ok=True)
    os.makedirs(PRIVATE_OUTPUT_DIR, exist_ok=True)

    # Ensure default ComfyUI output directory points to PUBLIC_OUTPUT_DIR
    current_out = os.path.abspath(folder_paths.get_output_directory())
    if current_out.endswith(("/3090/output", "/5090/output", "/ComfyUI/output")):
        folder_paths.set_output_directory(PUBLIC_OUTPUT_DIR)
        logger.info("Hermes Output Router: set default ComfyUI output directory to %s", PUBLIC_OUTPUT_DIR)

    orig_get_save_image_path = folder_paths.get_save_image_path
    orig_get_directory_by_type = folder_paths.get_directory_by_type

    def routed_get_save_image_path(
        filename_prefix: str,
        output_dir: str,
        image_width: int = 0,
        image_height: int = 0,
    ):
        prefix = (filename_prefix or "ComfyUI").replace("\\", "/")
        target_output_dir = output_dir

        if prefix.startswith(PRIVATE_PREFIX):
            clean_prefix = prefix[len(PRIVATE_PREFIX) :].lstrip("/") or "ComfyUI"
            target_output_dir = PRIVATE_OUTPUT_DIR
        elif prefix.startswith(PUBLIC_PREFIX):
            clean_prefix = prefix[len(PUBLIC_PREFIX) :].lstrip("/") or "ComfyUI"
            target_output_dir = PUBLIC_OUTPUT_DIR
        else:
            clean_prefix = filename_prefix
            abs_out = os.path.abspath(output_dir) if output_dir else ""
            if abs_out.endswith(("/3090/output", "/5090/output", "/ComfyUI/output")):
                target_output_dir = PUBLIC_OUTPUT_DIR

        os.makedirs(target_output_dir, exist_ok=True)
        return orig_get_save_image_path(clean_prefix, target_output_dir, image_width, image_height)

    def routed_get_directory_by_type(type_name: str) -> str | None:
        if type_name == "private_output":
            return PRIVATE_OUTPUT_DIR
        if type_name == "public_output":
            return PUBLIC_OUTPUT_DIR
        return orig_get_directory_by_type(type_name)

    folder_paths.get_save_image_path = routed_get_save_image_path
    folder_paths.get_directory_by_type = routed_get_directory_by_type
    folder_paths._hermes_output_router_installed = True
    logger.info(
        "Hermes Output Router active (public=%s, private=%s)",
        PUBLIC_OUTPUT_DIR,
        PRIVATE_OUTPUT_DIR,
    )


_install_hooks()

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
