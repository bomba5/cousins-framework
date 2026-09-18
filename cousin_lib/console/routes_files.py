"""The cousin file explorer (docs/reference/console-api.md, "Cousin files"):
a read-only view of one cousin home. Directory listings are one level
at a time; reads page text by lines and never return a binary file as
text; the download streams the bytes as an attachment (raster images
inline). Confinement, the `.secrets/` rule and the link rule are
cousin_lib.home_files'."""
from __future__ import annotations

from cousin_lib import home_files
from cousin_lib.console import router
from cousin_lib.console._common import cousin_home
from cousin_lib.console.app import HttpError
from cousin_lib.console.sse import Stream

CHUNK = 64 * 1024


def _refused(err):
    return HttpError(err.status, str(err))


def _chunks(path):
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                return
            yield block


def register():
    @router.route("GET", "/api/cousins/{slug}/files")
    def listing(req, slug):
        home = cousin_home(req.server, slug)
        try:
            return 200, home_files.list_dir(
                home, req.query.get("path") or "",
                show_hidden=req.query.get("hidden") in ("1", "true"))
        except home_files.PathRefused as err:
            raise _refused(err)

    @router.route("GET", "/api/cousins/{slug}/files/read")
    def read(req, slug):
        home = cousin_home(req.server, slug)
        try:
            return 200, home_files.read_text_page(
                home, req.query.get("path") or "",
                start=req.int_query("start", 1),
                count=req.int_query("count", home_files.DEFAULT_PAGE_LINES))
        except home_files.PathRefused as err:
            raise _refused(err)

    @router.route("GET", "/api/cousins/{slug}/files/download")
    def download(req, slug):
        home = cousin_home(req.server, slug)
        try:
            path, st = home_files.open_file(home, req.query.get("path") or "")
        except home_files.PathRefused as err:
            raise _refused(err)
        ctype, inline = home_files.download_type(path)
        name = path.name.replace('"', "").replace("\\", "")
        stream = Stream(_chunks(path), headers=[
            ("Content-Length", str(st.st_size)),
            ("Content-Disposition", '%s; filename="%s"'
             % ("inline" if inline else "attachment", name)),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", "sandbox; default-src 'none'"),
            ("Cache-Control", "no-store"),
        ])
        stream.content_type = ctype
        return 200, stream


register()
