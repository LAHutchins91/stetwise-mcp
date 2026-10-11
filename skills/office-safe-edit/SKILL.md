---
name: office-safe-edit
description: Make safe, reversible edits to Excel and Word files through Stetwise by Ouroboros. Use when the user wants to inspect, preview, apply, undo, or export an .xlsx or .docx change. Propose before apply. Do not use for PowerPoint, Google Drive, OneDrive, or bulk multi-file edits.
---

# Stetwise by Ouroboros

Use the connected Stetwise tools and the user's own account. Never ask for a pasted token. If a call fails, report the reason. Do not claim the file changed until `apply_edit` succeeds.

## Get a file in

ChatGPT can pass an attachment to `import_file`. If that reference has no download URL, or the client is Claude, Cursor, or Grok, call `start_upload` and have the file PUT to the returned URL:

```bash
curl -T ./file.xlsx "PASTE_THE_UPLOAD_URL"
```

Use the `file_id` from that response. Do not paste the workbook into the chat when the upload link works.

## Edit

1. `inspect`, then `read` or `search`.
2. `propose_edit` with one file and the smallest operation that does the job. Show the diff and the warnings. One proposal can cover at most 5000 cells. `clear_range` and `fill_range` edit a rectangle. `format_cells` can set font size, font colour, and a thin border. `insert_paragraph` can use a paragraph style that is already in the document, and will not create a style.
3. `apply_edit` with that `proposal_id` and the `base_version` token. A stale token means the file moved. Propose again. Do not reuse an old token.
4. `export` returns a short-lived download link. `restore_version` undoes by creating a new head equal to an earlier version.

`recalculate` runs LibreOffice on a copy and returns a preview of cached values. Dates and currency stay numbers. Apply that proposal to keep them. Do not invent cached values. A run counts when LibreOffice starts. `create_file` makes a blank workbook or document; fill it with `propose_edit`.

## Caps and retention

`get_usage` shows the monthly counters for edits, files, recalculations, and proposals. Each started 5 MB of a file counts as one unit. Past a cap, stop. Stetwise does not bill overage. Files expire 24 hours after last use unless the user sets 168 hours with `set_retention`.

## Refusals

If Stetwise refuses, the reason is the result. Do not retry a refused pivot, chart, protected sheet, table header, or overlapping tracked change by rewriting the file another way. Word edits on a document that already tracks changes are written as revisions by Stetwise. Pass `track_changes: true` to record revisions on a document that is not tracking yet.
