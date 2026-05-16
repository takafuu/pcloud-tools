from __future__ import annotations

import re
from dataclasses import fields

from conftest import *
from pcloud_tools.config import CONFIG_FIELD_SPECS, AppConfig


def test_config_field_specs_match_app_config_fields() -> None:
    spec_names = [spec.name for spec in CONFIG_FIELD_SPECS]
    field_names = [field.name for field in fields(AppConfig)]

    assert spec_names == field_names
    assert len(spec_names) == len(set(spec_names))


def test_env_example_keys_match_config_field_specs() -> None:
    spec_env_vars = {spec.env_var for spec in CONFIG_FIELD_SPECS if spec.env_var}
    example_text = (REPO_ROOT / ".env.example").read_text()
    example_env_vars = {
        match.group(1)
        for match in re.finditer(r"^(PCLOUD_TOOLS_[A-Z0-9_]+)=", example_text, re.MULTILINE)
    }

    assert example_env_vars == spec_env_vars
