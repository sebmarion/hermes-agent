---
name: product-design-review
description: "Use when reviewing or improving product UI/UX visual quality, taste, polish, hierarchy, color, typography, density, or design-system coherence before implemented visual changes."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [product-design, ui, ux, visual-design, design-review, taste, design-systems, screenshots]
    related_skills: [popular-web-designs, claude-design, dogfood, plan]
---

# Product Design Review

## Overview

Use this skill as the taste and product-design gate for UI/UX work. Its job is to prevent decorative intensity from being mistaken for good design.

Good product design is not "more visual." It is restrained hierarchy, legible workflow, coherent palette, appropriate density, clear affordances, and a visual system that serves the user's job. Load this skill before mutating visual surfaces and again before calling a design pass complete.

The key behavior is **fail closed**: if the rendered result still looks tasteless, incoherent, over-decorated, or hard to use, say so plainly, do not call it done, and either simplify/revert/iterate or ask for a concrete reference.

## When to Use

Use when the user asks for or complains about:

- visual taste, polish, style, "impressive" design, boring UI, ugly UI, color mismatch, generic cards, or bad hierarchy;
- implemented UX/UI iteration, screenshot-backed design passes, product-surface redesigns;
- design systems, design tokens, component styling, responsive UI, mobile/tablet/POS visual surfaces;
- product dashboards, operational queues, restaurant/POS/procurement screens, checkout/cart/review flows;
- any final design handoff where screenshot evidence could pass while aesthetic quality fails.

Do not use as a substitute for real project context. Always inspect the actual app/design tokens/components first when available.

## Required Design Gate

Before changing visuals, write a compact direction with constraints:

```text
Product/job: <who is using this and what they need to do quickly>
Reference basis: <project design system, existing good screen, or external system inspiration>
Visual direction: <calm/clinical/editorial/operational/etc.>
Typography: <scale, weight, density>
Palette roles: <base, primary action, status, warning, data/category accents>
Spacing/density: <compact/comfortable; target viewport>
Components: <rows/cards/sheets/nav/rail; what should dominate>
Anti-goals: <specific things not to do>
Stop rule: <what makes this unshippable>
```

If you cannot name a defensible reference basis or product job, do not start styling. Ask for a reference or create 2-3 lightweight directions first.

## Professional Reference Sources

Prefer these reference classes over random community prompt/persona skills:

- **Project source of truth:** design tokens, app theme, existing high-quality screens, Storybook, Figma, component docs, `DESIGN.md`.
- **Public design systems:** Apple HIG, Material Design 3, IBM Carbon, Shopify Polaris, Atlassian Design, GOV.UK Design System, Microsoft Fluent, Primer.
- **Bundled style templates:** `popular-web-designs` templates such as Linear, Stripe, Vercel, Apple, IBM, Notion, Figma, and Raycast. Borrow rhythm and constraints, not copyrighted exact styling.
- **Domain references:** POS/restaurant/procurement screens should bias toward operational systems: calm density, fast scanning, strong totals/actions, restrained color semantics.

Public/community skills may be inspected, but do not trust them blindly. A public skill is useful only if it provides concrete product-design principles and no unsafe prompt/persona behavior.

## Taste Rubric

Judge the latest screenshot/product surface across these dimensions:

1. **Primary job clarity** — Can the user tell what to do in under 5 seconds?
2. **Hierarchy** — Does the most important content/action dominate without drowning details?
3. **Typography** — Is scale/weight intentional, readable, and not cartoonish or shouty?
4. **Density** — Does the screen fit the real workflow and viewport without hiding work behind oversized cards?
5. **Palette coherence** — Are base, action, status, warning, and category colors separated?
6. **Semantic color discipline** — Are warning/gold/red reserved for warning states, not normal decoration?
7. **Spacing rhythm** — Are gaps consistent, with no accidental canyons or cramped collisions?
8. **Affordances** — Do buttons, tabs, chips, inputs, and row actions look interactive in the right hierarchy?
9. **Data legibility** — Dense rows/prices/quantities are readable, not placed over busy patterns or glow.
10. **Restraint** — Are gradients, shadows, patterns, animation, and accent colors used sparingly and purposefully?
11. **Brand/system fit** — Does it look like this product, not a random Dribbble shot?
12. **Responsive fit** — Does mobile/tablet/POS preserve hierarchy, touch targets, and no-overflow constraints?

A design can pass tests and fail this rubric. Treat that as a real failure.

## Negative Taste Filter

Reject or remove these unless there is a strong product-specific reason:

- glow soup, fake-premium glassmorphism, random radial gradients, noisy shadows;
- patterned backgrounds behind dense data rows, prices, or controls;
- too many accent colors competing in one viewport;
- warning/gold/orange/red used as normal provider/category identity;
- giant cards where dense rows would serve the workflow better;
- visually equal primary/secondary/destructive actions;
- decorative icons/emoji that make operational work feel toy-like;
- low-contrast muted text on dark backgrounds;
- capsule/chip overload where every metric has the same visual weight;
- dashboard-first layouts when the user needs an action-first workflow;
- "premium" styling not backed by design-system rhythm or product need.

## Operational / Restaurant / POS Rules

For restaurant, procurement, POS, and service-time operations:

- Calm beats wow. Staff need confidence and speed, not spectacle.
- First viewport should expose the current task, blockers, quantities, totals, and next action.
- Product names, quantities, unit/pack math, supplier, subtotal, and send/review action must be legible at a glance.
- Use rows or compact supplier groups for high-frequency transactional data; reserve cards for grouped summaries or decision points.
- Keep one primary action per decision moment.
- Warnings must mean operational risk; do not reuse warning color for ordinary identity.
- Dense cart/order rows should use quiet surfaces, clear dividers, and strong numeric alignment.
- Touch controls need large targets, but the card must not become so tall that it hides the workflow.
- Verify with realistic mobile/tablet/POS viewport screenshots, not only desktop.

## Pre-Implementation Checklist

Before editing UI code/CSS:

- [ ] Inspected the real route/artifact and project-local design context.
- [ ] Captured or reviewed baseline screenshot(s).
- [ ] Wrote the design direction block.
- [ ] Chose a reference basis and stated what is being borrowed.
- [ ] Listed anti-goals specific to this screen.
- [ ] Identified semantic color roles and status colors.
- [ ] Defined mobile/tablet/POS acceptance if relevant.
- [ ] Decided whether rows/cards/rail/sheet are the right primitive for the job.

## Post-Implementation Aesthetic Verdict

Before finalizing, include an explicit verdict:

```text
Aesthetic verdict: pass | fail | needs human reference
Reference basis: <what it is measured against>
Why: <1-3 concrete reasons>
Remaining taste risk: <none | specific issue>
```

Passing requires more than "it rendered". The result should be restrained, coherent, usable, and defensible against the rubric. If not, continue iterating, simplify, or revert.

## Useful Verification

Objective checks that support, but do not replace, aesthetic judgment:

- Browser screenshot at realistic viewports.
- Console error check.
- `document.documentElement.scrollWidth - clientWidth === 0` for responsive screens.
- Contrast checks for primary/muted text and buttons.
- CSS token audit: non-token colors, too many gradients/shadows, warning color misuse.
- DOM/content checks: primary CTA visible, important rows visible, no raw keys, no duplicate CTAs.
- Build/typecheck/lint/unit tests for code health.

## Common Pitfalls

1. **Treating "impressive" as decoration.** Translate it into confidence, clarity, and polish. Ask for references if ambiguous.
2. **Using screenshots as proof of quality.** Screenshots prove output, not good taste. Apply the rubric.
3. **Copying a brand.** Borrow rhythm, spacing, and hierarchy; do not clone copyrighted styling.
4. **Ignoring domain.** A restaurant procurement screen should not look like a crypto landing page or portfolio shot.
5. **Over-styling after a bad baseline.** Sometimes the right move is subtraction: fewer cards, fewer colors, fewer effects.
6. **Letting category identity fight status semantics.** Status colors must remain reserved and legible.
7. **Calling it done while it still looks bad.** Fail closed and say the aesthetic verdict is fail.

## Verification Checklist

- [ ] Design direction and reference basis stated before implementation.
- [ ] Negative taste filter applied before and after changes.
- [ ] Latest screenshot reviewed against the rubric.
- [ ] Semantic color roles preserved.
- [ ] Mobile/tablet/POS checked when relevant.
- [ ] Aesthetic verdict reported honestly.
- [ ] If verdict failed, the result was not presented as done.
