import sys
from ssh_jumper.instance import SingleInstance


def main():
    instance = SingleInstance()
    if not instance.acquire():
        return 0
    try:
        from PySide6.QtWidgets import QApplication
        from ssh_jumper.ui import MainWindow, STYLE
        app = QApplication(sys.argv)
        app.setApplicationName("SSH Jumper")
        app.setOrganizationName("SSH Jumper")
        app.setQuitOnLastWindowClosed(False)
        app.setStyle("Fusion")
        app.setStyleSheet(STYLE)
        window = MainWindow()
        window.show()
        return app.exec()
    finally:
        instance.close()


if __name__ == "__main__":
    sys.exit(main())
