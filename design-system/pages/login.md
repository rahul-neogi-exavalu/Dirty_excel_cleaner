# Login page (overrides MASTER where it differs)

**Concept: "Ledger".** The sign-in card is a selected range on a spreadsheet. The page is a
light sheet with column letters and row numbers; the card snaps to whole cells, the
headers it covers are highlighted as a selection, the Name Box shows the range
(`E3:H18`), and a fill handle sits at its bottom-right corner. Focusing a field moves the
active cell to that field's row, which is the only motion on the page.

## Rules
- **Palette:** MASTER's. Cobalt marks the sign-in button, the active cell and the fill
  handle (all selection or primary action). The sheet is `#FAFBFC` with hairline cells.
- **Debris:** faint, fixed values of the kind a messy export leaves (`#REF!`, `TOTAL`,
  `Unnamed: 3`) are scattered outside the selection in `ink-300` mono. They sit at least one
  clean cell away from the card, are `aria-hidden`, and never move.
- **Card:** a ring rather than a border, so its size stays exactly whole cells. Label
  above each input, error below, never a placeholder as label. One alert line at most.
- **Copy:** "Sign in" / "Use the account your admin created." Errors are one sentence:
  "Email or password is incorrect.", "Account inactive. Ask an admin.", "Too many
  attempts. Try again in N min." No "forgot password": there is no email service.
- **Theme:** light only, like the app (MASTER has no dark mode).
- **Responsive:** below 640px the card spans the screen with 16px gutters instead of
  snapping to columns. Short screens scroll the page; the sheet itself uses
  `overflow: clip` so focusing a field can't shift it.
- **Motion:** card scales in; the active-cell highlight eases between rows; all of it
  respects `prefers-reduced-motion`.

## Files
`frontend/src/pages/LoginPage.tsx`, `frontend/src/components/auth/SheetBackdrop.tsx`.
