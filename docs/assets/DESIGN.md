# Design note: the blotter and the console

`theme: custom (by request)` · idea: one engine quoted at both ends of a ticket · vibe: a desk blotter beside an engine console

## The subject's world

A front-office trading and risk desk. Instruments and tickets, a blotter of ruled rows, a term sheet, a price ladder with its ticks, rates quoted to the pip, a risk ladder of exposure by tenor. The customer product (hedge portal, pre-deal decision API) lives on the blotter side: documents, rates, verdicts. The developer product (C++ core, Kubernetes, Redis Streams, Cassandra, KEDA, traces, chaos) lives on the console side: dark instruments, logs, scrubbers. The site is one ticket cut in two, and the cut is the navigation.

## The one signature: the pip numeral on a split ticket

- **Pip numeral.** Every rate and headline figure is set the way an FX screen quotes it: the figure in a quiet weight, the last two digits (the pips) large and heavy on an amber marker, an optional raised fraction. `1.34` then `59`. Used for the forward rate, the proof numbers and tile figures. Only a desk would set numbers like this.
- **Split ticket.** The home page is a ticket divided at a spine: the open, ruled blotter half (customer) against a dark console half (developer). The same split recurs as the page language: customer content sits open on the page with hairline rules; developer instruments sit in dark console panels in both colour schemes. The two halves meet at the label "one engine".
- **Tick rule.** Section dividers are a price-ladder rule: a hairline with ticks, the major tick at the left.

## Palette (OKLCH, light then dark)

| Token | Light | Dark | Role |
| --- | --- | --- | --- |
| tape | oklch(96% 0.008 250) #eef2f7 | oklch(15.5% 0.022 258) #070c16 | the page |
| ink | oklch(19% 0.035 262) #0b1423 | oklch(94% 0.01 250) #e6ecf2 | text, rules at weight |
| signal | oklch(79% 0.165 78) #f4ab00 | oklch(80% 0.16 80) #f3b01d | the one action, the pip marker; signal-ink oklch(50% 0.14 55) is its text and chart form on light |
| engine | oklch(20% 0.04 262) #0c1628 | oklch(11% 0.025 260) #02040d | developer console panels |
| steel | oklch(47% 0.12 245) #005f99 | oklch(74% 0.1 245) #73b1e6 | the second data series, information |
| ok and fault | oklch(44% 0.12 160) / oklch(50% 0.19 27) | oklch(76% 0.14 160) / oklch(72% 0.17 27) | pass and fail, never alone (always a word) |

Posture: Committed. The dark console is the large field; amber stays under about 5% of any viewport. Dark mode is derived: the tape deepens, the console drops below it as an inset screen, and signal, steel and the status pair are re-mixed for the ground, not inverted. Scoped overrides under `.console` re-derive every token for the dark panel in both schemes.

## Type (Google Fonts)

- **Archivo** (variable, width and weight axes): the display and the body. The display is set wide (font-stretch 118%, weight 800) and only in h1, the wordmark and the pip figures; section heads are normal width. Fallback `system-ui` sans.
- **Sometype Mono**: figures that compare, code, the console. Fallback `ui-monospace`.
- Weights across the page: 400, 600, 800. Labels are the body face, a step smaller, sentence case, untracked. Scale (1.25 from 16px): 14 · 16 · 20 · 25 · 31 · 39 · 49 · 61 · display clamp up to 112.

## Space, surface, radius

- Spacing scale 2 · 4 · 8 · 12 · 16 · 24 · 40 · 64 · 96 · 144 as `--space-*`.
- Surface ladder: air, a hairline, the tick rule, a tinted well, then the console (a filled panel, the only heavy surface). No white cards on the page, no card in a card.
- Radius: 0 for panels and the console (they are screens and sheets), 3px for controls. Nothing is pill-shaped.

## Motion

One orchestrated moment: on load the two halves of the home ticket close from 14px apart to the spine while the pips tick down to their value (about 600ms, transform and opacity, text count in script). Everything else is state: press, selection, scrubber. Reduced motion shows the end state at once and removes the ticks.
