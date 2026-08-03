"""Make the plugin importable as the ``hermix`` package during tests.

In production the plugin is cloned into a directory literally named ``hermix``
(see skills/install-hermix/SKILL.md), so ``from hermix import ...`` just works.
In this repo the checkout directory is ``hermix`` (an invalid Python
identifier), so we register the repo root under the canonical package name
``hermix`` before any test module is collected.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

if "hermix" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "hermix",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermix"] = module
    spec.loader.exec_module(module)
