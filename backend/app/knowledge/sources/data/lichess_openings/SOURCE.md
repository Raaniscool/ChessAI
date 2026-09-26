# Lichess opening names (source data)

Files `a.tsv` … `e.tsv` are an unmodified copy of
https://github.com/lichess-org/chess-openings (columns: `eco`, `name`, `pgn`).

- License: CC0 1.0 (public domain dedication), see the upstream `COPYING.txt`.
- Imported: 2026-09-25, upstream commit recorded in `backend/app/knowledge/sources/lichess_openings.py`.

This is *source data*: the Knowledge Library uses it to prove that an opening
example follows a real, named line (`opening_line` validator) and to name
lines. It is never shown to learners directly.
