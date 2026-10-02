# Design System Master File

> **LOGIC:** When building a specific page, first check `design-system/pages/[page-name].md`.
> If that file exists, its rules **override** this Master file.
> If not, strictly follow the rules below.

---

**Project:** Exavalu Data Cleaning Studio
**Generated:** 2026-10-01 21:55:33
**Category:** Productivity Tool
**Design Dials:** Variance 4/10 (Balanced / Modern) | Motion 3/10 (Subtle) | Density 7/10 (Standard)

---

## Adopted for this project (overrides the generated defaults below)

The generated output below is the baseline the tool produced. Where it conflicts with this section, **this section wins**. The source of truth for values is `frontend/tailwind.config.js` and `frontend/src/index.css`.

### Palette: neutral first, one cobalt accent
Revised after review: the earlier forest-green brand was used for the sidebar, buttons, tints and greys, so "success" green had nothing to contrast with. The interface is now **neutral first**, closer to enterprise systems such as Carbon (see `design-system/reference/carbon-enterprise.DESIGN.md`, from awesome-design-md). The ui-ux-pro-max palettes for an analytics dashboard (deep blue on cool neutral) and for B2B (near-black plus blue) agree.

| Role | Token | Hex |
|------|-------|-----|
| Primary action / current step / focus / selection | `brand-600` | `#2556C7` (cobalt, ~70% saturation) |
| Primary hover / pressed | `brand-700` / `brand-800` | `#1E46A3` / `#1B3A82` |
| Selection tint | `brand-50` | `#EFF4FE` |
| Sidebar (charcoal) | `nav` | `#15181D` |
| Text strong / body / muted | `ink-900` / `ink-800` / `ink-500` | `#14171C` / `#1F242B` / `#67707D` |
| Page background | `ink-50` | `#F6F7F9` |
| Border | `ink-200` | `#E2E5EA` |
| Danger (errors only) | `danger-600` | `#D92D20` |

**Rules:**
- **Colour means state.** `emerald` is success, `amber` warning, `danger` error. Never use them for decoration. Icon tiles, file icons, section icons and completed steps stay neutral.
- **One accent.** Cobalt marks the primary action, the current step, focus and selection, and nothing else. A screen should have about one blue thing that asks to be clicked.
- **Brand red lives only in the Exavalu mark**, on a white tile in the sidebar. It is never used for UI, so it can't be mistaken for an error.
- **No gradients, glows or tinted page backgrounds.** Depth comes from hairline borders (`ink-200`) and one subtle shadow.
- **Shape:** radii of 5–9 px, tighter on inner elements. Badges are square-cornered, not pills.

### Typography: IBM Plex Sans + JetBrains Mono
Roboto (generated) was replaced with pairings from the typography database: **IBM Plex Sans** ("Financial Trust": enterprise, finance) for headings and UI, and **JetBrains Mono** ("Developer Mono") for codes, datatypes and step numbers.

| Token | Size / line | Use |
|-------|-------------|-----|
| `text-page` | 28/36, 600 | Page title (h1) |
| `text-section` | 18/26, 600 | Major panel title |
| `text-card` | 15/22, 600 | Section card / sub-panel title |
| `text-body` | 14/20 | Default UI text |
| `text-table` | 13/20 | Data tables |
| `text-caption` | 12/16 | Meta, hints |
| `.label-caps` | 12, semibold, sentence case | Field/group labels ("Summary", "Pipeline"). Capitals only in the `STEP 0n` eyebrow (mono). |
| `.num` | tabular-nums | Every count, size, percentage |

### Icons
Lucide only (`lucide-react`) at one stroke weight (1.75, set globally in `index.css`), 16–18px in UI and 14px inside badges. Decorative icons get `aria-hidden`, and icon-only buttons get `aria-label`. Never use emoji.

### Copy rules ("less writing")
- Page titles are one word: **Configure / Run / Results**. Each subtitle is one short line, five to seven words.
- Section titles are one or two words: Upload, Sheets, Append, Pipeline, Tables, Export.
- Status badges are one word: Ready, Hidden, Empty, Cleaned, Failed, Appended.
- Buttons are a verb, or a verb plus a noun: Continue, Start cleaning, Run again, Configure, CSV, ZIP.
- Alerts carry the title plus at most one short sentence. Detail goes in tooltips, modals or "Details" toggles.
- Never explain the mechanism inline when a tooltip can do it.

### Layout patterns
- Shell: a charcoal sidebar (workflow steps, a Workspace group with History and Users, the workbook card, and the signed-in user at the foot), a sticky 56px top bar with a breadcrumb, and content capped at 1320px (1560px from 1920px wide, 1880px from 2400px).
- Page header: eyebrow (`STEP 0n`, or `WORKSPACE` beside the workflow), title and one-line subtitle on the left, the 5-step stepper on the right (a step is ticked only when its work is actually done), then a hairline divider.

### Screens and scrolling
Every page must work from a 375px phone to a 2560px monitor, with no sideways page scroll anywhere, and should fit a 1366×768 laptop without vertical scrolling wherever the content allows.
- **Lists are paged, not scrolled.** Use `Pagination` with `useFitPageSize` (`components/ui/Pagination.tsx`): the page size comes from the room left on screen, so a laptop shows fewer rows than a 24-inch monitor. No `max-h` scroll boxes around tables.
- **Many files or tables: a dropdown, not a stack.** Show one at a time behind a `Select` ("File (3)", "Table (5)"), with what needs attention shown in each option's meta.
- **Sections take turns.** Where a page has a few sequential sections (Configure: Upload / Sheets / Append; Ingest: Files / Plan), show one at a time with `SectionNav` (tab mode) or `Segmented`, marking which is done and which needs attention.
- **Short screens** (`max-height: 820px`) tighten the sidebar and page header; the sidebar's middle scrolls on its own so the account footer stays in view; sticky side panels cap their height at the viewport.
- **Horizontal scroll containers are `relative`,** so `sr-only` labels inside them can't widen the page.
- `SectionCard`: an icon tile plus title in a bordered header row, then the body. Secondary meta (limits, counts) sits as a pill on the right.
- `StatTile` rows for key numbers. The label should be one word.
- Sticky right-hand summary panel on the Configure page (xl+).

---

## Global Rules (generated baseline)

### Color Palette

| Role | Hex | CSS Variable |
|------|-----|--------------|
| Primary | `#0D9488` | `--color-primary` |
| On Primary | `#000000` | `--color-on-primary` |
| Secondary | `#14B8A6` | `--color-secondary` |
| On Secondary | `#0F172A` | `--color-on-secondary` |
| Accent/CTA | `#EA580C` | `--color-accent` |
| On Accent/CTA | `#000000` | `--color-on-accent` |
| Background | `#F0FDFA` | `--color-background` |
| Foreground | `#134E4A` | `--color-foreground` |
| Card | `#FFFFFF` | `--color-card` |
| Card Foreground | `#134E4A` | `--color-card-foreground` |
| Muted | `#E8F1F4` | `--color-muted` |
| Muted Foreground | `#475569` | `--color-muted-foreground` |
| Border | `#99F6E4` | `--color-border` |
| Destructive | `#DC2626` | `--color-destructive` |
| On Destructive | `#FFFFFF` | `--color-on-destructive` |
| Ring | `#0D9488` | `--color-ring` |

**Color Notes:** Teal focus + action orange [Accent adjusted from #F97316]

### Typography

- **Heading Font:** Roboto
- **Body Font:** Roboto
- **Mood:** material design 3, md3, android, google, tonal, friendly, rounded, accessible, adaptive
- **Google Fonts:** [Roboto + Roboto](https://fonts.googleapis.com/css2?family=Roboto:ital,wght@0,300;0,400;0,500;0,700;1,400)

**CSS Import:**
```css
@import url('https://fonts.googleapis.com/css2?family=Roboto:ital,wght@0,300;0,400;0,500;0,700;1,400&display=swap');
```

### Spacing Variables

*Density: 7/10 — Standard*

| Token | Value | Usage |
|-------|-------|-------|
| `--space-xs` | `4px` / `0.25rem` | Tight gaps |
| `--space-sm` | `8px` / `0.5rem` | Icon gaps, inline spacing |
| `--space-md` | `16px` / `1rem` | Standard padding |
| `--space-lg` | `24px` / `1.5rem` | Section padding |
| `--space-xl` | `32px` / `2rem` | Large gaps |
| `--space-2xl` | `48px` / `3rem` | Section margins |
| `--space-3xl` | `64px` / `4rem` | Hero padding |

### Shadow Depths

| Level | Value | Usage |
|-------|-------|-------|
| `--shadow-sm` | `0 1px 2px rgba(0,0,0,0.05)` | Subtle lift |
| `--shadow-md` | `0 4px 6px rgba(0,0,0,0.1)` | Cards, buttons |
| `--shadow-lg` | `0 10px 15px rgba(0,0,0,0.1)` | Modals, dropdowns |
| `--shadow-xl` | `0 20px 25px rgba(0,0,0,0.15)` | Hero images, featured cards |

---

## Component Specs

### Buttons

```css
/* Primary Button */
.btn-primary {
  background: #EA580C;
  color: #000000;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
}

.btn-primary:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}

/* Secondary Button */
.btn-secondary {
  background: transparent;
  color: #134E4A;
  border: 2px solid #0D9488;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 600;
  transition: all 200ms ease;
  cursor: pointer;
}
```

### Cards

```css
.card {
  background: #F0FDFA;
  border-radius: 12px;
  padding: 24px;
  box-shadow: var(--shadow-md);
  transition: all 200ms ease;
  cursor: pointer;
}

.card:hover {
  box-shadow: var(--shadow-lg);
  transform: translateY(-2px);
}
```

### Inputs

```css
.input {
  padding: 12px 16px;
  border: 1px solid #E2E8F0;
  border-radius: 8px;
  font-size: 16px;
  transition: border-color 200ms ease;
}

.input:focus {
  border-color: #0D9488;
  outline: none;
  box-shadow: 0 0 0 3px #0D948820;
}
```

### Modals

```css
.modal-overlay {
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
}

.modal {
  background: white;
  border-radius: 16px;
  padding: 32px;
  box-shadow: var(--shadow-xl);
  max-width: 500px;
  width: 90%;
}
```

---

## Style Guidelines

**Style:** Flat Design

**Keywords:** 2D, minimalist, bold colors, no shadows, clean lines, simple shapes, typography-focused, modern, icon-heavy

**Best For:** Web apps, mobile apps, cross-platform, startup MVPs, user-friendly, SaaS, dashboards, corporate

**Key Effects:** No gradients/shadows, simple hover (color/opacity shift), fast loading, clean transitions (150-200ms ease), minimal icons

### Page Pattern

**Pattern Name:** Product Demo + Features

- **Conversion Strategy:** Use an interactive demo only when it explains value better than static media. Provide captions, transcript, visible play/pause controls, and a non-video fallback; do not autoplay under reduced motion. Pause media when offscreen or hidden and keep the final product state available as static content.
- **CTA Placement:** Video center + CTA right/bottom
- **Section Order:** Hero > Product video/mockup (center) > Feature breakdown per section > Comparison (optional) > CTA

---

## Motion

**Scroll Reveal** (Subtle) — Trigger: scroll (viewport enter) | Duration: 300-400ms | Easing: `power1.out`

```js
gsap.from(el, { opacity: 0, y: 12, duration: 0.35, ease: 'power1.out', scrollTrigger: { trigger: el, start: 'top 90%', toggleActions: 'play none none reverse' } });
```

**Framework notes:** Requires the ScrollTrigger plugin registered once via gsap.registerPlugin(ScrollTrigger); Use matchMedia('(prefers-reduced-motion: reduce)') to skip non-essential motion and render the final state immediately

- ✅ Keep the y offset small (8-16px) so it reads as a fade, not a slide
- ❌ Don't reveal below-the-fold content needed for SEO/crawlers as invisible-by-default without a no-JS fallback
- ⚡ toggleActions 'play none none reverse' avoids re-triggering on every scroll direction change

---

## Anti-Patterns (Do NOT Use)

- ❌ Complex onboarding
- ❌ Slow performance

### Additional Forbidden Patterns

- ❌ **Emojis as icons** — Use SVG icons (Heroicons, Lucide, Simple Icons)
- ❌ **Missing cursor:pointer** — All clickable elements must have cursor:pointer
- ❌ **Layout-shifting hovers** — Avoid scale transforms that shift layout
- ❌ **Low contrast text** — Maintain 4.5:1 minimum contrast ratio
- ❌ **Instant state changes** — Always use transitions (150-300ms)
- ❌ **Invisible focus states** — Focus states must be visible for a11y

---

## Pre-Delivery Checklist

Before delivering any UI code, verify:

- [ ] No emojis used as icons (use SVG instead)
- [ ] All icons from consistent icon set (Heroicons/Lucide)
- [ ] `cursor-pointer` on all clickable elements
- [ ] Hover states with smooth transitions (150-300ms)
- [ ] Light mode: text contrast 4.5:1 minimum
- [ ] Focus states visible for keyboard navigation
- [ ] `prefers-reduced-motion` respected
- [ ] Responsive: 375px, 768px, 1024px, 1440px
- [ ] No content hidden behind fixed navbars
- [ ] No horizontal scroll on mobile
