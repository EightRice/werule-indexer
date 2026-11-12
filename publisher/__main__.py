"""python -m publisher --network Localhost [--networks-index] --target dir:C:\\path | r2:<bucket>

Publishes one network's documents from the document store (DOCSTORE_PATH, default
data/werule.sqlite) to the target. One publisher per network; exactly one of them also writes
networks.json (--networks-index)."""
import argparse
import logging
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m publisher")
    ap.add_argument("--network", required=True, help="network name, as in networks/<name>.json")
    ap.add_argument("--target", required=True, help="dir:<path> or r2:<bucket>")
    ap.add_argument("--networks-index", action="store_true", help="also publish networks.json")
    ap.add_argument("--store", default=None, help="document store file (default: DOCSTORE_PATH or data/werule.sqlite)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=os.getenv("PUBLISHER_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("publisher")

    sys.path.insert(0, os.getcwd())
    import networks
    if not networks.NAME_RE.match(args.network):
        ap.error(f"network name {args.network!r} does not match [A-Za-z0-9-]{{1,64}}")
    N = args.network
    C = networks.collections(N)["dao_collection_name"]
    store = args.store or os.getenv("DOCSTORE_PATH") or os.path.join("data", "werule.sqlite")
    if not os.path.exists(store):
        ap.error(f"no document store at {store}")

    from .targets import make_target
    from .core import Publisher
    from .notify import Notifier
    from .source import StoreFeed

    target = make_target(args.target)
    notifier = None
    url = os.getenv("NOTIFY_URL")
    if url:
        secret = os.getenv("NOTIFY_SECRET")
        if not secret:
            ap.error("NOTIFY_URL is set but NOTIFY_SECRET is not")
        notifier = Notifier(url, secret)
    pub = Publisher(N, C, target, notifier=notifier, networks_index=args.networks_index)
    log.info("publishing %s (%s) from %s to %s%s%s", N, C, store, args.target,
             " with networks.json" if args.networks_index else "",
             f", notifying {url}" if url else ", no notify")

    feed = StoreFeed(store, C, pub, args.networks_index)
    feed.load()
    if pub.fatal is not None:
        raise SystemExit(f"fatal: {pub.fatal}")
    # a feed that stopped would leave the files silently stale: exit and let the supervisor restart
    # (a restart costs one full read and sends only the real diff)
    pub.health = lambda: f"change feed stopped: {feed.error}" if feed.dead() else None
    if notifier is not None:
        notifier.start()
    pub.startup()
    feed.start()
    try:
        pub.run()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        feed.stop()
        if notifier is not None:
            notifier.stop()


if __name__ == "__main__":
    main()
