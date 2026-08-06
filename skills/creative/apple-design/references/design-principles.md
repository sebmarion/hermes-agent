# Design Principles for Interaction Review

## Human Needs

Design is not decoration; it is a response to human need. Before reviewing
motion, materials, or interaction, ask whether the design satisfies the four
human needs:

- **safety / predictability** — Does the user feel safe and able to predict what will happen next? Does the design fail in a way the user understands, every time they interact with it?
- **understanding** — Does the user understand what this interaction is for and how to use it? Does it tell them where to go next?
- **achievement** — Does the user feel that their actions produce visible, immediate consequences? Does the design reward competent interaction?
- **joy** — Does the interaction delight the user? Is it a pleasure to use, or merely functional?

## Purpose

What purpose does this interaction serve in the user's current task? What evidence supports that this interaction is necessary for that purpose? How does the user know this interaction is serving them and not the system?

## Agency

Can the user undo, cancel, and reverse this interaction if they change their mind? What feedback confirms the user's control over the interaction at every stage? How does the user recover if the interaction behaves unexpectedly?

## Responsibility

Does every interaction respect the user's time, attention, and ability? What risk does this interaction impose on the user, and how does the design minimize that risk? How does the user know their data and actions are protected?

## Familiarity

Which existing patterns from the product and platform does this interaction use? How does this interaction align with the mental models users already have? What would a user familiar with similar interactions expect this to do?

## Flexibility

Does this interaction work across all relevant input methods and abilities? How does the interaction behave when the user uses assistive technologies, reduced motion, or alternative input devices? What fallback does the design provide if the primary interaction method is unavailable?

## Simplicity

Is this interaction simple enough that the user can understand it on first encounter? What new pattern, if any, does this interaction require the user to learn? How does the design reduce cognitive load while preserving the necessary complexity?

## Craft

Does this interaction demonstrate intentional craft in its motion, timing, and responsiveness? What details reveal that this interaction was designed thoughtfully rather than implemented hastily? How does the craft communicate quality without calling attention to itself?

## Delight

What does this interaction do to delight the user beyond the functional requirements? How does the interaction earn its emotional impact through competence rather than decoration? What subtle moments of pleasure does the design create for engaged users?

## Tactical Review Questions

When reviewing an interaction, ask these questions:

- **Status** feedback — What is happening now?
- **Completion** feedback — Did the action succeed?
- **Warning** feedback — Is something about to go wrong?
- **Error** feedback — Did something go wrong, and what can the user do?

Does the feedback match the user's expectation?

- **Wayfinding** — Does the interaction tell the user where they are, where
  they can go, and how to get there?
- **Grouping and Mapping** — Are related interactions grouped together? Are
  related inputs mapped to related outputs?
- **Direct Labels** — Does the interaction use direct labels that tell the user
  what the interaction does, rather than abstract icons or jargon?
- **Interactive Prototypes** — Have you prototyped the interaction in an
  interactive form, not just a static mock?
- **Real Context** — Have you tested the interaction in the real context where
  the user will use it, not just in a vacuum?
- **Frame-by-Frame Motion** — Have you inspected the motion frame-by-frame,
  not just the start and end states?

## Prototype and Test in Real Context

Prototype the interaction in the project's existing stack, in the project's
existing context, with the project's existing constraints. Test it with real
users, in real conditions, with real devices. Make assumptions explicit during
review, and require a real-context prototype before shipping when feasible and
proportionate.

## Review Scorecard

When reviewing an interaction, score it against these criteria:

- **User Control** — Does the user feel in control? Can they undo, cancel, and
  reverse the interaction?
- **Existing Language** — Does the interaction match the project's existing
  design language, tokens, and components?
- **Inputs and Abilities** — Does the interaction work across all relevant
  input methods and abilities, including reduced motion, reduced
  transparency, and increased contrast?
- **Motion and Material Justification** — Does every motion and material
  choice earn its place? Is it justified by Purpose, not decoration?

Score each criterion 0–3:

- **0** — Fails the criterion entirely.
- **1** — Partially satisfies the criterion; needs work.
- **2** — Satisfies the criterion; minor improvements possible.
- **3** — Exceeds the criterion; exemplary.

An interaction that scores below 2 on any criterion is not ready to ship.
