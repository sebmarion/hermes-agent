"""Contract tests for the bundled apple-design skill."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.skill_utils import extract_skill_description, parse_frontmatter


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "creative" / "apple-design"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES_DIR = SKILL_DIR / "references"
CLAUDE_DESIGN_MD = REPO_ROOT / "skills" / "creative" / "claude-design" / "SKILL.md"

SOURCE_COMMIT = "56de6f5d6642f761b5e17629fccf53e303b3da9b"
EXPECTED_DESCRIPTION = "Use when designing gesture-driven UI or physical web motion."
EXPECTED_REFERENCES = {
    "interaction-physics.md",
    "materials-type-accessibility.md",
    "design-principles.md",
    "UPSTREAM_LICENSE.txt",
}
EXPECTED_SECTIONS = (
    "When to Use",
    "Prerequisites",
    "How to Run",
    "Quick Reference",
    "Procedure",
    "Pitfalls",
    "Verification",
    "Attribution",
)
EXPECTED_UPSTREAM_LICENSE = """MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _h2_headings(source: str) -> tuple[str, ...]:
    return tuple(re.findall(r"^## (.+)$", source, flags=re.MULTILINE))


def _section_body(source: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, heading
    return match.group("body")


def _plain(source: str) -> str:
    return re.sub(r"\s+", " ", source.replace("`", " ")).strip()


@pytest.fixture(scope="module")
def skill_source() -> str:
    return _read(SKILL_MD)


@pytest.fixture(scope="module")
def parsed_skill(skill_source: str) -> tuple[dict, str]:
    frontmatter, body = parse_frontmatter(skill_source)
    assert frontmatter, "SKILL.md must contain valid YAML frontmatter"
    return frontmatter, body


def test_package_contains_only_the_approved_files() -> None:
    actual = {
        path.relative_to(SKILL_DIR).as_posix()
        for path in SKILL_DIR.rglob("*")
        if path.is_file()
    }
    expected = {"SKILL.md"} | {
        f"references/{name}" for name in EXPECTED_REFERENCES
    }
    assert actual == expected


def test_frontmatter_matches_the_bundled_skill_contract(parsed_skill) -> None:
    frontmatter, _ = parsed_skill

    assert frontmatter["name"] == "apple-design"
    assert frontmatter["description"] == EXPECTED_DESCRIPTION
    assert len(frontmatter["description"]) == 60
    assert frontmatter["description"].startswith("Use when ")
    assert frontmatter["description"].endswith(".")
    assert extract_skill_description(frontmatter) == EXPECTED_DESCRIPTION
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["author"] == "Emil Kowalski (emilkowalski), Hermes Agent"
    assert frontmatter["license"] == "MIT"
    assert frontmatter["platforms"] == ["linux", "macos", "windows"]

    hermes = frontmatter["metadata"]["hermes"]
    assert set(hermes["tags"]) >= {
        "design",
        "interaction",
        "motion",
        "gestures",
        "springs",
        "accessibility",
        "web",
    }
    assert hermes["related_skills"] == [
        "claude-design",
        "design-md",
        "popular-web-designs",
    ]


def test_modern_sections_are_present_and_ordered(parsed_skill) -> None:
    _, body = parsed_skill
    positions = []
    for section in EXPECTED_SECTIONS:
        heading = f"## {section}"
        assert heading in body
        positions.append(body.index(heading))
    assert positions == sorted(positions)


def test_all_local_links_resolve_inside_the_skill(parsed_skill) -> None:
    _, body = parsed_skill
    local_targets = []
    for target in re.findall(r"\]\(([^)]+)\)", body):
        if target.startswith(("https://", "http://", "#")):
            continue
        local_targets.append(target)

    assert {Path(target).name for target in local_targets} >= EXPECTED_REFERENCES
    for target in local_targets:
        resolved = (SKILL_DIR / target).resolve()
        assert resolved.is_relative_to(SKILL_DIR.resolve())
        assert resolved.is_file(), target


@pytest.mark.parametrize(
    ("filename", "required_terms"),
    [
        (
            "interaction-physics.md",
            (
                "setPointerCapture",
                "presentation value",
                "relativeVelocity",
                "decelerationRate",
                "rubberband",
                "hysteresis",
                "requestAnimationFrame",
                "starting points",
            ),
        ),
        (
            "materials-type-accessibility.md",
            (
                "backdrop-filter",
                "prefers-reduced-motion",
                "prefers-reduced-transparency",
                "prefers-contrast",
                "font-optical-sizing",
                "semantic activation",
                "brand typography",
            ),
        ),
        (
            "design-principles.md",
            (
                "Purpose",
                "Agency",
                "Responsibility",
                "Familiarity",
                "Flexibility",
                "Simplicity",
                "Craft",
                "Delight",
                "real context",
                "frame-by-frame",
            ),
        ),
    ],
)
def test_references_preserve_required_substance(
    filename: str, required_terms: tuple[str, ...]
) -> None:
    source = _read(REFERENCES_DIR / filename)
    for term in required_terms:
        assert term in source, f"{filename} is missing {term!r}"


def test_interaction_reference_headings() -> None:
    """Exact ordered H2 headings in interaction-physics.md."""
    src = _read(REFERENCES_DIR / "interaction-physics.md")
    headings = _h2_headings(src)
    expected = (
        "Immediate and Continuous Response",
        "One-to-One Direct Manipulation",
        "Interruptibility and Presentation-Value Continuity",
        "Springs as Behavior",
        "Velocity Sampling and Handoff",
        "Momentum Projection and Snap Points",
        "Spatial Consistency and Reversible Paths",
        "Intermediate Motion Signals the Destination",
        "Rubber-Banding at Boundaries",
        "Gesture Disambiguation and Cancellation",
        "Frame-Level Smoothness",
        "Starting Points, Not Requirements",
        "Framework and Dependency Boundary",
    )
    assert headings == expected, (
        f"interaction-physics.md headings: {headings}"
    )


def test_materials_reference_headings() -> None:
    """Exact ordered H2 headings in materials-type-accessibility.md."""
    src = _read(REFERENCES_DIR / "materials-type-accessibility.md")
    headings = _h2_headings(src)
    expected = (
        "Existing Product Language Comes First",
        "Optional Materials and Depth",
        "Progressive Enhancement and Performance",
        "Multimodal Feedback",
        "Reduced Motion",
        "Reduced Transparency",
        "Increased Contrast",
        "Typography",
        "Input and Activation Semantics",
        "Verification Matrix",
    )
    assert headings == expected, (
        f"materials-type-accessibility.md headings: {headings}"
    )


def test_principles_reference_headings() -> None:
    """Exact ordered H2 headings in design-principles.md."""
    src = _read(REFERENCES_DIR / "design-principles.md")
    headings = _h2_headings(src)
    expected = (
        "Human Needs",
        "Purpose",
        "Agency",
        "Responsibility",
        "Familiarity",
        "Flexibility",
        "Simplicity",
        "Craft",
        "Delight",
        "Tactical Review Questions",
        "Prototype and Test in Real Context",
        "Review Scorecard",
    )
    assert headings == expected, (
        f"design-principles.md headings: {headings}"
    )


def test_interaction_physics_semantics() -> None:
    """Physics semantics via tightly coupled assertions on normalized prose."""
    src = _read(REFERENCES_DIR / "interaction-physics.md")
    norm = _plain(src)
    lower = norm.lower()

    # Correct spring semantics are explicit, not inverted.
    assert "pointer events api" in lower
    assert "damping" in lower
    assert "1.0" in lower

    # Damping ratio 1.0 is critically damped / no overshoot.
    # Response 0.3–0.4 seconds.
    # Damping about 0.8 is reserved for momentum overshoot.
    assert re.search(
        r"(?:critically damped.*damping ratio(?: of)? 1\.0|"
        r"damping ratio(?: of)? 1\.0.*critically damped)",
        lower,
    ), lower
    assert re.search(
        r"response(?: time)?(?: in the range)? 0\.3[–-]0\.4 seconds",
        lower,
    ), lower
    assert re.search(
        r"damping ratio(?: of)? (?:approximately |about )?0\.8.*momentum",
        lower,
    ), lower

    # Reject: damping ratio 0.3–0.4 (wrong section placement for damping ratio).
    # Reject: "normalized stiffness of 1.0" (non-existent claim).
    assert not re.search(
        r"damping ratio(?: of)?(?: approximately| about)? 0\.3[–-]0\.4",
        lower,
    ), lower
    assert "normalized stiffness of 1.0" not in lower

    # Absolute-velocity vs relative-velocity API semantics.
    assert "apis expecting absolute velocity" in lower
    assert "pass the sampled px/s value directly" in lower
    assert "apis expecting relative velocity" in lower
    assert "divide by the remaining distance" in lower

    # Exact guarded relative-velocity statement and nonlinear rubber-band return.
    code = re.sub(r"\s+", " ", src)
    assert (
        "const distance = targetValue - currentValue; "
        "const relativeVelocity = distance === 0 ? 0 : "
        "gestureVelocity / distance;"
    ) in code
    assert (
        "return (overshoot * dimension * constant) / "
        "(dimension + constant * Math.abs(overshoot));"
    ) in code

    # Banned circular fallbacks.
    assert "fall back to requestanimationframe" not in lower
    assert "fall back to transform" not in lower


def test_interaction_activation_semantics() -> None:
    """Semantic activation via section-scoped assertions."""
    src = _read(REFERENCES_DIR / "interaction-physics.md")
    section = _section_body(src, "Gesture Disambiguation and Cancellation")
    lower = section.lower()

    assert "pointer-down may begin immediate visual feedback" in lower
    assert "does not activate" in lower
    assert "native click" in lower or "click/touch-up" in lower
    assert "keyboard" in lower
    assert "assistive technology" in lower
    assert "cancellation" in lower
    assert "suppresses activation" in lower, "must suppress activation"
    assert "restores state" in lower, "must restore state"


def test_materials_accessibility_sections() -> None:
    """Positive accessibility checks in materials sections."""
    src = _read(REFERENCES_DIR / "materials-type-accessibility.md")

    # Reduced Motion
    reduced_motion = _section_body(src, "Reduced Motion")
    rm_lower = reduced_motion.lower()
    assert "independent accessibility adaptation" in rm_lower
    assert "cross-fades" in rm_lower or "static transitions" in rm_lower
    assert "state feedback" in rm_lower

    # Reduced Transparency
    reduced_trans = _section_body(src, "Reduced Transparency")
    rt_lower = _plain(reduced_trans).lower()
    assert "independent accessibility adaptation" in rt_lower
    assert "solid surfaces" in rt_lower
    assert re.search(r"(?:remove|disable) backdrop-filter", rt_lower)
    assert re.search(r"(?:remove|disable|disables)(?:\s+\w+){0,2}\s+blur", rt_lower)

    # Increased Contrast
    increased_contrast = _section_body(src, "Increased Contrast")
    ic_lower = increased_contrast.lower()
    assert "independent accessibility adaptation" in ic_lower
    assert "boundaries" in ic_lower and "visible" in ic_lower
    assert "contrast ratio" in ic_lower

    # Reject opt-out language in all three sections.
    opt_out_phrases = (
        "do not add",
        "skip when unsupported",
        "only if already",
        "optional",
        "may omit",
    )
    for section_text in (reduced_motion, reduced_trans, increased_contrast):
        s_lower = section_text.lower()
        for phrase in opt_out_phrases:
            assert phrase not in s_lower, (
                f"Found accessibility opt-out {phrase!r} in: {section_text}"
            )


def test_design_principles_human_needs_and_feedback() -> None:
    """Human Needs and Feedback via section-scoped assertions."""
    src = _read(REFERENCES_DIR / "design-principles.md")

    human_needs = _section_body(src, "Human Needs")
    bullets = {
        match.group("label").strip().lower(): _plain(match.group("body"))
        for match in re.finditer(
            r"^- \*\*(?P<label>[^*]+)\*\*\s+[—-]\s+"
            r"(?P<body>.*?)(?=^- \*\*|\Z)",
            human_needs,
            flags=re.MULTILINE | re.DOTALL,
        )
    }
    expected_needs = {
        "safety / predictability",
        "understanding",
        "achievement",
        "joy",
    }
    assert set(bullets) == expected_needs
    for need, question in bullets.items():
        assert "?" in question, f"Human Need {need!r} has no review question"

    feedback = _section_body(src, "Tactical Review Questions")
    fb_lower = feedback.lower()
    assert "status" in fb_lower
    assert "completion" in fb_lower
    assert "warning" in fb_lower
    assert "error" in fb_lower


def test_design_principles_sections_have_review_questions() -> None:
    """Every principle sentence must be useful, complete interrogative guidance."""
    src = _read(REFERENCES_DIR / "design-principles.md")
    for heading in (
        "Purpose",
        "Agency",
        "Responsibility",
        "Familiarity",
        "Flexibility",
        "Simplicity",
        "Craft",
        "Delight",
    ):
        body = _plain(_section_body(src, heading))
        sentences = re.findall(r"[^.!?]+[.!?]", body)
        assert len(sentences) >= 2, (
            f"Section {heading!r} needs at least two substantive questions"
        )
        assert "".join(sentences).replace(" ", "") == body.replace(" ", ""), (
            f"Section {heading!r} contains an incomplete sentence: {body}"
        )
        for sentence in sentences:
            assert sentence.rstrip().endswith("?"), (
                f"Section {heading!r} contains declarative guidance: {sentence.strip()}"
            )


def test_interaction_spatial_consistency_semantics() -> None:
    """Spatial consistency via section-scoped assertions."""
    src = _read(REFERENCES_DIR / "interaction-physics.md")
    section = _section_body(src, "Spatial Consistency and Reversible Paths")
    lower = _plain(section).lower()
    assert "content exits along the same path it entered" in lower
    assert (
        "menus, popovers, and sheets originate from their triggering element"
        in lower
    )
    assert "source-anchored transform origin" in lower
    assert "reverse" in lower, "must demonstrate reversible paths"


def test_interaction_hysteresis_semantics() -> None:
    """Gesture disambiguation must enforce 10px hysteresis semantics."""
    src = _read(REFERENCES_DIR / "interaction-physics.md")
    section = _section_body(src, "Gesture Disambiguation and Cancellation")
    lower = _plain(section).lower()
    assert "10px" in lower, "must enforce 10px hysteresis threshold"
    assert "hysteresis" in lower, "must define hysteresis"
    assert "tap" in lower, "must cover tap detection"
    assert "drag" in lower, "must cover drag detection"
    assert "dragging away can cancel" in lower
    assert "returning below the threshold before release can restore" in lower
    assert "after drag commitment" in lower
    assert "does not restore tap candidacy" in lower


def test_interaction_rubber_banding_no_10px() -> None:
    """Rubber-Banding at Boundaries must NOT contain 10px."""
    src = _read(REFERENCES_DIR / "interaction-physics.md")
    section = _section_body(src, "Rubber-Banding at Boundaries")
    lower = section.lower()
    assert "10px" not in lower, "rubber-banding must not contain 10px"
    assert "boundaries" in lower or "boundary" in lower, \
        "must describe boundary behavior"


def test_attribution_and_license_match_the_pinned_source(skill_source: str) -> None:
    assert SOURCE_COMMIT in skill_source
    assert "Emil Kowalski" in skill_source
    assert (
        "https://raw.githubusercontent.com/emilkowalski/skills/"
        f"{SOURCE_COMMIT}/skills/apple-design/SKILL.md"
    ) in skill_source

    notice = _read(REFERENCES_DIR / "UPSTREAM_LICENSE.txt")
    assert notice == EXPECTED_UPSTREAM_LICENSE


def test_package_adds_no_executable_or_dependency_payload() -> None:
    assert not (SKILL_DIR / "scripts").exists()
    assert not any(
        path.name in {"package.json", "requirements.txt", "pyproject.toml"}
        for path in SKILL_DIR.rglob("*")
    )

    payload = "\n".join(
        _read(path)
        for path in SKILL_DIR.rglob("*")
        if path.is_file()
    )
    assert not re.search(r"\b(?:npm|pnpm|yarn)\s+(?:install|add)\b", payload)
    assert not re.search(r"\bpip(?:3)?\s+install\b", payload)


def test_related_design_skills_are_bidirectional(parsed_skill) -> None:
    apple_frontmatter, _ = parsed_skill
    claude_source = _read(CLAUDE_DESIGN_MD)
    claude_frontmatter, _ = parse_frontmatter(claude_source)

    assert "claude-design" in apple_frontmatter["metadata"]["hermes"]["related_skills"]
    assert "apple-design" in claude_frontmatter["metadata"]["hermes"]["related_skills"]


def test_claude_design_has_positive_and_negative_routing_boundaries() -> None:
    source = _read(CLAUDE_DESIGN_MD)
    routing = _plain(_section_body(source, "Physical Interaction Routing")).lower()

    assert "| **apple-design**" in source
    assert "## Physical Interaction Routing" in source
    assert "load apple-design alongside this skill when" in routing
    assert "do not load apple-design merely for static layout" in routing

    for positive_pattern in (
        r"\bdrag\b",
        r"\bswipe\b",
        r"\bsheets\b",
        r"\bsnap points\b",
        r"\bvelocity handoff\b",
        r"\bmomentum projection\b",
        r"\blive-value interruption\b",
        r"\brubber-banding\b",
        r"\bsignificant translucent-material behavior\b",
        r"\breduced-transparency\b",
        r"\bhigher-contrast\b",
    ):
        assert re.search(positive_pattern, routing), positive_pattern

    for negative_term in (
        "spacing",
        "hierarchy",
        "color",
        "copy",
        "icons",
        "DESIGN.md",
    ):
        assert negative_term.lower() in routing

    assert "typography or accessibility alone is also insufficient" in routing
