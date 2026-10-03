---
name: Energy Agent Tools
description: A scoped workbench for connecting energy systems to agents.
colors:
  primary: "#1d754f"
  primary-hover: "#185f41"
  focus: "#67b48b"
  canvas: "#f4f6f2"
  surface: "#ffffff"
  surface-soft: "#f8faf7"
  surface-green: "#edf6ef"
  ink: "#1d2b25"
  ink-soft: "#4a5c52"
  ink-muted: "#596a60"
  line: "#dfe6df"
  line-strong: "#cbd6cc"
  pine-950: "#102d24"
  pine-900: "#173a2f"
  pine-800: "#214a3b"
  pine-active: "#285443"
  nav-text: "#d2e2d6"
  green-soft: "#e7f3e9"
  status-good-ink: "#245d3e"
  amber-800: "#7d5315"
  amber-soft: "#fbf0d8"
  status-neutral-bg: "#eef1ed"
  status-neutral-ink: "#4d5b51"
  status-muted-bg: "#edf0ed"
  status-muted-ink: "#627067"
  red-800: "#8b3e38"
  red-soft: "#fae9e6"
typography:
  body:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  headline:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "clamp(27px, 2.2vw, 34px)"
    fontWeight: 660
    lineHeight: 1.16
    letterSpacing: "-0.035em"
  label:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: 10px
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "0.08em"
  button:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: 11px
    fontWeight: 650
    lineHeight: 1.5
  metadata:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: 10px
    fontWeight: 400
    lineHeight: 1.5
rounded:
  button: 6px
  small: 7px
  medium: 12px
  badge: 5px
  pill: 999px
spacing:
  xsmall: 4px
  small: 8px
  medium: 12px
  large: 16px
  xlarge: 24px
  xxlarge: 32px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    typography: "{typography.button}"
    rounded: "{rounded.button}"
    height: 37px
    padding: "0 12px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.pine-800}"
    typography: "{typography.button}"
    rounded: "{rounded.button}"
    height: 37px
    padding: "0 12px"
  search-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    height: 42px
    padding: "0 11px"
  filter-chip:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
    height: 28px
  navigation-item:
    backgroundColor: "{colors.pine-900}"
    textColor: "{colors.nav-text}"
    rounded: "{rounded.small}"
    height: 39px
    padding: "0 11px"
  toolkit-row:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: 12px
    height: 74px
  toolkit-row-selected:
    backgroundColor: "{colors.surface}"
  status-badge-good:
    backgroundColor: "{colors.green-soft}"
    textColor: "{colors.status-good-ink}"
    rounded: "{rounded.badge}"
    padding: "4px 7px"
  setup-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.medium}"
    padding: "18px 18px 19px"
---

# Design System: Energy Agent Tools

## Overview

**Creative North Star: "The Connector Workbench"**

This is an operational workbench for developers and energy-system owners connecting energy systems to agents. Daylight setup work should feel steady and precise: a pale neutral canvas carries readable system-ui type, a dark pine rail anchors navigation, and energy green marks actions and useful state.

Dense catalogue and gateway metadata stay scannable through ruled rows, small labels, and visible setup details. White surfaces separate related information; compact corners and simple line icons keep the interface technical without making it feel like an enterprise console. Every action and status should reflect what the authenticated gateway supplied.

**Key Characteristics:**
- Daylight surfaces framed by dark pine navigation.
- Compact, factual metadata with quiet dividers and clear status labels.
- Selected setup details stay connected to the catalogue choice at every screen size.
- Registry and connection states remain grounded in gateway data.

**Capture note:** The supplied review capture omits the initial QUALITY BAR card. This document records the implemented workbench only; no approved comps or generated raster artwork accompany the reviewed design.

## Colors

The palette pairs pine navigation with green actions, cool green-tinted neutrals, and labeled caution and error states.

### Primary
- **Gateway Green** (`{colors.primary}`): Primary actions, setup links, and positive interaction cues.
- **Pine Navigation** (`{colors.pine-900}`): The persistent workspace rail; deeper pine supports emphasis and technical values.

### Secondary
- **Soft Green** (`{colors.green-soft}`): Selected filters and positive status backgrounds, paired with dark readable text.

### Tertiary
- **Caution Amber** (`{colors.amber-800}`): Credential requirements and experimental states, always accompanied by a text label.
- **Error Clay** (`{colors.red-800}`): Request and authentication errors, paired with explicit recovery text.

### Neutral
- **Daylight Canvas** (`{colors.canvas}`) and **Paper Surface** (`{colors.surface}`): The page field and grouped content surfaces.
- **Soft Surface** (`{colors.surface-soft}`) and **Green Surface** (`{colors.surface-green}`): Quiet tag and interaction backgrounds.
- **Ink** (`{colors.ink}`), **Soft Ink** (`{colors.ink-soft}`), and **Muted Ink** (`{colors.ink-muted}`): Primary copy, supporting copy, and labels.
- **Quiet Line** (`{colors.line}`) and **Strong Line** (`{colors.line-strong}`): Row dividers and control outlines.

The `ink-muted` token measures 5.28:1 against the canvas and 5.75:1 against white. The focus outline uses the dedicated `focus` token.

**The Pine Frame Rule.** Keep pine on workspace identity and navigation; use green for actions and positive state. Pair caution and error colors with visible labels.

## Typography

**Display Font:** UI system stack; there is no separate display face.  
**Body Font:** UI system stack (`ui-sans-serif`, system UI, and platform fallbacks).  
**Label/Mono Font:** System UI for labels; system monospace for IDs and configuration.

**Character:** Native system type keeps the workbench compact and familiar. Monospace is reserved for machine identifiers and copyable configuration.

### Hierarchy
- **Page title** (weight 660, responsive 27–34px, line-height 1.16): The active workspace view.
- **Section title** (weight 680, 14–15px): Catalogue sections and record groups.
- **Body** (weight 400, 14px base, line-height 1.5): Explanations; workbench support copy commonly uses 10–13px.
- **Label** (weight 650–700, 9–10px, tracked uppercase): Metadata keys, navigation groups, and status context.
- **Machine data** (system monospace, 9–10px): Toolkit IDs, record references, and agent configuration.

**The Metadata Legibility Rule.** Keep names and explanations in system UI; reserve monospace for identifiers and configuration.

## Layout

Desktop uses a 252px navigation rail beside a fluid workspace capped at 1510px. The Connect Apps workbench places a flexible catalogue beside a 286–322px setup panel, with a 25px gap. Page gutters scale from 22px to 48px; the visible spacing rhythm is roughly 4px based, with observed steps from 4px to 32px.

At 1000px and below, setup details appear directly beneath the selected catalogue row. At 700px and below, the navigation becomes a horizontally scrollable rail, the site summary and page heading stack, and catalogue rows simplify to two columns. At 390px and below, page gutters reduce to 14px. Only navigation and filter rails scroll horizontally; content identifiers wrap so the page itself stays within the viewport.

Search matches names, IDs, descriptions, runtimes, and categories. Category filters update the visible count and selected detail. If filtering removes the selected toolkit, the detail panel returns to its empty prompt instead of showing stale content.

## Elevation & Depth

The workbench is mostly flat. Canvas and white surfaces, thin dividers, and a restrained inset selection ring provide hierarchy. Search focus adds a soft ring; the sign-in and gateway status panels use the only broad ambient shadow. Respect reduced-motion preferences and keep state transitions short.

**The Border Before Shadow Rule.** Use borders and surface contrast for catalogue structure; reserve ambient shadow for the centered sign-in and status panels.

## Shapes

Controls use gently softened corners: buttons use a 6px radius, small controls and navigation use 7px, and setup surfaces use 12px. Filter chips are fully pill-shaped (999px). Catalogue rows remain mostly square and are separated by one-pixel rules rather than card outlines.

## Components

### Buttons
- **Shape:** Compact, gently rounded (6px), with a 37px minimum height and 12px horizontal padding.
- **Primary:** Gateway Green with white text; hover shifts to deeper green.
- **Hover / Focus:** Short color transition; keyboard focus uses a 3px green outline with 3px offset.
- **Secondary:** White surface, strong neutral border, and pine text; hover adds a soft green surface.

### Chips
- **Style:** White pill with a quiet border, soft ink, 4px by 10px padding, and a 28px minimum height.
- **State:** Selected filters use a pale green fill, stronger green border, and pine text. The count always reflects the filtered catalogue.

### Cards / Containers
- **Corner Style:** Setup detail uses a 12px radius.
- **Background:** White over the daylight canvas.
- **Shadow Strategy:** Flat with a fine border; see Elevation & Depth.
- **Internal Padding:** 18px horizontally and 18–19px vertically.

### Inputs / Fields
- **Style:** White search field with a 1px strong neutral border and 7px radius; 42px minimum height.
- **Focus:** Border shifts to muted green and a soft 3px focus halo appears around the field.
- **Error / Disabled:** Keep error copy explicit and adjacent to the affected action; pending gateway mutations use concise status text.

### Navigation
- **Style:** Persistent pine rail with grouped uppercase labels and compact icon-and-text items. The active item uses a lighter pine surface; on narrow screens the rail becomes a horizontal strip.

### Toolkit Rows and Setup Detail
Rows show toolkit name and description, runtime, registry status, and a right-aligned “View setup” action. Selection adds a white surface and a subtle inset border. The selected setup detail shows registry metadata and any safe provider guide; when provider onboarding is unavailable, state that limitation plainly. On mobile, place the detail immediately after its selected row.

### Status Badges
Use compact tinted badges for stable, experimental, credential-required, and unavailable states. Preserve the status label in text; color supplements it.

## Do's and Don'ts

### Do:
- **Do** keep gateway-supplied registry metadata visually distinct from connected-account records.
- **Do** label synthetic fixture sites and sample values as synthetic; they are not evidence of a physical installation.
- **Do** keep “View setup” tied to a safe HTTPS guide or a clear provider-onboarding limitation.
- **Do** keep muted text on the canvas or white surface using the `ink-muted` token.
- **Do** keep selected setup details inline on mobile and synchronized with active filters.

### Don't:
- **Don't** imply that registry metadata means a provider account is connected; provider credentials and account onboarding are not available here yet.
- **Don't** present synthetic site or energy fixtures as physical meter records or verified site data.
- **Don't** use color alone to communicate status, error, selection, or authentication state.
- **Don't** add decorative raster artwork or a component pattern inferred from the missing QUALITY BAR capture card.

The Octopus connection form uses the existing form, button and notice styles.
It appears before toolkit metadata in both the desktop detail column and mobile
inline detail panel. Gateway metadata supplies labels and input constraints.
Failed submission clears the key and keeps a fixed visible error; success loads
the scoped Connections view. Fictional fixture captures qualify this interaction
only, not private provider access or the unfinished product views.
