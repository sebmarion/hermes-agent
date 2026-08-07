# Visual Smell Checklist

Run this before saying a UI looks polished.

## Immediate fail signals

- The screenshot looks busier than the baseline without improving task clarity.
- The first viewport is dominated by decoration instead of task/content/action.
- Dense data is placed over gradients, patterns, or low-contrast surfaces.
- More than one accent color competes for primary attention.
- Warning colors are used for normal identity/category styling.
- Primary and secondary actions are visually indistinguishable.
- Cards are so large that they reduce throughput.
- Typography looks cartoonish, shouty, or inconsistent.
- The design resembles a random template more than the product.

## Screenshot questions

Ask:

1. What is the user's next action?
2. What is the most important data?
3. What can be ignored?
4. Are status/warning/action colors semantically clean?
5. Is the density right for this device and workflow?
6. Would removing decoration make it better?
7. Does it look like one system designed it?
8. Would a competent product designer ship this?

If the answer to 8 is no, the verdict is fail.

## CSS/code smells

- Many raw hex colors instead of tokens.
- Multiple unrelated gradients.
- Stacked box-shadows and text-shadows.
- Large blur/backdrop-filter effects on functional UI.
- Hardcoded warning/yellow/orange around non-warning content.
- Overlapping custom breakpoints with no design reason.
- Repeated one-off component overrides instead of reusable tokens.
