# Local Course Workspace

`stepik_cache_lesson(course_id, lesson_position)` downloads one published lesson
directly to Markdown. The MCP response contains only its path and import metadata,
not the lesson body. All Stepik requests are GETs (OAuth token acquisition is POST).
This tool writes local files, unlike the other Stepik reading tools.

`stepik_cache_lesson_comments(course_id, lesson_position)` separately saves the
default-thread comments and replies beside an existing local lesson. It returns
only the comments-file path and count; it does not change the lesson file and does
not fetch statistics, submissions or the shared-solutions thread.

`stepik_cache_course_text_lessons(course_id)` creates all course/module folders
and saves every lesson, but keeps only `text` theory steps in each lesson file.
It returns a compact summary and paths, never lesson bodies. Existing local lesson
files are preserved.

## Usage

Default root: `C:\Courses`. Optional process environment setting:
`COURSE_WORKSPACE_ROOT` (absolute path). No per-call destination path is accepted.
The implementation requires Python 3.12+ and a filesystem supporting hard links
(the tested Windows NTFS configuration supports them).

```python
stepik_cache_lesson(course_id=266999, lesson_position="5.2")
```

The position uses Stepik section/unit position fields, not a guessed curriculum
number. In the verified course snapshot, pagination (`2498212`) is **5.2**;
**6.2** is StatesGroup/FSMContext (`2498603`). Older chat examples confused them.

Restart OpenCode Desktop after adding/updating the server files so it discovers
the new tool. No change to the existing MCP command or credentials is required.
Keep `stepik_workspace.py` beside `stepik_mcp.py` and `stepik_bridge.py`.

## Files

```text
C:\Courses\course-266999\
    .course.json
    COURSE_MAP.md
    AGENTS.md
    01-<module-title>-<section-id>\
    ...
    05-<module-title>-<section-id>\
        05.02-lesson-2498212.md
        05.02-lesson-2498212.comments.md
```

- All module directories are created; other lesson bodies are not downloaded.
- Course folder names use IDs; module names are Windows-safe and include IDs.
- `.course.json` records the structure snapshot, IDs, paths and import metadata.
- `COURSE_MAP.md` is a generated index of mapped lessons and local file presence.
- `AGENTS.md` tells agents to edit local files and fetch analytics only on request.
  An existing `AGENTS.md` is never overwritten.
- Comments are stored in a separate `.comments.md` snapshot with lesson, step and
  comment IDs, replies, authors and fetch time. They are not mixed into lesson text.
- Each lesson has JSON-quoted YAML frontmatter, ordinal step headings and stable
  Stepik step IDs. It reuses the existing Markdown step renderer with answers
  enabled; it is an editable text representation, not a lossless API backup or
  an export of media files and every author execution setting.

For ongoing editing, open the course folder as an OpenCode project so its
`AGENTS.md` is loaded. When accessing it from another project, OpenCode may ask
for external-directory permission; this implementation does not widen global
permissions automatically.

## Preservation And Recovery

- Existing lesson files are returned without reading their contents or calling
  Stepik. Local changes are never overwritten. Returned title and step count are
  import metadata, not a fresh analysis of the edited document.
- Existing `.comments.md` files are also returned without API requests and are
  never overwritten. There is no force-refresh operation yet.
- Missing mapped files can be downloaded again. No force/refresh option exists.
- Published changes, renamed modules and new course positions are **not** synced
  automatically. Unknown positions fail explicitly instead of guessing.
- New local drafts can be created normally in a module directory. They are not
  automatically assigned Stepik IDs or indexed by this loader.
- Downloads and rendering finish before publishing a lesson file. Temporary
  writes are flushed and atomically published without replacing existing files.
- A failed API call may leave a valid structure/index, but not a partial lesson.
  Retrying finishes initialization and repairs generated indexes.
- The per-course `.stepik-cache.lock` directory prevents concurrent loader writes.
  If a process crashes, the lock may remain. Confirm no loader is running before
  manually removing that lock. Do not delete `.course.json` to clear a lock.
- A corrupt/missing manifest in an otherwise populated course folder is an error.
  Existing content is not silently adopted or overwritten.
- Traversal, Windows drive/ADS paths and escaping symlinks/junctions are rejected.
  Do not concurrently replace filesystem links while a tool call is running.

Publishing remains manual. The lesson loader never fetches comments, statistics,
submissions or reviews. Comment caching fetches only ordinary comments and replies
when explicitly called. A request to download does not require the model to read
the saved lesson; an editing/review request does.

## Verification

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -v
.\.venv\Scripts\python.exe -m py_compile .\stepik_workspace.py .\stepik_mcp.py
```

Tests use isolated directories under `C:\Temp\opencode`, never the actual course
workspace. They cover selective loading, cache hits, edits, partial API responses,
atomic publication failures, recovery, invalid paths and compact MCP responses.
