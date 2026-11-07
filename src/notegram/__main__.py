"""Compatibility entry point for python -m notegram and the notegram command."""


def run(argv=None):
    from .server.cli import run as server_main

    return server_main(argv)


if __name__ == "__main__":
    run()
