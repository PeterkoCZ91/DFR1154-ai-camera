"""Minimal pkg_resources compatibility shim for face_recognition_models."""

from __future__ import annotations

import importlib.util
import os


def resource_filename(package_or_requirement: str, resource_name: str) -> str:
    package_name = package_or_requirement.split()[0]
    spec = importlib.util.find_spec(package_name)
    if spec is None:
        raise ModuleNotFoundError(package_name)

    if spec.submodule_search_locations:
        package_dir = next(iter(spec.submodule_search_locations))
    elif spec.origin:
        package_dir = os.path.dirname(spec.origin)
    else:
        raise ModuleNotFoundError(package_name)

    return os.path.join(package_dir, resource_name)
