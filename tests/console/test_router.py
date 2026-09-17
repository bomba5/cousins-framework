import unittest
from cousin_lib.console import router


class RouterContract(unittest.TestCase):
    def setUp(self):
        router.clear()

    def test_route_registers_and_dispatches_with_path_params(self):
        @router.route("GET", "/api/cousins/{slug}")
        def show(req, slug):
            return 200, {"slug": slug}
        status, body = router.dispatch("GET", "/api/cousins/testa", req=None)
        self.assertEqual((status, body), (200, {"slug": "testa"}))

    def test_unknown_path_is_404_and_wrong_method_is_405(self):
        @router.route("GET", "/api/jobs")
        def jobs(req):
            return 200, {"jobs": []}
        self.assertEqual(router.dispatch("GET", "/api/nothing", req=None)[0], 404)
        self.assertEqual(router.dispatch("POST", "/api/jobs", req=None)[0], 405)

    def test_literal_segments_win_over_params_and_registration_is_idempotent(self):
        @router.route("GET", "/api/loops/{slug}")
        def per(req, slug):
            return 200, {"per": slug}
        @router.route("GET", "/api/loops/recent")
        def recent(req):
            return 200, {"recent": True}
        self.assertEqual(router.dispatch("GET", "/api/loops/recent", req=None)[1], {"recent": True})
        self.assertEqual(router.dispatch("GET", "/api/loops/x", req=None)[1], {"per": "x"})
        router.route("GET", "/api/loops/recent")(recent)
        self.assertEqual(len([r for r in router.routes() if r[1] == "/api/loops/recent"]), 1)
