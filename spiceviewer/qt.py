"""Use Qt 6 where installed, with a Qt 5 fallback for system packages."""

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    from PySide2 import QtCore, QtGui, QtWidgets
