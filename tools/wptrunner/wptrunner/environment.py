import json
import os
import signal
import socket
import sys
import time
import uuid

from mozlog import get_default_logger, handlers, proxy
from mozlog.structuredlog import StructuredLogger

from . import mpcontext
from .wptlogging import LogLevelRewriter

here = os.path.dirname(__file__)
repo_root = os.path.abspath(os.path.join(here, os.pardir, os.pardir, os.pardir))

sys.path.insert(0, repo_root)
from tools import localpaths  # noqa: F401

from wptserve.handlers import StringHandler

serve = None


def do_delayed_imports(logger, test_paths):
    global serve

    serve_root = serve_path(test_paths)
    sys.path.insert(0, serve_root)

    failed = []

    try:
        from tools.serve import serve
    except ImportError:
        failed.append("serve")

    if failed:
        logger.critical(
            "Failed to import %s. Ensure that tests path %s contains web-platform-tests" %
            (", ".join(failed), serve_root))
        sys.exit(1)


def serve_path(test_paths):
    return test_paths["/"]["tests_path"]


class TestEnvironmentError(Exception):
    pass


class _QueueLogger(StructuredLogger):
    """Structured logger that has a unique name and puts messages on a queue"""
    def __init__(self, queue):
        name = str(uuid.uuid4())
        self.queue = queue
        super().__init__(name)
        assert not self.handlers

    def _handle_log(self, data):
        self.queue.put(data)


class LoggerProxy:
    """Logger frontend that puts messages on a provided queue.

    This is intended for use in wptserve which doesn't directly depend
    on mozlog, so it presents an API similar to the stdlib logger module.

    To allow the data to be used directly by mozlog, we construct
    an internal StructuredLogger that is just used to transform the data
    into the correct format. This is given a unique name to avoid clashing
    with any other configured loggers.
    """

    def __init__(self, name, queue):
        self.name = name
        self._logger = _QueueLogger(queue)

    def critical(self, *args):
        self._logger.critical(*args)

    def error(self, *args):
        self._logger.error(*args)

    def warning(self, *args):
        self._logger.warning(*args)

    def info(self, *args):
        self._logger.info(*args)

    def debug(self, *args):
        self._logger.debug(*args)


class ProxyLoggingContext:
    """Context manager object that handles setup and teardown of a logger to pass to wptserve"""

    def __init__(self, component):
        server_logger = get_default_logger(component=component)
        assert server_logger is not None
        log_filter = handlers.LogLevelFilter(lambda x: x, "info")
        # Downgrade errors to warnings for the server
        log_filter = LogLevelRewriter(log_filter, ["error"], "warning")
        server_logger.component_filter = log_filter

        mp_context = mpcontext.get_context()
        self.log_queue = mp_context.Queue()
        self.logging_thread = proxy.LogQueueThread(self.log_queue, server_logger)
        self.logger = LoggerProxy("wptserve", self.log_queue)

    def __enter__(self):
        self.logging_thread.start()
        return self.logger

    def __exit__(self, *args):
        self.log_queue.put(None)
        # Wait for thread to shut down but not for too long since it's a daemon
        self.logging_thread.join(1)


class TestEnvironment(object):
    """Context manager that owns the test environment i.e. the http and
    websockets servers"""
    def __init__(self, test_paths, testharness_timeout_multipler,
                 pause_after_test, debug_test, debug_info, options, ssl_config, env_extras,
                 enable_quic=False, mojojs_path=None):
        self.test_paths = test_paths
        self.server = None
        self.config_ctx = None
        self.config = None
        self.server_logging_ctx = ProxyLoggingContext("wptserve")
        self.testharness_timeout_multipler = testharness_timeout_multipler
        self.pause_after_test = pause_after_test
        self.debug_test = debug_test
        self.test_server_port = options.pop("test_server_port", True)
        self.debug_info = debug_info
        self.options = options if options is not None else {}

        mp_context = mpcontext.get_context()
        self.cache_manager = mp_context.Manager()
        self.stash = serve.stash.StashServer(mp_context=mp_context)
        self.env_extras = env_extras
        self.env_extras_cms = None
        self.ssl_config = ssl_config
        self.enable_quic = enable_quic
        self.mojojs_path = mojojs_path

    def __enter__(self):
        server_logger = self.server_logging_ctx.__enter__()
        self.config_ctx = self.build_config(server_logger)

        self.config = self.config_ctx.__enter__()

        self.stash.__enter__()
        self.cache_manager.__enter__()

        assert self.env_extras_cms is None, (
            "A TestEnvironment object cannot be nested")

        self.env_extras_cms = []

        for env in self.env_extras:
            cm = env(self.options, self.config)
            cm.__enter__()
            self.env_extras_cms.append(cm)

        self.servers = serve.start(self.config,
                                   self.get_routes(),
                                   mp_context=mpcontext.get_context())

        if self.options.get("supports_debugger") and self.debug_info and self.debug_info.interactive:
            self.ignore_interrupts()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.process_interrupts()

        for scheme, servers in self.servers.items():
            for port, server in servers:
                server.kill()
        for cm in self.env_extras_cms:
            cm.__exit__(exc_type, exc_val, exc_tb)

        self.env_extras_cms = None

        self.cache_manager.__exit__(exc_type, exc_val, exc_tb)
        self.stash.__exit__()
        self.config_ctx.__exit__(exc_type, exc_val, exc_tb)
        self.server_logging_ctx.__exit__(exc_type, exc_val, exc_tb)

    def ignore_interrupts(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    def process_interrupts(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    def build_config(self, server_logger):
        override_path = os.path.join(serve_path(self.test_paths), "config.json")

        config = serve.ConfigBuilder(proxy_logger=server_logger)

        ports = {
            "http": [8000, 8001],
            "https": [8443, 8444],
            "ws": [8888],
            "wss": [8889],
            "h2": [9000],
        }
        if self.enable_quic:
            ports["quic-transport"] = [10000]
        config.ports = ports

        if os.path.exists(override_path):
            with open(override_path) as f:
                override_obj = json.load(f)
            config.update(override_obj)

        config.check_subdomains = False

        ssl_config = self.ssl_config.copy()
        ssl_config["encrypt_after_connect"] = self.options.get("encrypt_after_connect", False)
        config.ssl = ssl_config

        if "browser_host" in self.options:
            config.browser_host = self.options["browser_host"]

        if "bind_address" in self.options:
            config.bind_address = self.options["bind_address"]

        config.server_host = self.options.get("server_host", None)
        config.doc_root = serve_path(self.test_paths)

        return config

    def get_routes(self):
        route_builder = serve.RoutesBuilder()

        for path, format_args, content_type, route in [
                ("testharness_runner.html", {}, "text/html", "/testharness_runner.html"),
                ("print_reftest_runner.html", {}, "text/html", "/print_reftest_runner.html"),
                (os.path.join(here, "..", "..", "third_party", "pdf_js", "pdf.js"), None,
                 "text/javascript", "/_pdf_js/pdf.js"),
                (os.path.join(here, "..", "..", "third_party", "pdf_js", "pdf.worker.js"), None,
                 "text/javascript", "/_pdf_js/pdf.worker.js"),
                (self.options.get("testharnessreport", "testharnessreport.js"),
                 {"output": self.pause_after_test,
                  "timeout_multiplier": self.testharness_timeout_multipler,
                  "explicit_timeout": "true" if self.debug_info is not None else "false",
                  "debug": "true" if self.debug_test else "false"},
                 "text/javascript;charset=utf8",
                 "/resources/testharnessreport.js")]:
            path = os.path.normpath(os.path.join(here, path))
            # Note that .headers. files don't apply to static routes, so we need to
            # readd any static headers here.
            headers = {"Cache-Control": "max-age=3600"}
            route_builder.add_static(path, format_args, content_type, route,
                                     headers=headers)

        data = b""
        with open(os.path.join(repo_root, "resources", "testdriver.js"), "rb") as fp:
            data += fp.read()
        with open(os.path.join(here, "testdriver-extra.js"), "rb") as fp:
            data += fp.read()
        route_builder.add_handler("GET", "/resources/testdriver.js",
                                  StringHandler(data, "text/javascript"))

        for url_base, paths in self.test_paths.items():
            if url_base == "/":
                continue
            route_builder.add_mount_point(url_base, paths["tests_path"])

        if "/" not in self.test_paths:
            del route_builder.mountpoint_routes["/"]

        if self.mojojs_path:
            route_builder.add_mount_point("/gen/", self.mojojs_path)

        return route_builder.get_routes()

    def ensure_started(self):
        # Pause for a while to ensure that the server has a chance to start
        total_sleep_secs = 30
        each_sleep_secs = 0.5
        end_time = time.time() + total_sleep_secs
        while time.time() < end_time:
            failed, pending = self.test_servers()
            if failed:
                break
            if not pending:
                return
            time.sleep(each_sleep_secs)
        raise EnvironmentError("Servers failed to start: %s" %
                               ", ".join("%s:%s" % item for item in failed))

    def test_servers(self):
        failed = []
        pending = []
        host = self.config["server_host"]
        for scheme, servers in self.servers.items():
            for port, server in servers:
                if not server.is_alive():
                    failed.append((scheme, port))

        if not failed and self.test_server_port:
            for scheme, servers in self.servers.items():
                # TODO(Hexcles): Find a way to test QUIC's UDP port.
                if scheme == "quic-transport":
                    continue
                for port, server in servers:
                    s = socket.socket()
                    s.settimeout(0.1)
                    try:
                        s.connect((host, port))
                    except socket.error:
                        pending.append((host, port))
                    finally:
                        s.close()

        return failed, pending
