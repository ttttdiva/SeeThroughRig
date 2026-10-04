"""Project-scoped editor data contract; no persistent web service is required."""
from __future__ import annotations

from pathlib import Path

from .package_validate import require
from .production_export import collect_model, verified_model_core_ref
from .production_io import atomic_json, inside, lock, read_file, read_json, sha
from .production_projects import REPO, load_project, save, tree_hashes

MAX_SETTINGS = 256 * 1024


def _model_directory(project: Path, state: dict) -> Path:
    adopted = state.get("adoption")
    record = adopted or state.get("build_cache") or {}
    relative = record.get("model_dir") if adopted else record.get("directory")
    require(isinstance(relative, str) and relative, "No editable completed model exists in this project")
    directory = inside(project, project / relative)
    if adopted:
        require(tree_hashes(directory) == record.get("files"), "Adopted snapshot changed; re-adoption is required")
    require((directory / "model.json").is_file(), "Project model is missing")
    return directory


def editor_info(project_id: str, root: Path | None = None) -> dict:
    project, state = load_project(project_id, root)
    directory = _model_directory(project, state)
    settings = project / "material/settings.json"
    if not settings.is_file():
        settings = directory / "settings.json"
    explicit = settings if settings.is_file() else None
    preview = directory / "preview.png" if (directory / "preview.png").is_file() else None
    payload, model, _ = collect_model(directory, preview=preview, settings=explicit, source_sha256=state["source_sha256"])
    assets = {"model.json": str((directory / "model.json").relative_to(project))}
    for atlas in model["atlases"]:
        assets[atlas["file"]] = str((directory / atlas["file"]).relative_to(project))
    if explicit:
        assets["settings.json"] = str(explicit.relative_to(project))
    commit = (state.get("adoption") or {}).get("commit")
    core_ref = verified_model_core_ref(REPO, payload, model, state["source_sha256"], commit)
    return {"project_id": project_id, "owner": state["owner"], "character": state["character"],
            "source_sha256": state["source_sha256"], "core_ref": core_ref,
            "files": assets, "hashes": {name: sha(payload[name]) for name in assets},
            "settings_present": explicit is not None, "status": state["status"]}


def editor_save(project_id: str, settings_file: Path, root: Path | None = None) -> dict:
    project, state = load_project(project_id, root)
    incoming = inside(project / "requests", settings_file)
    require(incoming.parent == project / "requests" and incoming.suffix == ".json", "Editor input must be a scoped JSON request")
    read_file(incoming, MAX_SETTINGS)
    data = read_json(incoming)
    require(isinstance(data, dict), "Editor settings must be an object")
    with lock(project / "operation.lock"):
        project, state = load_project(project_id, root)
        directory = _model_directory(project, state)
        preview = directory / "preview.png" if (directory / "preview.png").is_file() else None
        # The canonical exporter validates source model and safe settings together.
        collect_model(directory, preview=preview, settings=incoming, source_sha256=state["source_sha256"])
        atomic_json(project / "material/settings.json", data)
        settings_hash = sha(read_file(project / "material/settings.json", MAX_SETTINGS))
        previous = state.get("revision")
        save(project, state, status="created", stage="edited", previous_revision=previous,
             revision=None, output_package=None, artistic_quality_approved=False)
    return {"project_id": project_id, "saved": True, "settings_sha256": settings_hash,
            "status": "created", "requires_new_export": True, "active_asset_changed": False}
