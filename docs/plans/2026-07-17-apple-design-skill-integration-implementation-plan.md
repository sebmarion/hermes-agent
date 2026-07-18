# Apple Design Specialist Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a bundled, conditionally selected `apple-design` specialist that improves gesture-driven and physical-motion design without changing Hermes's general design identity or runtime skill loader.

**Architecture:** Add a progressive-disclosure skill package under `skills/creative/apple-design`, with a concise router in `SKILL.md` and detailed guidance in three references. Extend `claude-design` with a narrow composition rule, verify the metadata and routing contracts with hermetic tests, and regenerate only the feature-attributable documentation artifacts. No core Python, tool, prompt-builder, package, or runtime-selection code changes are required.

**Tech Stack:** Markdown skill packages, YAML frontmatter, Python 3.11-3.13, pytest through `scripts/run_tests.sh`, Hermes `skill_view`, Docusaurus, Node.js 20+.

**Relevant execution skills:** Use `@superpowers:test-driven-development` for Tasks 1-3 and `@superpowers:verification-before-completion` for Task 5. Apply Hermes's in-repo `hermes-agent-skill-authoring` rules over generic skill scaffolding defaults.

---

## Source of truth and repository note

- Approved design: `docs/plans/2026-07-17-apple-design-skill-integration-design.md`
- Immutable upstream skill: `https://raw.githubusercontent.com/emilkowalski/skills/56de6f5d6642f761b5e17629fccf53e303b3da9b/skills/apple-design/SKILL.md`
- Immutable upstream license: `https://raw.githubusercontent.com/emilkowalski/skills/56de6f5d6642f761b5e17629fccf53e303b3da9b/LICENSE`
- Upstream skill author credit: Emil Kowalski (`emilkowalski`)
- Upstream repository MIT notice: Copyright (c) 2026 Matt Pocock

The plan is stored under `docs/plans/` because this repository ignores
`docs/superpowers/`. Do not run the generic `skill-creator` initializer: the
approved Hermes package topology intentionally omits `agents/openai.yaml`, and
Hermes's richer frontmatter plus generated website documentation are the
repository-specific contract.

## File map

### Create

- `skills/creative/apple-design/SKILL.md` — compact trigger, precedence, reference router, procedure, pitfalls, and verification checklist.
- `skills/creative/apple-design/references/interaction-physics.md` — gesture tracking, interruption, springs, velocity, momentum, boundaries, and frame behavior.
- `skills/creative/apple-design/references/materials-type-accessibility.md` — optional materials, typography, multimodal feedback, and accessibility alternatives.
- `skills/creative/apple-design/references/design-principles.md` — product principles and interaction-review process.
- `skills/creative/apple-design/references/UPSTREAM_LICENSE.txt` — verbatim immutable upstream MIT notice.
- `tests/skills/test_apple_design_skill.py` — hermetic metadata, topology, content-boundary, attribution, and routing contracts.
- `website/docs/user-guide/skills/bundled/creative/creative-apple-design.md` — generated skill page.

### Modify

- `skills/creative/claude-design/SKILL.md:1-40` — related-skill metadata and design-skill decision table.
- `skills/creative/claude-design/SKILL.md:92-120` — explicit positive and negative `apple-design` routing boundary.
- `website/docs/user-guide/skills/bundled/creative/creative-claude-design.md` — regenerated from the modified source skill.
- `website/docs/reference/skills-catalog.md` — mechanically retain only the generated `apple-design` catalog delta.
- `website/sidebars.ts` — mechanically retain only the generated `apple-design` sidebar delta.

### Must remain unchanged

- `agent/prompt_builder.py`
- `agent/skill_utils.py`
- `tools/skills_tool.py`
- `CHANGELOG.md`
- package manifests and lockfiles
- translated website pages under `website/i18n/`

## Commit sequence

1. `feat(skills): add apple interaction design specialist`
2. `feat(skills): route physical design work to apple specialist`
3. `docs(skills): publish apple design specialist`

Task 1 deliberately leaves a red test uncommitted. Task 2 commits that test
together with the implementation that turns it green.

### Task 1: Lock the package contract in a failing test

**Files:**

- Create: `tests/skills/test_apple_design_skill.py`
- Read: `agent/skill_utils.py:123-160`
- Read: `agent/skill_utils.py:783-791`
- Test: `tests/skills/test_apple_design_skill.py`

- [ ] **Step 1: Create the contract-test module**

Create `tests/skills/test_apple_design_skill.py` with this initial content:

```python
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
```

- [ ] **Step 2: Run the contract test and prove the package is missing**

Run:

```bash
scripts/run_tests.sh tests/skills/test_apple_design_skill.py -q
```

Expected: FAIL because the approved package paths do not exist yet (the first
failure may be the empty-package assertion or a `FileNotFoundError` for
`skills/creative/apple-design/SKILL.md`). This is the required red state. Do
not commit it yet.

### Task 2: Add the specialist package and turn the contract green

**Files:**

- Create: `skills/creative/apple-design/SKILL.md`
- Create: `skills/creative/apple-design/references/interaction-physics.md`
- Create: `skills/creative/apple-design/references/materials-type-accessibility.md`
- Create: `skills/creative/apple-design/references/design-principles.md`
- Create: `skills/creative/apple-design/references/UPSTREAM_LICENSE.txt`
- Test: `tests/skills/test_apple_design_skill.py`

- [ ] **Step 1: Add the exact upstream MIT notice**

Create `references/UPSTREAM_LICENSE.txt` with exactly the
`EXPECTED_UPSTREAM_LICENSE` value from Task 1, including the trailing newline.
Do not substitute Emil Kowalski into the copyright line: Emil is the skill
author; Matt Pocock is the copyright holder named by the pinned repository
license.

- [ ] **Step 2: Write the interaction-physics reference**

Create `references/interaction-physics.md` using this section contract:

```markdown
# Interaction Physics

> These values are starting points from the pinned source, not product
> requirements. Existing behavior and measured evidence win.

## Immediate and Continuous Response
## One-to-One Direct Manipulation
## Interruptibility and Presentation-Value Continuity
## Springs as Behavior
## Velocity Sampling and Handoff
## Momentum Projection and Snap Points
## Spatial Consistency and Reversible Paths
## Intermediate Motion Signals the Destination
## Rubber-Banding at Boundaries
## Gesture Disambiguation and Cancellation
## Frame-Level Smoothness
## Starting Points, Not Requirements
## Framework and Dependency Boundary
```

Populate it by adapting upstream sections 1-11. Preserve these concrete
technical elements:

- Pointer Events, grab offset, `setPointerCapture`, and a short timestamped
  position history.
- Start interruption from the live presentation value, never the stale target.
- Keep X and Y motion independently retargetable.
- Critically damped starting point `1.0` with response `0.3-0.4`; reserve
  approximately `0.8` damping for momentum-driven overshoot.
- Explain absolute velocity and guarded normalization:

  ```js
  const distance = targetValue - currentValue;
  const relativeVelocity = distance === 0 ? 0 : gestureVelocity / distance;
  ```

- Preserve exponential projection and snap-point selection:

  ```js
  function project(initialVelocity, decelerationRate = 0.998) {
    return (initialVelocity / 1000) *
      decelerationRate / (1 - decelerationRate);
  }

  const projectedEndpoint = currentPosition + project(releaseVelocity);
  const target = nearestSnapPoint(projectedEndpoint);
  ```

- Preserve the rubber-band relationship with constant `0.55`, approximately
  `10px` gesture hysteresis, symmetric enter/exit paths, source-anchored
  origins, `requestAnimationFrame`, and compositor-friendly properties.
- State that pointer-down may begin visual feedback, while activation still
  follows click, keyboard, cancellation, and assistive-technology semantics.
- If Motion/Framer Motion is mentioned, label it as an optional mapping only
  for projects already using it. Never prescribe a package addition.

- [ ] **Step 3: Write the materials, type, and accessibility reference**

Create `references/materials-type-accessibility.md` with these sections:

```markdown
# Materials, Type, and Accessibility

## Existing Product Language Comes First
## Optional Materials and Depth
## Progressive Enhancement and Performance
## Multimodal Feedback
## Reduced Motion
## Reduced Transparency
## Increased Contrast
## Typography
## Input and Activation Semantics
## Verification Matrix
```

Adapt upstream sections 12-15 while making every aesthetic prescription
conditional:

- Start from a solid, readable surface; use `backdrop-filter` only when it
  improves hierarchy, is supported, and performs on target devices.
- Explain scrims for modal focus, translucent separation for parallel flow,
  surface stacking, and legibility over changing backgrounds.
- Keep sound, haptics, and vibration optional, synchronized, causal, useful,
  and nonessential to task completion.
- Include independent fallbacks for `prefers-reduced-motion`,
  `prefers-reduced-transparency`, and `prefers-contrast`.
- Preserve state feedback in reduced motion with short cross-fades or static
  transitions rather than removing feedback entirely.
- Cover `font-optical-sizing`, size-aware tracking and leading, scalable
  `rem`/`em` layout, and platform-aware fonts.
- Say explicitly that existing brand typography outranks the system-font
  heuristic.
- Say explicitly that visual pointer-down feedback never changes semantic
  activation, keyboard, cancellation, or assistive-technology behavior.

- [ ] **Step 4: Write the design-principles reference**

Create `references/design-principles.md` with these sections:

```markdown
# Design Principles for Interaction Review

## Human Needs
## Purpose
## Agency
## Responsibility
## Familiarity
## Flexibility
## Simplicity
## Craft
## Delight
## Tactical Review Questions
## Prototype and Test in Real Context
## Review Scorecard
```

Frame the four human needs and eight principles as review questions, never an
Apple visual-style mandate. Include feedback types, wayfinding,
grouping/mapping, direct labels, interactive prototypes, testing in real
context, and frame-by-frame motion inspection. The scorecard must ask whether
the design preserves user control, matches the product's existing language,
works across relevant inputs and abilities, and earns every motion/material
choice.

- [ ] **Step 5: Write the compact SKILL.md router**

Create `skills/creative/apple-design/SKILL.md` from this exact operating
skeleton. Keep detailed formulas and long explanations in the references.

```markdown
---
name: apple-design
description: Use when designing gesture-driven UI or physical web motion.
version: 1.0.0
author: Emil Kowalski (emilkowalski), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, interaction, motion, gestures, springs, accessibility, web]
    related_skills: [claude-design, design-md, popular-web-designs]
---

# Apple Design Skill

Use this specialist for gesture-driven interfaces and physical web motion. It
is not an Apple visual-style preset: preserve the project's brand, components,
tokens, platform behavior, accessibility requirements, and dependency policy.

The core invariant is continuity: physical motion starts from the live
presentation value, carries gesture velocity where relevant, projects momentum
when useful, and remains interruptible and reversible.

## When to Use

Load this skill when the request explicitly asks for Apple-like, fluid,
physical, or tactile interaction, or when the work involves drag, swipe,
throw, flick, snap points, sheets, drawers, carousels, rubber-banding,
interruptible springs, velocity handoff, momentum, or motion-quality review.
Also load it for significant translucent-material or depth behavior that needs
reduced-transparency or higher-contrast alternatives.

Do not load it merely for static layout, spacing, hierarchy, color, copy,
icons, ordinary responsive CSS, generic UI, DESIGN.md token authoring, known
brand matching, typography alone, or accessibility work without physical-
interaction or significant translucent-material scope.

## Prerequisites

There are no external tools, packages, credentials, or runtime prerequisites.
Read the repository instructions and existing design system before applying
this guidance.

## How to Run

Load this file with `skill_view(name="apple-design")`, then load only the
reference material needed for the task with, for example,
`skill_view(name="apple-design", file_path="references/interaction-physics.md")`:

| Need | Reference |
|---|---|
| Drag, springs, interruption, velocity, momentum, boundaries | [`interaction-physics.md`](references/interaction-physics.md) |
| Materials, typography, multimodal feedback, accessibility alternatives | [`materials-type-accessibility.md`](references/materials-type-accessibility.md) |
| Product principles, prototyping, and interaction review | [`design-principles.md`](references/design-principles.md) |

## Quick Reference

Apply guidance in this precedence order:

1. User instructions and repository contracts such as `AGENTS.md`,
   `DESIGN.md`, tokens, components, and accessibility requirements.
2. Existing product behavior and platform conventions.
3. Measured usability, performance, and compatibility evidence.
4. This skill's heuristics and numerical starting points.

Use `claude-design` as the general process and taste layer. Add `design-md`
for a persistent token specification and `popular-web-designs` when a known
product supplies the visual reference.

## Procedure

1. Read the repository instructions, current implementation, design system,
   and locked constraints.
2. State the interaction's purpose, frequency, input methods, start state,
   intermediate behavior, completion state, cancellation path, and
   reduced-motion equivalent.
3. Load only the reference file or files relevant to the interaction.
4. Choose the simplest mechanism that preserves the required behavior in the
   project's existing stack.
5. Prototype and exercise the interaction, including interruption and reversal
   when relevant.
6. Verify pointer, touch, keyboard, assistive technology, reduced motion,
   performance, responsive behavior, and product-design fidelity in proportion
   to the task.

## Pitfalls

- Do not make the product look like Apple unless the user asked for that.
- Do not force springs, bounce, glass, blur, system fonts, press scaling,
  sound, or haptics into an established component language.
- Do not convert visual pointer-down feedback into premature semantic
  activation.
- Do not add Motion, Framer Motion, or another dependency unless the project
  already uses it or the user separately approves it.
- Do not treat numerical examples as universal thresholds.
- Do not trade legibility, accessibility, input semantics, or frame stability
  for visual effect.

## Verification

Use the target project's existing checks and exercise the interaction in its
real context. Confirm live-value interruption, clean reversal, correct
velocity handoff when applicable, pointer/touch/keyboard behavior,
assistive-technology semantics, reduced-motion and contrast alternatives,
responsive behavior, performance, and fidelity to the existing design system.

## Attribution

Adapted from [Emil Kowalski's `apple-design` skill](https://raw.githubusercontent.com/emilkowalski/skills/56de6f5d6642f761b5e17629fccf53e303b3da9b/skills/apple-design/SKILL.md)
at source commit
[`56de6f5d6642f761b5e17629fccf53e303b3da9b`](https://github.com/emilkowalski/skills/commit/56de6f5d6642f761b5e17629fccf53e303b3da9b).
Distributed under MIT; see the [complete upstream notice](references/UPSTREAM_LICENSE.txt).
```

- [ ] **Step 6: Run the package contract test and turn it green**

Run:

```bash
scripts/run_tests.sh tests/skills/test_apple_design_skill.py -q
```

Expected: all tests in `test_apple_design_skill.py` PASS.

- [ ] **Step 7: Inspect the package for accidental extras and malformed links**

Run:

```bash
git diff --check
if rg -n 'npm (install|add)|pnpm (install|add)|yarn add|pip3? install' skills/creative/apple-design; then
  exit 1
fi
rg -n '\]\(references/[^)]+\)' skills/creative/apple-design/SKILL.md
```

Expected: `git diff --check` passes; the dependency-command search returns no
matches; the four intended reference links are visible.

- [ ] **Step 8: Commit the green package and its contract test**

```bash
git add \
  skills/creative/apple-design \
  tests/skills/test_apple_design_skill.py
git commit -m "feat(skills): add apple interaction design specialist"
```

### Task 3: Route physical design work through `claude-design`

**Files:**

- Modify: `tests/skills/test_apple_design_skill.py`
- Modify: `skills/creative/claude-design/SKILL.md:1-40`
- Modify: `skills/creative/claude-design/SKILL.md:92-120`
- Test: `tests/skills/test_apple_design_skill.py`
- Regression test: `tests/agent/test_prompt_builder.py`

- [ ] **Step 1: Add failing bidirectional-routing tests**

Add this constant beside the other paths in
`tests/skills/test_apple_design_skill.py`:

```python
CLAUDE_DESIGN_MD = REPO_ROOT / "skills" / "creative" / "claude-design" / "SKILL.md"
```

Append these tests:

```python
def test_related_design_skills_are_bidirectional(parsed_skill) -> None:
    apple_frontmatter, _ = parsed_skill
    claude_source = _read(CLAUDE_DESIGN_MD)
    claude_frontmatter, _ = parse_frontmatter(claude_source)

    assert "claude-design" in apple_frontmatter["metadata"]["hermes"]["related_skills"]
    assert "apple-design" in claude_frontmatter["metadata"]["hermes"]["related_skills"]


def test_claude_design_has_positive_and_negative_routing_boundaries() -> None:
    source = _read(CLAUDE_DESIGN_MD)

    assert "| **apple-design**" in source
    assert "## Physical Interaction Routing" in source
    assert "Load `apple-design` alongside this skill when" in source
    assert "Do not load `apple-design` merely for static layout" in source

    for positive_term in (
        "drag",
        "swipe",
        "sheet",
        "snap points",
        "velocity",
        "momentum",
        "interruptible",
        "rubber-banding",
        "translucent",
        "reduced-transparency",
        "higher-contrast",
    ):
        assert positive_term in source

    for negative_term in (
        "spacing",
        "hierarchy",
        "color",
        "copy",
        "icons",
        "DESIGN.md",
    ):
        assert negative_term in source
```

- [ ] **Step 2: Run only the new routing tests and prove they fail**

Run:

```bash
scripts/run_tests.sh \
  tests/skills/test_apple_design_skill.py \
  -q \
  -k 'related_design_skills_are_bidirectional or claude_design_has_positive_and_negative_routing_boundaries'
```

Expected: both tests FAIL because `claude-design` does not yet name or route
to `apple-design`.

- [ ] **Step 3: Add the related-skill metadata and decision-table entry**

In `skills/creative/claude-design/SKILL.md`:

1. Add `apple-design` to `metadata.hermes.related_skills`.
2. Change “Hermes has three design-related skills” to “Hermes has four
   design-related skills.”
3. Add this row to the decision table:

```markdown
| **apple-design** | Physical interaction and material craft — direct manipulation, interruption, springs, velocity, momentum, boundaries, translucent depth, and accessibility alternatives | Apple-like or tactile behavior, drag/swipe/sheet interactions, snap points, interruptible motion, significant translucent materials, or a focused gesture/motion review |
```

4. Add this rule-of-thumb line:

```markdown
- **Physical interaction behavior** → claude-design + apple-design
- **Significant translucent material behavior** → claude-design + apple-design
```

5. Extend the composition sentence to say that `apple-design` supplies
   physical-interaction and significant material behavior while
   `claude-design` continues to own the general process and artifact
   verification.

- [ ] **Step 4: Add the explicit routing boundary**

Immediately after the existing `## When To Use` block and its DESIGN.md
exception, insert:

```markdown
## Physical Interaction Routing

Load `apple-design` alongside this skill when the user explicitly wants an
Apple-like, fluid, physical, or tactile interaction, or when the work involves
drag, swipe, throw, flick, sheets, drawers, carousels, snap points,
rubber-banding, live-value interruption, velocity handoff, momentum projection,
focused gesture/motion review, or significant translucent-material behavior
that needs reduced-transparency or higher-contrast alternatives.

Do not load `apple-design` merely for static layout, spacing, hierarchy, color,
copy, icons, ordinary responsive CSS, generic UI, or DESIGN.md token authoring.
Typography or accessibility alone is also insufficient unless the task has
Apple-style, physical-interaction, or significant translucent-material scope.

When it does apply, keep this skill as the general design-process layer and use
`apple-design` only for the qualifying physical-interaction or significant
material behavior. Existing repository design contracts, tokens, component
APIs, accessibility requirements, and measured evidence remain authoritative.
```

- [ ] **Step 5: Run the skill and prompt-builder tests**

Run:

```bash
scripts/run_tests.sh \
  tests/skills/test_apple_design_skill.py \
  tests/agent/test_prompt_builder.py \
  -q
```

Expected: all tests PASS, including the compact 60-character description path
through `extract_skill_description`.

- [ ] **Step 6: Review the source diff for unintended general-design changes**

Run:

```bash
git diff --check
git diff -- skills/creative/claude-design/SKILL.md tests/skills/test_apple_design_skill.py
```

Expected: only metadata, the four-skill routing table/composition text, the
explicit physical-interaction boundary, and its contract tests changed. Do not
rewrite unrelated `claude-design` doctrine.

- [ ] **Step 7: Commit the routing change**

```bash
git add \
  skills/creative/claude-design/SKILL.md \
  tests/skills/test_apple_design_skill.py
git commit -m "feat(skills): route physical design work to apple specialist"
```

### Task 4: Regenerate documentation without absorbing existing drift

**Files:**

- Create: `website/docs/user-guide/skills/bundled/creative/creative-apple-design.md`
- Modify: `website/docs/user-guide/skills/bundled/creative/creative-claude-design.md`
- Modify: `website/docs/reference/skills-catalog.md`
- Modify: `website/sidebars.ts`
- Verify: `website/static/api/skills.json` (ignored build artifact)
- Verify: `website/static/api/skills-meta.json` (ignored, nondeterministic build artifact)

The current branch-base generator output has unrelated tracked and untracked
drift. Do not stage a raw whole-repository generator diff. The steps below
mechanically three-way the feature delta onto the tracked catalog/sidebar,
restore tracked drift, and remove only the untracked pages created by this
generator run.

- [ ] **Step 1: Require a clean source state and create an isolated generator baseline**

Run:

```bash
test -z "$(git status --porcelain)"
apple_docs_tmp="$(git rev-parse --git-path apple-design-docs-tmp)"
test ! -e "$apple_docs_tmp"
mkdir -p "$apple_docs_tmp/base" "$apple_docs_tmp/current" "$apple_docs_tmp/feature"
apple_base_ref="$(git merge-base HEAD origin/main)"
git archive "$apple_base_ref" | tar -x -C "$apple_docs_tmp/base"
python3 "$apple_docs_tmp/base/website/scripts/generate-skill-docs.py"
```

Expected: the source worktree is clean; the temporary baseline contains the
generator output for the branch's exact merge-base without touching the live checkout.
Keep the printed `apple_docs_tmp` value available for the remaining steps.

- [ ] **Step 2: Snapshot the tracked shared files before generation**

Run:

```bash
apple_docs_tmp="$(git rev-parse --git-path apple-design-docs-tmp)"
test -d "$apple_docs_tmp/base"
cp website/docs/reference/skills-catalog.md \
  "$apple_docs_tmp/current/skills-catalog.md"
cp website/sidebars.ts \
  "$apple_docs_tmp/current/sidebars.ts"
```

- [ ] **Step 3: Run the metadata extractor and docs generator**

Run:

```bash
python3 website/scripts/extract-skills.py
python3 website/scripts/generate-skill-docs.py
```

Expected:

- `website/static/api/skills.json` contains a built-in `apple-design` record
  whose `docsPath` is `bundled/creative/creative-apple-design`.
- The generator creates the Apple page, updates the Claude page, and rewrites
  catalogs/sidebar along with known unrelated drift.
- `website/static/api/skills-meta.json` changes its ignored `extractedAt`; never
  stage it.

Verify the extracted record:

```bash
rg -o '"name":"apple-design"[^}]*"docsPath":"bundled/creative/creative-apple-design"' \
  website/static/api/skills.json
```

- [ ] **Step 4: Snapshot feature-generated shared files and three-way only the feature delta**

Run:

```bash
set -e
apple_docs_tmp="$(git rev-parse --git-path apple-design-docs-tmp)"
test -d "$apple_docs_tmp/base"
cp website/docs/reference/skills-catalog.md \
  "$apple_docs_tmp/feature/skills-catalog.md"
cp website/sidebars.ts \
  "$apple_docs_tmp/feature/sidebars.ts"

git merge-file \
  "$apple_docs_tmp/current/skills-catalog.md" \
  "$apple_docs_tmp/base/website/docs/reference/skills-catalog.md" \
  "$apple_docs_tmp/feature/skills-catalog.md"
git merge-file \
  "$apple_docs_tmp/current/sidebars.ts" \
  "$apple_docs_tmp/base/website/sidebars.ts" \
  "$apple_docs_tmp/feature/sidebars.ts"

cp "$apple_docs_tmp/current/skills-catalog.md" \
  website/docs/reference/skills-catalog.md
cp "$apple_docs_tmp/current/sidebars.ts" \
  website/sidebars.ts
```

Expected: both `git merge-file` commands exit 0 with no conflict markers. The
tracked catalog and sidebar keep their existing state plus only the generated
`apple-design` row/item.

- [ ] **Step 5: Restore tracked drift and remove generated untracked pages**

Restore exactly these tracked paths produced by the current generator mismatch:

```bash
git restore -- \
  website/docs/reference/optional-skills-catalog.md \
  website/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent.md \
  website/docs/user-guide/skills/bundled/creative/creative-humanizer.md \
  website/docs/user-guide/skills/bundled/email/email-himalaya.md \
  website/docs/user-guide/skills/bundled/research/research-research-paper-writing.md \
  website/docs/user-guide/skills/bundled/software-development/software-development-hermes-agent-skill-authoring.md \
  website/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging.md \
  website/docs/user-guide/skills/bundled/software-development/software-development-test-driven-development.md \
  website/docs/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-antigravity-cli.md \
  website/docs/user-guide/skills/optional/creative/creative-kanban-video-orchestrator.md
```

The same generator run creates five files that are absent from the branch
base. Prove they are untracked generated outputs, then remove only those exact
files:

```bash
for generated_page in \
  website/docs/user-guide/skills/bundled/computer-use/computer-use-computer-use.md \
  website/docs/user-guide/skills/bundled/hermes-desktop-plugins/hermes-desktop-plugins-hermes-desktop-plugins.md \
  website/docs/user-guide/skills/optional/creative/creative-unreal-mcp.md \
  website/docs/user-guide/skills/optional/security/security-unbroker.md \
  website/docs/user-guide/skills/optional/web-development/web-development-cloudflare-temporary-deploy.md; do
  test -f "$generated_page"
  test -z "$(git ls-files -- "$generated_page")"
done

rm -- \
  website/docs/user-guide/skills/bundled/computer-use/computer-use-computer-use.md \
  website/docs/user-guide/skills/bundled/hermes-desktop-plugins/hermes-desktop-plugins-hermes-desktop-plugins.md \
  website/docs/user-guide/skills/optional/creative/creative-unreal-mcp.md \
  website/docs/user-guide/skills/optional/security/security-unbroker.md \
  website/docs/user-guide/skills/optional/web-development/web-development-cloudflare-temporary-deploy.md
```

Do not restore either intended creative page, the bundled catalog, or the
sidebar.

- [ ] **Step 6: Prove the uncommitted generated-path allowlist**

Run:

```bash
diff -u \
  <(printf '%s\n' \
    website/docs/reference/skills-catalog.md \
    website/docs/user-guide/skills/bundled/creative/creative-apple-design.md \
    website/docs/user-guide/skills/bundled/creative/creative-claude-design.md \
    website/sidebars.ts | sort) \
  <({ git diff --name-only; git ls-files --others --exclude-standard; } | sort)
```

Expected: no diff. If another path appears, inspect it and restore it only if
it is generator drift; do not expand the PR allowlist casually.

- [ ] **Step 7: Inspect the four generated artifacts**

Run:

```bash
git diff --check
git diff -- \
  website/docs/reference/skills-catalog.md \
  website/docs/user-guide/skills/bundled/creative/creative-claude-design.md \
  website/sidebars.ts
rg -n 'apple-design|Emil Kowalski|Matt Pocock|56de6f5d' \
  website/docs/user-guide/skills/bundled/creative/creative-apple-design.md \
  website/docs/reference/skills-catalog.md \
  website/sidebars.ts
```

Expected: the new page is generated from `SKILL.md`; the Claude page mirrors
the source routing update; the catalog/sidebar contain one correctly sorted
Apple entry; no conflict markers or unrelated generated changes remain.

- [ ] **Step 8: Run website generator tests, diagram lint, and production build**

Run:

```bash
scripts/run_tests.sh \
  tests/website/test_generate_skill_docs.py \
  tests/website/test_extract_skills.py \
  -q

if [ ! -d website/node_modules ]; then
  npm --prefix website ci
fi
npm --prefix website run lint:diagrams
npm --prefix website run build
```

Expected: Python tests PASS, diagram lint exits 0, and Docusaurus reports a
successful production build. The build may refresh ignored skills JSON,
skills-index, and llms files; do not stage them.

- [ ] **Step 9: Recheck the allowlist and commit the generated docs**

Repeat Step 6, then commit only the four intended paths:

```bash
git add \
  website/docs/reference/skills-catalog.md \
  website/docs/user-guide/skills/bundled/creative/creative-apple-design.md \
  website/docs/user-guide/skills/bundled/creative/creative-claude-design.md \
  website/sidebars.ts
git diff --cached --check
git commit -m "docs(skills): publish apple design specialist"

apple_docs_tmp="$(git rev-parse --git-path apple-design-docs-tmp)"
case "$apple_docs_tmp" in
  */apple-design-docs-tmp) rm -rf -- "$apple_docs_tmp" ;;
  *) echo "Refusing unexpected cleanup path: $apple_docs_tmp" >&2; exit 1 ;;
esac
```

### Task 5: Forward-test selection and run the final gate

**Files:**

- Read: `skills/creative/apple-design/SKILL.md`
- Read: `skills/creative/claude-design/SKILL.md`
- Temporary: isolated skill-index snapshot under `mktemp -d`
- No tracked file changes expected

- [ ] **Step 1: Build the actual compact Hermes skill index without touching user state**

Run from the repository root:

```bash
apple_eval_home="$(git rev-parse --git-path apple-design-eval-home)"
test ! -e "$apple_eval_home"
mkdir -p "$apple_eval_home"
ln -s "$PWD/skills" "$apple_eval_home/skills"
HERMES_HOME="$apple_eval_home" python3 -c \
  'from agent.prompt_builder import build_skills_system_prompt; print(build_skills_system_prompt())' \
  > "$apple_eval_home/skills-index.txt"
rg -n 'apple-design|claude-design|design-md|popular-web-designs' \
  "$apple_eval_home/skills-index.txt"
```

Expected: the real compact index shows the full, untruncated 60-character
`apple-design` description and the existing complementary design skills. The
temporary `HERMES_HOME` prevents prompt snapshots or skill state from touching
the user's real Hermes installation.

- [ ] **Step 2: Dispatch seven context-isolated selection evaluations**

For each prompt below, dispatch a fresh agent with no conversation history.
Resolve the stable index path with
`git rev-parse --git-path apple-design-eval-home`, append
`skills-index.txt`, and give the fresh agent only that path, the user request,
and this neutral instruction:

```text
Read the supplied Hermes compact skill index only. Before attempting the user
request, return JSON with the skill_view names you would load and one short
reason. Do not inspect any skill body before selecting. Do not assume a skill
must be selected merely because it exists in the index.
```

Use one fresh agent per request:

1. `Build a bottom sheet that follows the finger and can be reversed while it is settling.`
2. `Review this swipe-to-dismiss card; the release feels disconnected from the gesture.`
3. `Make this prototype feel intentionally Apple-like while preserving its existing brand tokens.`
4. `Clean up the spacing and information hierarchy in this dashboard.`
5. `Write a DESIGN.md token specification for this brand.`
6. `Rewrite the copy and icons on this static landing page.`
7. `Design a translucent control surface with reduced-transparency and higher-contrast alternatives.`

Do not include the expected classifications in the evaluator prompts. Preserve
the first response from each evaluator. If an evaluator selects
`claude-design`, give that same evaluator only the loaded `claude-design` body
and ask whether its routing rules require another `skill_view` call before
acting. Record the complete first- and second-hop load chain in the task notes
or PR body; do not add a tracked results file and do not disclose the expected
classification between hops.

- [ ] **Step 3: Grade selection only after all seven responses return**

Acceptance:

- Cases 1-3 and 7 include `apple-design` by the end of the load chain. Case 7
  may reach it through `claude-design`'s explicit material-routing rule.
- Cases 4-6 do not include `apple-design` unless the evaluator independently
  identifies additional physical-interaction scope in a supplied artifact.
- Case 5 may select `design-md`.
- General artifact work may also select `claude-design`; the key assertion is
  the specialist boundary.

If selection fails, adjust the description or `claude-design` routing only
from the observed evidence, rerun Tasks 3-4 as needed, and repeat all seven fresh
evaluations. Do not solve under-triggering by making `apple-design` universal.

- [ ] **Step 4: Forward-test project-contract preservation with the loaded body**

Use one additional fresh agent. Give it the implemented `apple-design` skill,
a small fictional existing brand contract that specifies a non-system typeface
and opaque surfaces, and this request:

```text
Make this drag-to-dismiss prototype feel intentionally Apple-like while
preserving the supplied brand contract. Return the implementation approach and
the verification checklist; do not write files.
```

Do not tell the evaluator which guardrails are expected. Accept only if it
preserves the brand typeface and opaque-surface rule while applying interaction
continuity, interruption, velocity, and accessibility guidance. Reject a plan
that blindly adds system fonts, glass, blur, or a new animation dependency.

- [ ] **Step 5: Run the complete focused verification gate**

Run:

```bash
scripts/run_tests.sh \
  tests/skills/test_apple_design_skill.py \
  tests/agent/test_prompt_builder.py \
  tests/website/test_generate_skill_docs.py \
  tests/website/test_extract_skills.py \
  -q
npm --prefix website run lint:diagrams
npm --prefix website run build
apple_base_ref="$(git merge-base HEAD origin/main)"
git diff --check "$apple_base_ref"...HEAD
test -z "$(git diff --name-only "$apple_base_ref"...HEAD -- \
  agent/prompt_builder.py agent/skill_utils.py tools/skills_tool.py CHANGELOG.md \
  package.json package-lock.json website/package.json website/package-lock.json)"
git status --short
git log --oneline "$apple_base_ref"..HEAD

apple_eval_home="$(git rev-parse --git-path apple-design-eval-home)"
case "$apple_eval_home" in
  */apple-design-eval-home) rm -rf -- "$apple_eval_home" ;;
  *) echo "Refusing unexpected cleanup path: $apple_eval_home" >&2; exit 1 ;;
esac
```

Expected:

- All focused tests PASS.
- Diagram lint and production build exit 0.
- No whitespace errors.
- The protected runtime, changelog, and package paths remain unchanged.
- `git status --short` is empty.
- The branch contains the approved design/plan commits followed by the three
  implementation commits listed above.

- [ ] **Step 6: Review the final branch scope**

Run:

```bash
apple_base_ref="$(git merge-base HEAD origin/main)"
git diff --stat "$apple_base_ref"...HEAD
git diff --name-status "$apple_base_ref"...HEAD
```

The final implementation scope should contain only:

- the approved design and implementation-plan documents,
- the five-file `apple-design` package,
- the narrow `claude-design` source update,
- the skill contract test,
- the four intended generated documentation artifacts.

Report the seven selection results, the project-contract preservation result,
the exact test/build receipts, and any limitation. Do not claim automatic
selection is verified if the forward evaluations were skipped.
