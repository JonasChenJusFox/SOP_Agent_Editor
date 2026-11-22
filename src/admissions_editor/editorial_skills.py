"""Packaged, versioned editorial instructions shared by CLI and cloud calls.

These files are application resources. Loading a personal Codex skill has no
effect on the application's provider requests, and no network fetch is needed.
"""

import hashlib
import json
from importlib.resources import files

STAGES = ("sop_revise", "polish", "humanize", "verify_revision")
RESOURCE_DIRECTORY = "revision_prompts"


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def load_editorial_skill(stage):
    """Return checked instructions and traceable metadata for one model stage."""
    if stage not in STAGES:
        raise ValueError("Unknown editorial stage.")
    root = files("admissions_editor").joinpath(RESOURCE_DIRECTORY)
    manifest_bytes = root.joinpath("manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != 1 or set(manifest.get("stages", {})) != set(STAGES):
        raise ValueError("Unsupported editorial skill manifest.")
    entry = manifest["stages"][stage]
    if entry.get("file") != stage + ".md":
        raise ValueError("Editorial prompt path does not match its stage.")
    common = root.joinpath("common.md").read_bytes()
    body = root.joinpath(entry["file"]).read_bytes()
    if _sha256(common) != manifest["common_sha256"] or _sha256(body) != entry["body_sha256"]:
        raise ValueError("Editorial prompt differs from its versioned manifest.")
    instructions = common.decode("utf-8").rstrip() + "\n\n" + body.decode("utf-8").strip() + "\n"
    digest = _sha256(instructions.encode("utf-8"))
    if digest != entry["sha256"]:
        raise ValueError("Editorial instructions checksum differs.")
    sources = manifest["sources"]
    return {"stage": stage, "name": entry["name"], "version": entry["version"],
            "instructions": instructions, "sha256": digest,
            "manifest_sha256": _sha256(manifest_bytes),
            "references": [sources[source_id] for source_id in entry["reference_ids"]]}


def editorial_manifest():
    """Return stage metadata without sending instruction bodies to callers."""
    return [{key: value for key, value in load_editorial_skill(stage).items() if key != "instructions"}
            for stage in STAGES]


if __name__ == "__main__":
    print(json.dumps(editorial_manifest(), ensure_ascii=False, indent=2))
