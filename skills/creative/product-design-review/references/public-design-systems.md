# Public Design-System Reference Notes

Use these as constraint sources, not as brands to clone.

## Apple Human Interface Guidelines

Borrow:
- clarity over ornament;
- typography and spacing as hierarchy;
- platform conventions;
- restraint in chrome and decoration.

Do not borrow:
- Apple-specific product identity unless the user explicitly asks for native Apple-style UI.

## Material Design 3

Borrow:
- explicit semantic roles;
- responsive layout guidance;
- component state discipline;
- accessible contrast and touch targets.

Do not borrow:
- generic Material look when the product has its own stronger system.

## IBM Carbon

Borrow:
- enterprise/productivity density;
- table, dashboard, form, and data hierarchy;
- neutral surfaces with restrained blue action semantics.

Good for:
- operations dashboards;
- procurement/admin tools;
- data-heavy business workflows.

## Shopify Polaris

Borrow:
- commerce/admin workflow hierarchy;
- clear primary actions;
- resource lists, filters, banners, sheets, and contextual help.

Good for:
- carts, orders, catalog/admin screens, merchant-style operational flows.

## Atlassian Design

Borrow:
- task/project workflow patterns;
- compact navigation and issue surfaces;
- readable hierarchy across dense work queues.

## GOV.UK Design System

Borrow:
- ruthlessly clear copy;
- accessibility and form validation patterns;
- low-decoration task completion discipline.

## Microsoft Fluent / Primer

Borrow:
- mature product chrome;
- command bars, tabs, lists, and developer-product affordances;
- controlled elevation and token systems.

## Bundled style templates

`popular-web-designs` is useful for inspiration, but templates must be filtered through product-design-review:

- Borrow layout rhythm, typography, spacing, and palette structure.
- Do not clone brand assets, exact proprietary layouts, or copyrighted expression.
- Do not choose a high-drama template for operational workflows unless the product demands it.

## Choosing references quickly

| Product surface | Better references | Avoid |
|---|---|---|
| POS / restaurant ops | Shopify Polaris, Carbon, GOV.UK forms, existing app tokens | crypto/AI landing pages, heavy glow, card bloat |
| Developer dashboard | Linear, Vercel, Primer, Atlassian | over-branded consumer marketing |
| Checkout/cart/order review | Polaris, Stripe checkout, GOV.UK task flow | decorative gradients around dense prices |
| Analytics dashboard | Carbon, Linear, Atlassian | chart junk, neon data walls |
| Marketing landing page | Stripe, Vercel, Framer, Webflow | blindly mixing several brands |
