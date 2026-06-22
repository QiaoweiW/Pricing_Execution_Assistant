"""
XXICS Pricebook editor library.

All data access is via ORDS REST (ords_client) — the Oracle DB sits behind ORDS
and no DB DSN is available to clients:
  READ  -> GET {base}/{resource}/?q=...   (filtered, paged)
  WRITE -> POST / PUT / DELETE

The Streamlit surface lives in ``pages/pricebook_editor_view.py`` and drives the
generic engine in ``editor.render_editor``.
"""
