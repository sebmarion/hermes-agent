---
title: "Apple design specialist skill integration"
status: proposed
date: 2026-07-17
type: design
target_repo: hermes-agent
origin: user-approved architecture decision
---

# Apple Design Specialist Skill Integration

## Decision summary

Hermes Agent will ship an adapted, attributed `apple-design` bundled skill for
gesture-driven interfaces and physical web motion. The skill will be available
by default but will not become a blanket design persona. Hermes will load it
when a request has explicit Apple-style intent or clearly involves physical
interaction behavior such as drag, swipe, sheets, snap points, momentum,
springs, interruption, rubber-banding, or motion-focused review.

The integration will use Hermes's existing skill discovery and mandatory skill
loading behavior. It will not add a core tool, a new prompt-builder branch, a
runtime auto-loader, or a dependency. `claude-design` will remain the general
design-process skill and will route qualifying work to `apple-design`.

## Context

Hermes currently has three complementary bundled web-design skills:

- `claude-design` supplies the general design process, taste, artifact workflow,
  and visual verification discipline.
- `design-md` supplies persistent design-token specifications.
- `popular-web-designs` supplies reference vocabularies from real products.

`claude-design` currently gives motion broad guidance: motion should clarify
state, preserve continuity, remain subtle, and respect reduced-motion settings.
It does not provide implementation-level guidance for direct manipulation,
velocity handoff, momentum projection, interruptible springs, rubber-banding,
or material behavior.

Emil Kowalski's upstream `apple-design` skill fills that specialist gap. It
distills Apple interface and motion guidance into web-oriented principles and
examples. The approved source snapshot is:

- Repository: `https://github.com/emilkowalski/skills`
- Skill: `skills/apple-design/SKILL.md`
- Source commit: `56de6f5d6642f761b5e17629fccf53e303b3da9b`
- License: MIT, copyright Emil Kowalski (2026)

The source is valuable but cannot be copied unchanged. Its discovery
description exceeds Hermes's 60-character limit, its trigger boundary is broad,
and several prescriptions are written as universal rules even though they are
context-dependent on the web. Hermes needs an attributed adaptation with
explicit routing, precedence, accessibility, compatibility, and dependency
guardrails.

## Goals

1. Preserve the upstream skill's useful interaction expertise and concrete
   values without making Apple aesthetics Hermes's default design identity.
2. Make the skill discoverable and automatically selected for the interaction
   tasks where it materially improves output.
3. Keep general layout, branding, token, and static visual work routed through
   the existing design skills.
4. Preserve project-level design contracts, existing component systems,
   accessibility semantics, performance constraints, and dependency policies.
5. Vendor a reproducible source snapshot with complete attribution and license
   notice.
6. Verify both positive and negative trigger behavior before treating the skill
   as production-ready.

## Non-goals

- Redesigning Hermes WebUI, Hermes Desktop, or any other product surface.
- Making every Hermes-designed interface look like an Apple product.
- Implementing the previously proposed `frontend-design`,
  `product-design-review`, or `iterateux` skills in this change.
- Importing the entire `emilkowalski/skills` repository.
- Adding `Motion`, Framer Motion, or any other JavaScript dependency.
- Adding a new skill-selection algorithm or changing system-prompt caching.
- Automatically applying glass, translucency, springs, system fonts, haptics,
  bounce, or press scaling to every interface.
- Treating the skill's numerical defaults as product requirements when measured
  behavior or an existing design system calls for different values.

## Options considered

### 1. Adapted conditional specialist — selected

Ship `apple-design` as a bundled specialist, add a narrow trigger description,
and route to it from `claude-design` only for Apple-style or physical-interaction
work.

This preserves the domain expertise, keeps the skill independently maintainable,
and avoids loading its full body into unrelated design tasks.

### 2. Copy upstream unchanged and load for all design tasks — rejected

The upstream description would be truncated in Hermes's compact skill index,
making selection less reliable. Its broad rules would also bias static,
enterprise, data-dense, non-Apple, and established-brand work toward an
unrequested interaction and visual language.

### 3. Merge the guidance into `claude-design` — rejected

This would make an already-large general skill larger, duplicate a distinct
source of expertise, and load detailed gesture physics for every one-off design
artifact. A narrow specialist gives clearer ownership and better progressive
disclosure.

## Package topology

Implementation will add and modify these source files:

```text
skills/creative/apple-design/
├── SKILL.md
└── references/
    ├── interaction-physics.md
    ├── materials-type-accessibility.md
    ├── design-principles.md
    └── UPSTREAM_LICENSE.txt

skills/creative/claude-design/SKILL.md
tests/skills/test_apple_design_skill.py
```

The normal documentation generator will add or update the corresponding
generated files, including:

```text
website/docs/user-guide/skills/bundled/creative/creative-apple-design.md
website/docs/reference/skills-catalog.md
website/sidebars.ts
```

Only files actually changed by `website/scripts/generate-skill-docs.py` will be
committed. The implementation will not hand-edit generated skill documentation.

## Skill metadata contract

`skills/creative/apple-design/SKILL.md` will use this frontmatter shape:

```yaml
---
name: apple-design
description: Use for gesture-driven UI and physical web motion.
version: 1.0.0
author: Emil Kowalski (emilkowalski), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, interaction, motion, gestures, springs, accessibility, web]
    related_skills: [claude-design, design-md, popular-web-designs]
---
```

The short description is both trigger-focused and fully visible in Hermes's
60-character compact index. The skill is cross-platform because it contains
web guidance and no platform-specific executable code.

The human source author is credited first. `UPSTREAM_LICENSE.txt` will contain
the complete upstream MIT notice rather than relying only on the frontmatter
license field or a link.

## Trigger and routing contract

### Load `apple-design` when

- The user explicitly asks for an Apple-like, iOS-like, fluid, physical, or
  tactile interaction style.
- The interface includes drag, swipe, throw, flick, snap points, carousels,
  bottom sheets, drawers, draggable cards, direct manipulation, or
  rubber-banding.
- Motion must inherit gesture velocity, project momentum, remain interruptible,
  or reverse cleanly from its current presentation value.
- The task is a focused review of gesture or motion quality.
- Significant motion or translucent material behavior needs reduced-motion,
  reduced-transparency, or higher-contrast alternatives.

### Do not load `apple-design` merely because

- The task changes static layout, spacing, hierarchy, color, copy, icons, or
  ordinary responsive CSS.
- The user wants a landing page, dashboard, component, or generic UI without a
  physical-interaction requirement.
- The task authors a `DESIGN.md` token specification.
- The task matches a known brand reference already handled by
  `popular-web-designs`.
- Typography or accessibility is mentioned without Apple-style or
  interaction-motion scope.

### Composition rules

- Use `claude-design` as the general process and taste layer.
- Add `apple-design` when the positive trigger contract is satisfied.
- Add `design-md` when persistent tokens are part of the deliverable.
- Add `popular-web-designs` when a known product supplies the visual reference.
- A single task may use more than one skill, but `apple-design` never replaces
  the general design brief, project context, or design-system source of truth.

`claude-design` will be updated in three places:

1. Add `apple-design` to `metadata.hermes.related_skills`.
2. Add it to the design-skill decision table as the physical-interaction and
   motion specialist.
3. Add an explicit routing rule using the positive and negative boundaries
   above.

No change to `agent/prompt_builder.py`, `agent/skill_utils.py`, or
`tools/skills_tool.py` is required. Their existing behavior already exposes the
short skill description and tells the model to load relevant skills.

## Content adaptation

### `SKILL.md`

The main file will be a concise router and operating procedure, using Hermes's
modern section order:

1. `# Apple Design Skill`
2. Two- or three-sentence scope statement
3. `## When to Use`
4. `## Prerequisites`
5. `## How to Run`
6. `## Quick Reference`
7. `## Procedure`
8. `## Pitfalls`
9. `## Verification`
10. `## Attribution`

There are no external prerequisites. `How to Run` will explain that the model
loads the skill with `skill_view` and then reads only the references required
for the task.

The procedure will require the agent to:

1. Read repository instructions and the existing design system before applying
   the specialist guidance.
2. State the interaction's purpose, frequency, input methods, start state,
   intermediate behavior, completion state, cancellation path, and reduced-
   motion equivalent.
3. Load the matching reference file or files.
4. Choose the simplest mechanism that preserves the required behavior, using
   the project's existing stack.
5. Prototype and exercise the interaction, including interruption and reversal
   when relevant.
6. Verify pointer, touch, keyboard, assistive-technology, reduced-motion,
   performance, and responsive behavior in proportion to the task.

### `references/interaction-physics.md`

This reference will preserve and adapt the upstream material on:

- immediate and continuous feedback,
- one-to-one direct manipulation and grab offsets,
- Pointer Events and pointer capture,
- interruptibility and presentation-value continuity,
- spring behavior and restrained bounce,
- velocity handoff,
- exponential momentum projection,
- spatially consistent origins and reversible paths,
- gesture disambiguation and hysteresis,
- rubber-banding,
- frame-level smoothness and compositor-aware implementation,
- a concise table of starting values.

Exact values will be labeled as starting points from the cited source, not
universal acceptance thresholds. Examples will prefer framework-neutral web
concepts. Library-specific snippets may remain as clearly marked mappings, but
they will not instruct the agent to install a library.

### `references/materials-type-accessibility.md`

This reference will preserve and adapt the upstream material on:

- translucent materials and depth,
- hierarchy and focus treatment,
- synchronized multimodal feedback,
- reduced motion, reduced transparency, and increased contrast,
- optical sizing, tracking, leading, scalable typography, and platform-aware
  type choices.

It will require progressive enhancement and fallbacks for unsupported browser
features. Expensive effects such as blur and backdrop filtering must be tested
on the actual target devices and removed or simplified when they harm
performance or legibility.

### `references/design-principles.md`

This reference will preserve the upstream principles of purpose, agency,
responsibility, familiarity, flexibility, simplicity, craft, and delight, plus
the interactive-prototyping and real-context testing guidance.

It will frame these as review questions, not as an Apple visual-style mandate.

## Precedence and safety rules

The adapted skill will state this precedence order:

1. User instructions and repository contracts such as `AGENTS.md`,
   `DESIGN.md`, design tokens, component APIs, and accessibility requirements.
2. Existing product behavior and platform conventions.
3. Measured usability, performance, and compatibility evidence.
4. `apple-design` heuristics and numerical starting points.

The following upstream ideas must be made conditional rather than absolute:

- Springs are appropriate for direct manipulation and genuinely interruptible
  physical behavior, not every clickable or touchable control.
- Translucency and blur are optional hierarchy tools, not a default surface
  treatment.
- System fonts are a strong platform-native default, not a replacement for an
  established brand type system.
- Press scaling is one possible feedback treatment and must not fight the
  product's existing component language.
- Visual feedback may begin on pointer-down, but semantic activation must keep
  correct click, keyboard, cancellation, and assistive-technology behavior.
- Sound, vibration, and haptics require platform support, user benefit, and
  restraint; unsupported APIs degrade without breaking the interaction.
- Motion examples must not introduce a dependency or framework that the project
  does not already use.

For Hermes WebUI specifically, its calm-console design contract, token system,
vanilla JavaScript architecture, and prohibition on decorative or theatrical
motion remain authoritative.

## Discovery, caching, and rollout

The skill remains progressive-disclosure content:

1. The compact system prompt contains only `apple-design` and its short
   description.
2. On a matching request, the model loads `SKILL.md` with `skill_view`.
3. `SKILL.md` directs the model to the task-relevant references.
4. Nonmatching design requests never pay the full specialist context cost.

Adding the bundled skill changes the system prompt only when a new conversation
builds its prompt. The implementation will not force mid-conversation prompt
rebuilds or violate Hermes's prompt-cache invariant.

Normal bundled-skill sync will seed `apple-design` for users who have not opted
out of bundled skills. If a user already has a different local skill named
`apple-design`, sync will preserve the user's copy and print the existing reset
hint rather than overwrite it. No migration or destructive replacement is
needed.

## Testing strategy

### Automated contract tests

`tests/skills/test_apple_design_skill.py` will assert behavior contracts rather
than snapshot the full prose:

- Frontmatter parses and satisfies the bundled-skill metadata contract.
- The description is at most 60 characters, is a sentence, and remains
  untruncated through `extract_skill_description`.
- The declared author, source commit, MIT license, and full upstream license
  notice are present.
- Every referenced support file exists and stays inside the skill directory.
- The required modern sections exist.
- `apple-design` points to the existing design skills and `claude-design`
  points back to `apple-design`.
- `claude-design` contains both positive routing and an explicit negative
  boundary, so generic design work is not routed blindly.
- The skill contains no executable script, package-install instruction,
  secret, or runtime dependency.

The tests will avoid asserting the full reference wording, exact line counts,
or a frozen catalog size.

### Forward trigger evaluation

Fresh agents will receive the actual skill index and these task prompts without
being told the expected selection:

Positive cases:

1. “Build a bottom sheet that follows the finger and can be reversed while it
   is settling.”
2. “Review this swipe-to-dismiss card; the release feels disconnected from the
   gesture.”
3. “Make this prototype feel intentionally Apple-like while preserving its
   existing brand tokens.”

Negative cases:

4. “Clean up the spacing and information hierarchy in this dashboard.”
5. “Write a DESIGN.md token specification for this brand.”
6. “Rewrite the copy and icons on this static landing page.”

The first three must load `apple-design`. The last three must not load it unless
the agent discovers additional interaction requirements in the supplied
artifact. Case 3 must also preserve the existing brand contract rather than
blindly applying system fonts, glass, or generic iOS styling.

### Verification commands

Implementation verification will run:

```bash
scripts/run_tests.sh tests/skills/test_apple_design_skill.py tests/agent/test_prompt_builder.py -q
python3 website/scripts/extract-skills.py
python3 website/scripts/generate-skill-docs.py
npm --prefix website run build
git diff --check
```

The generated documentation diff will be reviewed before commit. The forward
trigger evaluation will be recorded with the six prompts, loaded-skill results,
and any observed over-triggering or under-triggering.

## Acceptance criteria

The change is ready for delivery when all of the following are true:

- `apple-design` ships under `skills/creative/` with no external dependency.
- Its compact description is fully visible and selects the intended physical-
  interaction task class.
- `claude-design` routes qualifying tasks to it and keeps generic design work
  on the existing paths.
- The adapted guidance preserves the upstream interaction, materials,
  typography, accessibility, principles, and prototyping substance.
- Project contracts and measured evidence explicitly outrank the specialist's
  heuristics.
- Attribution includes the immutable source commit and complete MIT notice.
- Generated docs and catalog entries are current.
- Focused automated tests, the existing prompt-builder tests, the docs build,
  and diff checks pass.
- All six forward trigger cases behave according to the trigger contract.
- No core tool, prompt-builder branch, mid-session reload, framework, or package
  dependency is introduced.

## Release note wording

Hermes now includes an Apple-inspired interaction-design skill for fluid,
gesture-driven web UI. It is selected for physical motion work such as drags,
swipes, sheets, momentum, and interruptible transitions while existing project
design systems remain authoritative.
