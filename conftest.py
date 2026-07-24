"""Make the plugin importable as the ``hermies`` package during tests.

In production the plugin is cloned into a directory literally named ``hermies``
(see skills/install-hermies/SKILL.md), so ``from hermies import ...`` just works.
In this repo the checkout directory is ``hermies-and-friends`` (an invalid Python
identifier), so we register the repo root under the canonical package name
``hermies`` before any test module is collected.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

if "hermies" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "hermies",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermies"] = module
    spec.loader.exec_module(module)
