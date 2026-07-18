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
