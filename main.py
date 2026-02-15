"""Nexus Mod Updater – Application entry point."""

import logging

from gui import ModUpdaterApp


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    app = ModUpdaterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
