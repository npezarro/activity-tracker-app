import logging
import logging.handlers
import os
import sys


def _setup_logging():
    from . import paths

    handler = logging.handlers.RotatingFileHandler(
        os.path.join(paths.data_dir(), "activitytracker.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def _setup_tls():
    """The frozen macOS app's OpenSSL looks for a CA file at a build-machine path that
    users' Macs don't have; point it at the bundled certifi list instead."""
    if sys.platform != "darwin" or os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        if os.path.exists(certifi.where()):
            os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


def main():
    _setup_tls()
    _setup_logging()
    if "--update-now" in sys.argv:  # check + download + hand over (tests/CI)
        from .update import run_cli

        i = sys.argv.index("--update-now")
        return run_cli(sys.argv[i + 1] if len(sys.argv) > i + 1 else None)
    if "--selftest" in sys.argv:
        from .selftest import run

        return run(sys.argv[1:])
    smoke = None
    if "--smoke-ui" in sys.argv:
        i = sys.argv.index("--smoke-ui")
        smoke = sys.argv[i + 1]
    from . import single
    from .app import App

    holder = {}
    if not smoke and not single.claim(lambda: holder["app"].ui_q.put(("show",)) if "app" in holder else None):
        return 0
    try:
        holder["app"] = App(smoke)
        holder["app"].run()
        return 0
    except Exception:
        logging.getLogger("activitytracker").exception("fatal")
        raise


if __name__ == "__main__":
    sys.exit(main())
