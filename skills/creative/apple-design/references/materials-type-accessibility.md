# Materials, Type, and Accessibility

## Existing Product Language Comes First

The project's existing brand, tokens, components, and platform conventions
outrank this guide. When the two disagree, keep the existing product language
and revisit this guide.

## Optional Materials and Depth

Depth is a signal, not a mandate. Use it to indicate focus, hierarchy, and
separation between parallel flows. Do not add depth to a component that does
not already have it.

A solid, readable surface is the starting point. Translucent materials —
frosted glass, blur, sheen — are optional and must be justified by improved
hierarchy or separation. `backdrop-filter` is appropriate when it improves
visual hierarchy and is supported and performs on the project's target
devices. Do not use `backdrop-filter` as a substitute for a solid surface.

Scrim materials are for modal focus: when the user is completing a task that
requires their full attention. Translucent separation is for parallel flow:
when the user is multitasking and needs to see the underlying content. Surface
stacking is for sequential flow: when the user is moving through a sequence
of steps. Legibility over changing backgrounds is the goal; materials are the
means.

## Progressive Enhancement and Performance

Materials must not degrade performance on the project's target devices. If a
material causes a measurable performance regression on a target device, do not
use it on that device. If a material causes a layout shift on a target device,
do not use it on that device. If a material causes a frame drop on a target
device, do not use it on that device.

## Multimodal Feedback

Sound, haptics, and vibration are optional, synchronized with visual feedback,
causal to the user's action, useful to the user's task, and nonessential to
the user's ability to complete the task. Do not add sound, haptics, or
vibration to a component that does not already have it.

Visual feedback is always present. Multimodal feedback is always optional. The
user must be able to complete the task without sound, haptics, or vibration.
Require platform support and feature detection before calling the relevant
APIs. When an API is unsupported, unavailable, or denied, omit that feedback
and preserve the visual path without breaking the interaction.

## Reduced Motion

`prefers-reduced-motion` is an independent accessibility adaptation. When the
user has requested reduced motion, replace motion with short cross-fades or
static transitions. State feedback must still be visible.

## Reduced Transparency

`prefers-reduced-transparency` is an independent accessibility adaptation. When
the user has requested reduced transparency, replace translucent materials with
opaque solid surfaces and remove `backdrop-filter` (which disables blur)
entirely. Treat opaque solid surfaces as the safe default until support and the
user's preference are known. When the media query is unsupported or
unavailable, provide an application-level reduced-transparency control that
applies the same solid-surface behavior.

## Increased Contrast

`prefers-contrast` is an independent accessibility adaptation. When the user
has requested increased contrast, ensure that all material boundaries are
visible at the project's required contrast ratio.

## Typography

`font-optical-sizing` must be used when the project's existing stack supports
it. Size-aware tracking and leading must be used when the project's existing
stack supports it. Scalable `rem`/`em` layout must be used for responsive
typography. Platform-aware fonts must be used for body text.

Existing brand typography outranks the system-font heuristic. When the
project's brand specifies a typeface, use it. When the project's brand does
not specify a typeface, use the system font.

## Input and Activation Semantics

The element's semantic activation is always reachable via click/touch-up,
keyboard, and assistive technology. Cancellation prevents activation or
restores state.

Visual pointer-down feedback (scale, color, shadow) is always optional and
never changes the semantic activation, keyboard, cancellation, or
assistive-technology behavior of the element.

## Verification Matrix

| Concern | Check |
|---|---|
| Material performance | No measurable regression on target devices |
| Material layout | No layout shift on target devices |
| Material frames | No frame drop on target devices |
| Reduced motion | State feedback preserved with static transitions |
| Reduced transparency | Solid surfaces replace translucent materials |
| Increased contrast | All material boundaries visible at required contrast ratio |
| Typography | `font-optical-sizing`, size-aware tracking, scalable layout |
| Input semantics | Visual feedback does not alter semantic activation |
