import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from issueforge.agents.agy_runner import AgySessionRunner
from issueforge.core.models import AgentRole
from issueforge.core.sandbox import NativeSandbox


EXPECTED_SKILLS = [
    "senior-dev",
    "ponytail",
    "frontend-tool",
    "ui-development",
    "senior-backend-engineer",
]


def test_repo_skills_exist_and_have_frontmatter():
    import issueforge
    skills_dir = Path(issueforge.__file__).parent / "agents" / "skills"
    assert skills_dir.is_dir(), "skills must exist inside the issueforge package"

    for skill_name in EXPECTED_SKILLS:
        skill_path = skills_dir / skill_name / "SKILL.md"
        assert skill_path.is_file(), f"Skill file {skill_name}/SKILL.md must exist"
        content = skill_path.read_text(encoding="utf-8")
        assert "---" in content, f"Skill {skill_name} must contain YAML frontmatter delimiter"
        assert f"name: {skill_name}" in content or f"name: \"{skill_name}\"" in content or f"name: '{skill_name}'" in content
        assert "description:" in content


def test_sandbox_setup_injects_skills(tmp_path):
    sandbox = NativeSandbox("test-skills-sandbox", run_id="run-1")
    with patch.object(sandbox, "workspace_path", tmp_path / "sandbox-workspace"):
        sandbox.setup()
        injected_skills_dir = sandbox.workspace_path / ".agents" / "skills"
        assert injected_skills_dir.is_dir(), "Sandbox workspace must contain .agents/skills directory after setup"

        for skill_name in EXPECTED_SKILLS:
            skill_file = injected_skills_dir / skill_name / "SKILL.md"
            assert skill_file.is_file(), f"Injected skill {skill_name}/SKILL.md must exist in sandbox workspace"
            content = skill_file.read_text(encoding="utf-8")
            assert len(content) > 50


def test_agy_runner_ensures_skills_present(tmp_path):
    sandbox_dir = tmp_path / "agy-workspace"
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    runner = AgySessionRunner(
        task_id="test-task",
        run_id="run-1",
        sandbox_dir=sandbox_dir,
        role=AgentRole.CODER,
    )

    loaded = runner._ensure_skills_present()
    assert len(loaded) >= len(EXPECTED_SKILLS)
    for skill_name in EXPECTED_SKILLS:
        assert (sandbox_dir / ".agents" / "skills" / skill_name / "SKILL.md").exists()
