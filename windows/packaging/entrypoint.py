"""Packaging-only dispatch; smoke branches run before app/profile/Tk startup."""

import sys


def main():
    if sys.argv[1:] in (["--packaging-smoke-check"], ["--packaging-smoke-worker"],
                       ["--packaging-gateway-smoke"]):
        from gui_runtime import attach_standard_streams
        attach_standard_streams()
        from smoke import gateway_smoke, smoke_check, smoke_worker
        return {"--packaging-smoke-check": smoke_check,
                "--packaging-smoke-worker": smoke_worker,
                "--packaging-gateway-smoke": gateway_smoke}[sys.argv[1]]()
    from gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
