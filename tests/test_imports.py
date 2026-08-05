from tracecast import __version__
from tracecast.cli import build_parser


def test_package_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_parser_builds() -> None:
    parser = build_parser()
    assert parser.prog == "tracecast"

