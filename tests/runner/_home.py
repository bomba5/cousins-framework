"""A throwaway cousin home for runner tests. Invented cast only."""
import pathlib
import tempfile


def temp_home(case, slug="wren", runner="sdk"):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / slug
    for sub in ("data", "run", "memory"):
        (home / sub).mkdir(parents=True)
    (home / "cousin.toml").write_text(
        '[cousin]\nslug = "%s"\nname = "%s"\n\n[agent]\nrunner = "%s"\n'
        % (slug, slug.capitalize(), runner))
    return home
