# Studio Design QA

This file is the **single canonical UI QA/rules document for Studio**. All new or changed Studio UI must follow it. Do not create UI_STANDARD.md, DESIGN_SYSTEM.md, another checklist, or a parallel source of truth.

External engineering reference: https://interfaces.dev/cheat-sheet — reviewed 2026-09-19; the upstream page was updated 2026-09-02. It is a source for engineering guidance, not a second project standard. Project-specific requirements, Telegram constraints, accessibility semantics, and exceptions are defined here; if an external recommendation conflicts with this file or a product contract, this file wins.

The goal is not a broad visual redesign. Apply these rules to changed surfaces, preserve public component/API contracts, and turn each discovered gap into a concrete regression or acceptance criterion.

## Project UI rules

### UI-GEO — geometry, radii, spacing, surfaces

- **UI-GEO-1 Concentric nested radii.** When one rounded surface is visibly nested inside another, the outer radius should be visually concentric with the inner radius plus the surrounding padding. Do not force one formula onto unrelated surfaces.
- **UI-GEO-2 Optical alignment.** Align icons, labels, badges, and controls optically, not only by geometric centers.
- **UI-GEO-3 Spacing hierarchy.** Space between groups should be clearly larger than spacing inside a group; use the existing token/spacing rhythm instead of introducing one-off values.
- **UI-GEO-4 Surface consistency.** Borders, shadows, and radii must express the same hierarchy across comparable cards/panels. Do not remove semantic field boundaries or contrast-critical borders just to prefer shadows.
- **UI-GEO-5 Text containers.** Avoid fixed widths/heights that clip localized or dynamic text. Dense work tables may be compact; long-form reading text should stay at a readable measure.

### UI-CTL — buttons, icons, controls, touch targets

- **UI-CTL-1 Native controls.** Use native button/link/form elements for interactive semantics.
- **UI-CTL-2 Consistent button language.** Button labels start with an action verb when text is present; destructive confirmation repeats the consequence. Keep terminology consistent through one flow.
- **UI-CTL-3 Icon consistency.** Icon stroke/weight should match adjacent text and peer icons. Icon + text controls may use optically asymmetric padding on the icon side.
- **UI-CTL-4 Icon-only names.** Every icon-only action has a descriptive accessible name; title alone is not sufficient.
- **UI-CTL-5 Touch area.** Target at least 44×44 CSS px on touch and 40×40 where practical on desktop; never below 24×24. Extended hit areas must not overlap.
- **UI-CTL-6 Busy controls.** A pending mutation disables only the conflicting action scope. Do not globally disable unrelated controls without a safety/authority reason.

### UI-TYPE — typography and Russian copy

- **UI-TYPE-1 Fonts.** Prefer the system stack already used by Studio. If a web font is introduced, ship WOFF2; do not add TTF/OTF web assets.
- **UI-TYPE-2 Dynamic numbers.** Use tabular numerals for counters, timers, quotas, metrics, and dense numeric columns where changing digit width would shift layout.
- **UI-TYPE-3 Wrapping.** Long Russian labels, channel names, URLs, IDs, and technical values must wrap, truncate with a reachable full value, or scroll intentionally; they must not overlap controls.
- **UI-TYPE-4 Long-form measure.** Narrative/help copy should generally stay near 60–75 characters per line. Do not impose that measure on planners, tables, or other dense operational surfaces.
- **UI-TYPE-5 Case and punctuation.** Store copy in natural case and use consistent sentence case. Prefer typographically correct punctuation in user-facing Russian copy.

### UI-COLOR — semantic colors and themes

- **UI-COLOR-1 Semantic tokens.** Components consume colors by role/purpose and Telegram theme variables, not by a screen-specific or appearance-specific token name.
- **UI-COLOR-2 Theme correctness.** Light and dark modes are independently legible; do not assume one palette is the inverse of the other.
- **UI-COLOR-3 Contrast in context.** Check contrast against the actual surface the text/control renders on.
- **UI-COLOR-4 Not color alone.** Success, warning, error, selected, disabled, and pending states need text, iconography, shape, or other semantics in addition to color.

### UI-FORM — forms and validation

- **UI-FORM-1 Labels.** Every input has a visible or programmatically associated label and the correct input type/inputmode when applicable.
- **UI-FORM-2 Validation.** Explain validation failures with text; use aria-invalid and aria-describedby where the error belongs to a field. Focus the first invalid field for submit-time validation when practical.
- **UI-FORM-3 Paste.** Never block paste.
- **UI-FORM-4 Disabled rationale.** Normal validation should not hide the reason behind a disabled submit button. Pending mutations, safety/permission boundaries, duplicate prevention, and unknown-outcome protection are valid reasons to disable when the reason is understandable.

### UI-FOCUS — keyboard and focus

- **UI-FOCUS-1 Keyboard path.** Every action must be reachable and operable with the keyboard in natural tab order.
- **UI-FOCUS-2 Visible focus.** Style :focus-visible. Never remove outline/focus indication without an equivalent visible replacement.
- **UI-FOCUS-3 Async stability.** Loading completion, mutation success, and errors must not unexpectedly discard focus. Moving focus is allowed only when it directly helps recovery, such as the first invalid field.
- **UI-FOCUS-4 Tab order.** Use only natural order plus tabindex 0/-1 where needed; never positive tabindex.

### UI-STATUS — accessible status, success, and errors

- **UI-STATUS-1 Routine updates.** Background/loading progress uses role="status" / aria-live="polite" when an announcement is useful. Avoid duplicate announcements if visible button text already says the same thing.
- **UI-STATUS-2 Errors.** Actionable/urgent errors use role="alert" and remain visible long enough to understand and recover.
- **UI-STATUS-3 Distinct states.** Loading, empty, error, success, disabled, and stale-data situations must be distinguishable in text/semantics, not only by color.

### UI-MOTION — motion and reduced motion

- **UI-MOTION-1 Explicit transitions.** Name the exact properties that change; transition: all is forbidden.
- **UI-MOTION-2 Purposeful speed.** Frequent interactions are instant or very fast. Animation must explain state/change, not decorate waiting.
- **UI-MOTION-3 Trigger origin.** When spatial motion is used, its origin should correspond to the trigger/context.
- **UI-MOTION-4 Reduced motion.** Decorative motion must not be required to understand state. Respect prefers-reduced-motion and keep the existing project fallback effective for new animations/transitions.
- **UI-MOTION-5 Theme changes.** Theme switching must not create distracting transition cascades.

### UI-ASYNC — loading, stale responses, and operation locks

- **UI-ASYNC-1 Loading is not empty.** Unknown/initial data must never render as confirmed empty/free/zero/“—”. Use SkeletonBlock/reserved geometry and AsyncRegion where they fit.
- **UI-ASYNC-2 Refresh vs initial load.** Initial load may replace content with reserved loading geometry. Background refresh should normally preserve already-valid data and show a local InlineStatus.
- **UI-ASYNC-3 Error without valid data.** A failed first load has a distinct error state; it must not fall through to empty.
- **UI-ASYNC-4 Stale-response protection.** Reads tied to channel, week/range, source, selection, or another context need request ownership/generation, AbortController, or the established equivalent. A response may commit state only if its captured context is still current.
- **UI-ASYNC-5 Stale finally protection.** Old request completion must not clear loading/error/busy state owned by a newer request or operation.
- **UI-ASYNC-6 Operation-scoped locks.** Mutations acquire a synchronous lock before the first await for the resource/conflict scope. Repeated clicks must not issue conflicting requests; unrelated resources remain usable.
- **UI-ASYNC-7 Existing primitives.** Reuse AsyncRegion, InlineStatus, SkeletonBlock, ChannelRequestOwnership, ExclusiveOperationLock, and existing reduced-motion handling instead of creating parallel primitives.

### UI-MOBILE — responsive, Telegram safe areas, and long content

- **UI-MOBILE-1 Mobile first failure modes.** Check narrow screens for wrapping, horizontal overflow, sticky/fixed UI, keyboard coverage, and controls that become too small.
- **UI-MOBILE-2 Telegram safe areas.** Fixed/sticky/top/bottom UI must respect Telegram/WebView safe-area insets. Test top and bottom insets on mobile; do not assume browser chrome geometry.
- **UI-MOBILE-3 Long Russian content.** Test long channel names, long action/status copy, long errors, URLs, IDs, and pluralized text on narrow screens.
- **UI-MOBILE-4 Touch behavior.** Hover-only affordances are insufficient on touch; hover styles should not create a persistent selected-looking state after tap.

## Verification categories

Every Studio UI PR must classify its checks into both categories below. Not every rule needs a new test in every PR, but changed behavior needs evidence and any justified exception belongs in the PR description.

### Automatic

- [ ] **Lint/tests/build:** run the configured Studio static checks, Vitest suite, and TypeScript/Vite build. When a dedicated lint command exists, it is mandatory.
- [ ] **Accessibility semantics:** component/regression tests cover native roles, accessible names, status/error semantics, labels, and state distinctions touched by the change.
- [ ] **Forbidden CSS patterns:** automated checks reject new transition: all, focus removal without a replacement, and TTF/OTF web-font assets in Studio-owned UI.
- [ ] **Async state regression:** tests cover initial loading vs loaded-empty/error and preserve already-valid data during refresh where intended.
- [ ] **Stale-response regression:** out-of-order responses cannot overwrite newer channel/week/source/selection state; stale catch/finally cannot reset newer state.
- [ ] **Operation lock regression:** repeated conflicting mutations have one request winner while unrelated resources remain available.
- [ ] **Focus behavior:** changed keyboard flows preserve visible focus and sensible focus ownership through success/error/loading.
- [ ] **Reduced-motion behavior:** changed animations/transitions still communicate state with reduced motion enabled.

Current baseline commands:

    cd studio
    npm run test
    npm run build

### Visual/browser

- [ ] **Optical alignment:** icons, text, badges, and controls look aligned at real rendered sizes.
- [ ] **Radii/shadows consistency:** nested radii are concentric where applicable; comparable surfaces have consistent depth/borders.
- [ ] **Light/dark:** changed states are legible and correctly themed in both modes.
- [ ] **Mobile/tablet/desktop:** verify existing breakpoints plus a narrow mobile viewport.
- [ ] **Long Russian text:** exercise long channel names, statuses, errors, links, and button labels.
- [ ] **Touch targets:** inspect/tap changed controls; no undersized or overlapping hit areas.
- [ ] **Telegram safe areas:** verify top/bottom fixed or sticky UI with safe-area insets.
- [ ] **State matrix:** inspect loading, loaded-empty/free, loaded-data, refresh, success, error, disabled, and slow-network states relevant to the change.

## PR and agent rule

Before editing Studio UI, read this file. PR descriptions for Studio UI changes should state:

1. which rules/states were touched;
2. automatic checks run and their result;
3. visual/browser checks performed;
4. any intentional exception and why it is safer or more appropriate for Studio/Telegram.

AGENTS.md links to this file as the mandatory Studio UI rule set. External references may inspire updates here, but they do not become project requirements until incorporated into this document.
