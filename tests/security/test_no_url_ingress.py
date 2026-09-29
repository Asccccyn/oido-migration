"""安全合同：oído 工具不接受 URL/路径输入（WP00 §3 的本仓侧落地）。"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def _tools():
    contract = json.loads((CONTRACTS / "mcp-tools.v1.json").read_text())
    return contract["tools"]


class TestNoUrlIngressInToolContract:
    def test_analyze_has_no_url_or_path_param(self):
        analyze = next(t for t in _tools() if t["name"] == "oido_analyze")
        props = analyze["input"]["properties"]
        assert "url" not in props and "path" not in props
        assert "audio_id" in props
        # schema 的 not-branch 也显式封死 url/path
        assert "not" in analyze["input"]

    def test_no_tool_accepts_url_or_path(self):
        for tool in _tools():
            props = tool["input"].get("properties", {})
            assert "url" not in props, f"{tool['name']} accepts url"
            assert "path" not in props, f"{tool['name']} accepts path"

    def test_display_name_is_locked(self):
        contract = json.loads((CONTRACTS / "mcp-tools.v1.json").read_text())
        assert contract["toolset_display_name"] == "我只听你"

    def test_six_tools_frozen(self):
        names = [t["name"] for t in _tools()]
        assert names == [
            "oido_capabilities", "oido_analyze", "oido_get",
            "oido_view", "oido_find", "oido_compare",
        ]

    def test_analysis_schema_stages_use_reason_codes(self):
        schema = json.loads((CONTRACTS / "analysis.v1.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        reason_enum = schema["properties"]["stages"]["items"]["properties"]["reason_code"]["enum"]
        assert "provider_not_configured" in reason_enum
        assert "vibe_check" not in reason_enum
