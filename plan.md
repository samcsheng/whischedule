# Plan: Refresh Changes Popup

## Overview
After refresh detects changes, show a bottom-sheet popup detailing what changed. Reuses lesson-row styles for each change entry. Old rows get a strikethrough/delete treatment, an arrow points down to the new row. Grouped by date, scrollable.

## Current State
- `diffLessons(old, new)` returns a summary string like `"+3 new, -2 removed, 1 updated"` but discards the actual change details
- The summary is shown briefly in the sync status bar, then fades away
- Existing bottom-sheet modal pattern: `.modal-overlay` + `.modal-sheet` (used by profile, install modals) and `.prog-modal-overlay` + `.prog-modal-sheet` (used by program/client modals)

## Changes

### 1. Modify `diffLessons()` to return structured change data
**File:** `public/index.html` (~line 3592)

Instead of returning just a summary string, return an object:
```js
{
  summary: "+3 new, -2 removed, 1 updated",
  added: [ { lesson, dateKey } ... ],
  removed: [ { lesson, dateKey } ... ],
  changed: [ { oldLesson, newLesson, dateKey } ... ]
}
```
Each lesson object is the raw lesson from the scrape response (has `date`, `activity`, `assignment`, `client`, etc.). We'll derive display card data from these when rendering.

Return `null` if no changes (same as now).

### 2. Add "Changes" modal HTML
**File:** `public/index.html` (after the other modals, ~line 1797)

Uses the `prog-modal-overlay` / `prog-modal-sheet` pattern (full-height scrollable sheet):
```html
<div class="prog-modal-overlay" id="changesModal" onclick="handleChangesModalOverlay(event)">
  <div class="prog-modal-sheet" id="changesModalSheet">
    <div style="position:relative"><div class="prog-modal-handle"></div></div>
    <div class="prog-modal-header">
      <div>
        <div class="prog-modal-title">Schedule Updated</div>
        <div class="changes-modal-sub" id="changesModalSub">+3 new, -2 removed</div>
      </div>
      <button class="prog-modal-close" onclick="closeChangesModal()">
        <svg viewBox="0 0 24 24">...</svg>
      </button>
    </div>
    <div class="prog-modal-divider"></div>
    <div class="changes-modal-body" id="changesModalBody"></div>
  </div>
</div>
```

### 3. Add CSS for changes modal
**File:** `public/index.html` (CSS section, after prog-modal styles ~line 1504)

New styles needed:
- `.changes-modal-sub` — muted subheader showing the summary string
- `.changes-modal-body` — scrollable container, overflow-y auto
- `.changes-date-header` — date section header (reuse day-header styling: small, uppercase, muted)
- `.change-group` — wraps one change entry (old row + arrow + new row, or just added/removed)
- `.change-row-removed` — lesson-row with red-tinted background, strikethrough on title, reduced opacity
- `.change-row-added` — lesson-row with green-tinted background/left border accent
- `.change-arrow` — centered downward arrow between old and new rows (for "updated" changes)
- `.change-badge` — small label like "NEW", "REMOVED", "UPDATED" next to each group

Visual treatment:
- **Removed rows:** Red-ish tint background (`rgba(239,68,68,0.08)`), title has `text-decoration: line-through`, slightly faded
- **Added/new rows:** Green-ish tint background (`rgba(34,197,94,0.08)`), subtle green left border
- **Updated entries:** Old row (red/strikethrough) → down arrow `↓` → new row (green), stacked vertically
- **Arrow:** Centered `↓` icon between old and new row, styled in muted color, small font

### 4. Add JS functions for the modal
**File:** `public/index.html` (JS section, near other modal functions)

```js
function openChangesModal(diffResult) { ... }
function closeChangesModal() { ... }
function handleChangesModalOverlay(event) { ... }
function buildChangesHtml(diffResult) { ... }
```

**`buildChangesHtml(diffResult)`:**
1. Collect all changes into a flat list with `dateKey` attached
2. Group by `dateKey`, sort date groups chronologically
3. For each date group, render a `.changes-date-header` (format: "Saturday, March 8")
4. For each change in the group:
   - **Added:** Render a `.change-group` with a "NEW" badge and a single lesson row using `.change-row-added` styling. Convert the raw lesson to a card object using the same logic as `buildDayView` (call `classifyRow`, `cleanTitle`, etc.) then render with a variant of `buildRowsHtml` (or inline the row HTML).
   - **Removed:** Same but with "REMOVED" badge and `.change-row-removed` styling (strikethrough)
   - **Updated:** "UPDATED" badge, old row (strikethrough/red), arrow `↓`, new row (green)

**`openChangesModal(diffResult)`:**
1. Set `#changesModalSub` text to `diffResult.summary`
2. Set `#changesModalBody` innerHTML to `buildChangesHtml(diffResult)`
3. Add `.open` class to `#changesModal`

**Helper — `buildChangeRowHtml(lesson, extraClass)`:**
Renders a single lesson row (reuses the same structure as `buildRowsHtml` for one card), but adds the `extraClass` (e.g. `change-row-removed` or `change-row-added`). This keeps the visual appearance consistent with the main schedule view.

### 5. Wire up: show popup after refresh when changes detected
**File:** `public/index.html` (~line 3667-3682 in `triggerRefresh`)

Current code:
```js
const summary = diffLessons(oldLessons, data.lessons);
...
if (summary) {
  setSyncStatus('current', `Updated — ${summary}`);
  scheduleSyncReturn(4000);
}
```

Updated:
```js
const diff = diffLessons(oldLessons, data.lessons);
...
if (diff) {
  setSyncStatus('current', `Updated — ${diff.summary}`);
  scheduleSyncReturn(4000);
  openChangesModal(diff);
}
```

Also: make the sync status bar clickable — if there's a stored diff result, tapping it re-opens the popup (in case user dismisses it and wants to see again). Store `_lastDiff` globally, clear it on next refresh start.

### 6. Clickable sync status bar (optional enhancement)
When `_lastDiff` exists and sync status shows "Updated — ...", tapping the status text re-opens the changes modal. Add a click handler to `.app-header-sync-bar` that calls `openChangesModal(_lastDiff)` if available.

## Summary of touched areas
1. `diffLessons()` — return object instead of string
2. `triggerRefresh()` — use new diff object, open modal
3. New HTML — changes modal markup
4. New CSS — change row styles (removed/added/arrow/badges)
5. New JS — `openChangesModal`, `closeChangesModal`, `buildChangesHtml`, `buildChangeRowHtml`
6. Sync bar — clickable to re-open changes

## Edge cases
- If only non-paid lessons changed, diff returns null (no popup) — same as current behavior
- Very large number of changes (e.g. start of season load) — scrollable body handles this naturally
- Modal dismissed by tapping overlay or close button — standard pattern already exists
