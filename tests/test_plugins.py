"""cousin_lib/plugins.py: config/plugins.toml, the manifest, placeholders
and a cousin's [plugins] enabled. A fake plugin ("clock") in a temp root;
nothing is started here."""
import pathlib
import tempfile

from cousin_lib import plugins
from tests import _plugins as fake
from tests._hermetic import HermeticCase


class PluginCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()

    def home(self, slug, *enabled, raw=None):
        home = self.root / "cousins" / slug
        home.mkdir(parents=True, exist_ok=True)
        text = '[cousin]\nslug = "%s"\nname = "%s"\n' % (slug, slug.capitalize())
        (home / "cousin.toml").write_text(text + (raw or ""))
        if enabled:
            fake.enable(home, *enabled)
        return home

    def problems(self):
        return plugins.load(self.root)[1]


class TestLoad(PluginCase):
    def test_no_config_is_no_plugins(self):
        self.assertEqual(plugins.load(self.root), ({}, []))

    def test_a_good_plugin_loads_with_every_part(self):
        directory = fake.write_plugin(self.root, port=18180)
        fake.declare(self.root, "clock")
        loaded, problems = plugins.load(self.root)
        self.assertEqual(problems, [])
        clock = loaded["clock"]
        self.assertEqual(clock.dir, directory.resolve())
        self.assertEqual((clock.description, clock.version), ("a fake clock", "0.1.0"))
        self.assertEqual(clock.service_url, "http://127.0.0.1:18180")
        row = clock.row()
        self.assertEqual({k: row[k] for k in ("name", "mcp", "service", "console", "port",
                                              "title")},
                         {"name": "clock", "mcp": True, "service": True, "console": True,
                          "port": 18180, "title": "Clock"})

    def test_an_absolute_path_works_too(self):
        directory = fake.write_plugin(self.root)
        fake.declare(self.root, raw='[plugins.clock]\npath = "%s"\n' % directory)
        self.assertEqual(list(plugins.load(self.root)[0]), ["clock"])

    def test_a_manifest_with_only_mcp_is_fine(self):
        fake.write_plugin(self.root, service=False, console=False)
        fake.declare(self.root, "clock")
        loaded, problems = plugins.load(self.root)
        self.assertEqual(problems, [])
        self.assertIsNone(loaded["clock"].service_url)

    def test_disabled_is_hidden_not_a_problem(self):
        fake.write_plugin(self.root)
        fake.declare(self.root, "clock", enabled={"clock": False})
        self.assertEqual(plugins.load(self.root), ({}, []))

    def test_one_bad_plugin_does_not_stop_another(self):
        fake.write_plugin(self.root, "clock")
        fake.write_plugin(self.root, "dial", text='name = "dial"\ncolour = "red"\n')
        fake.declare(self.root, "clock", "dial")
        loaded, problems = plugins.load(self.root)
        self.assertEqual(list(loaded), ["clock"])
        self.assertEqual([p["name"] for p in problems], ["dial"])
        self.assertIn("colour", problems[0]["reason"])


class TestRefusals(PluginCase):
    def refused(self, text=None, *, raw=None, name="clock"):
        if text is not None:
            fake.write_plugin(self.root, name, text=text)
        if raw is not None:
            fake.declare(self.root, raw=raw)
        else:
            fake.declare(self.root, name)
        loaded, problems = plugins.load(self.root)
        self.assertEqual(loaded, {})
        self.assertEqual(len(problems), 1, problems)
        return problems[0]["reason"]

    def test_the_config_file(self):
        self.assertIn("does not parse", self.refused(raw="[plugins\n"))
        self.assertIn("unknown key", plugins.load(self._raw('extra = 1\n'))[1][0]["reason"])

    def _raw(self, text):
        (self.root / "config" / "plugins.toml").write_text(text)
        return self.root

    def test_the_entry(self):
        fake.write_plugin(self.root)
        self.assertIn("needs `path`", self.refused(raw="[plugins.clock]\nenabled = true\n"))
        self.assertIn("unknown key", self.refused(raw='[plugins.clock]\npath = "plugins-local/'
                                                      'clock"\nport = 1\n'))
        self.assertIn("true or false", self.refused(raw='[plugins.clock]\npath = "plugins-local'
                                                        '/clock"\nenabled = "yes"\n'))
        self.assertIn("not a directory", self.refused(raw='[plugins.clock]\npath = "nowhere"\n'))

    def test_the_name(self):
        for bad in ("Clock", "9lives", "a" * 33, "cousin"):
            fake.write_plugin(self.root, bad, text='name = "%s"\n' % bad)
            reason = self.refused(raw='[plugins.%s]\npath = "plugins-local/%s"\n' % (bad, bad),
                                  name=bad)
            self.assertTrue("must match" in reason or "reserved" in reason, (bad, reason))

    def test_the_manifest_names_its_key(self):
        self.assertIn("names 'dial'", self.refused('name = "dial"\n'))

    def test_no_manifest(self):
        (self.root / "plugins-local" / "clock").mkdir(parents=True)
        self.assertIn("no plugin.toml", self.refused())

    def test_unknown_keys(self):
        self.assertIn("unknown key", self.refused('name = "clock"\nhomepage = "x"\n'))
        self.assertIn("[mcp] has unknown key", self.refused(
            'name = "clock"\n[mcp]\ncommand = "x"\ncwd = "/"\n'))

    def test_parts(self):
        base = 'name = "clock"\n'
        cases = {
            "[mcp] needs a `command`": "[mcp]\nargs = []\n",
            "`args` must be a list": '[mcp]\ncommand = "x"\nargs = "y"\n',
            "`env` must map": '[mcp]\ncommand = "x"\nenv = { A = 1 }\n',
            "needs `port`": '[service]\ncommand = "x"\n',
            "(1-65535)": '[service]\ncommand = "x"\nport = 70000\n',
            "`health` must be": '[service]\ncommand = "x"\nport = 9\nhealth = "healthz"\n',
            "[console] needs [service]": '[console]\ntitle = "C"\npage = "/"\n',
            "needs a `title`": '[service]\ncommand = "x"\nport = 9\n[console]\npage = "/"\n',
            "needs `page`": '[service]\ncommand = "x"\nport = 9\n[console]\ntitle = "C"\n'
                            'page = "p"\n',
        }
        for why, text in cases.items():
            self.assertIn(why, self.refused(base + text), text)

    def test_placeholders(self):
        base = 'name = "clock"\n'
        self.assertIn("unknown placeholder {user}", self.refused(
            base + '[mcp]\ncommand = "x"\nargs = ["{user}"]\n'))
        # {slug} and {home} are a cousin's: never in the install's service
        self.assertIn("unknown placeholder {slug}", self.refused(
            base + '[service]\ncommand = "x"\nport = 9\nenv = { S = "{slug}" }\n'))
        self.assertIn("no [service]", self.refused(
            base + '[mcp]\ncommand = "x"\nenv = { U = "{service_url}" }\n'))

    def test_two_services_on_one_port(self):
        fake.write_plugin(self.root, "clock", port=18181)
        fake.write_plugin(self.root, "dial", port=18181)
        fake.declare(self.root, "clock", "dial")
        loaded, problems = plugins.load(self.root)
        self.assertEqual(list(loaded), ["clock"])
        self.assertIn("already clock's", problems[0]["reason"])


class TestRender(PluginCase):
    def setUp(self):
        super().setUp()
        self.dir = fake.write_plugin(self.root, port=18182).resolve()
        fake.declare(self.root, "clock")
        self.clock = plugins.load(self.root)[0]["clock"]

    def test_render(self):
        self.assertEqual(plugins.render("{a}/{b} {c", {"a": "1", "b": "2"}), "1/2 {c")
        with self.assertRaises(KeyError):
            plugins.render("{nope}", {})

    def test_the_mcp_server_for_a_cousin(self):
        home = self.home("wren", "clock")
        server = self.clock.mcp_server(self.root, slug="wren", home=home)
        self.assertEqual(server["args"], ["%s/mcp_server.py" % self.dir])
        self.assertEqual(server["env"], {
            "CLOCK_URL": "http://127.0.0.1:18182", "CLOCK_FOR": "wren", "COUSIN_SLUG": "wren",
            "COUSIN_HOME": str(home), "FRAMEWORK_ROOT": str(self.root),
            "PLUGIN_DIR": str(self.dir)})

    def test_the_service(self):
        argv, env = self.clock.service_command(self.root)
        self.assertEqual(argv[1:], ["%s/server.py" % self.dir])
        self.assertEqual(env, {"CLOCK_MODE": "fake", "PLUGIN_PORT": "18182",
                               "PLUGIN_DIR": str(self.dir), "FRAMEWORK_ROOT": str(self.root)})

    def test_the_console_tab(self):
        home = self.home("wren", "clock")
        self.assertEqual(self.clock.console_tab(self.root, slug="wren", home=home),
                         {"name": "clock", "title": "Clock", "url": "/plugins/clock/page/wren"})


class TestCousin(PluginCase):
    def setUp(self):
        super().setUp()
        fake.write_plugin(self.root, "clock")
        fake.write_plugin(self.root, "dial", mcp=False, console=False)
        fake.write_plugin(self.root, "bell", text='name = "bell"\nshade = 1\n')
        fake.declare(self.root, "clock", "dial", "bell")

    def test_enabled_for_is_the_valid_enabled_ones_by_name(self):
        home = self.home("wren", "dial", "clock", "clock", "bell", "ghost")
        self.assertEqual(plugins.cousin_enabled(home), (["dial", "clock", "bell", "ghost"], None))
        self.assertEqual([p.name for p in plugins.enabled_for(home, self.root)],
                         ["clock", "dial"])
        _, skipped = plugins.cousin_report(home, self.root)
        self.assertEqual([s["name"] for s in skipped], ["bell", "ghost"])
        self.assertIn("shade", skipped[0]["reason"])
        self.assertIn("not an enabled plugin", skipped[1]["reason"])

    def test_no_table_is_none_and_a_bad_one_is_said(self):
        self.assertEqual(plugins.cousin_enabled(self.home("wren")), ([], None))
        names, why = plugins.cousin_enabled(self.home("sam", raw='[plugins]\nenabled = "clock"\n'))
        self.assertEqual(names, [])
        self.assertIn("list of plugin names", why)
        names, why = plugins.cousin_enabled(self.home("toki", raw='[plugins]\nclock = true\n'))
        self.assertIn("unknown key", why)

    def test_mcp_servers_and_tabs(self):
        home = self.home("wren", "clock", "dial")
        servers, skipped = plugins.mcp_servers(home, self.root)
        self.assertEqual(list(servers), ["clock"])          # dial has no [mcp]
        self.assertEqual(skipped, [])
        self.assertEqual(plugins.console_tabs(home, self.root),
                         [{"name": "clock", "title": "Clock", "url": "/plugins/clock/page/wren"}])

    def test_services_wanted_only_when_a_cousin_enables_one(self):
        self.home("wren")
        self.assertEqual(plugins.services_wanted(self.root), {})
        self.home("sam", "dial")
        self.assertEqual(list(plugins.services_wanted(self.root)), ["dial"])
