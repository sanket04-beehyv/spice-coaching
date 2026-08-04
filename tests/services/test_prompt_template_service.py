"""Unit tests for PromptTemplateService."""

from __future__ import annotations

import pytest
from platform_service.services.prompt_template_service import (
    PromptTemplateRenderError,
    PromptTemplateService,
)


class TestPromptTemplateService:
    def test_substitute_replaces_variables(self) -> None:
        result = PromptTemplateService.substitute(
            "Hello {name}, count={count}",
            {"name": "CHW", "count": "3"},
        )
        assert result == "Hello CHW, count=3"

    def test_substitute_raises_on_missing_variable(self) -> None:
        with pytest.raises(PromptTemplateRenderError, match="missing template variable"):
            PromptTemplateService.substitute("Hello {name}", {})

    def test_validate_variables_reports_missing(self) -> None:
        with pytest.raises(PromptTemplateRenderError, match="missing variables"):
            PromptTemplateService.validate_variables(
                required_variables=["a", "b"],
                variables={"a": "1"},
                template_id="demo",
            )

    def test_validate_template_syntax_requires_declared_placeholders(self) -> None:
        with pytest.raises(PromptTemplateRenderError, match="not listed in required_variables"):
            PromptTemplateService.validate_template_syntax(
                system_prompt_template="Hello {name}",
                human_message_template="Body",
                required_variables=[],
            )

    def test_extract_placeholders_preserves_order(self) -> None:
        names = PromptTemplateService.extract_placeholders("{b} text {a} {b}")
        assert names == ["b", "a"]
