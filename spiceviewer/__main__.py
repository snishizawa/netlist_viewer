"""Open a netlist and show it in a desktop schematic view."""

import argparse
from pathlib import Path
import sys

from .connectivity import Connectivity
from .layout import Layout, ViewState
from .netlist import NetlistError, read_netlist
from .qt import QtCore, QtGui, QtWidgets
from .routing import RoutingError
from .scene import DiagramScene, HIGHLIGHT_COLORS


Qt = QtCore.Qt
INPUT_FILES = "SPICE / CDL (*.sp *.spi *.spice *.cir *.net *.cdl)"


class Canvas(QtWidgets.QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)

    def zoom(self, multiplier):
        if 0.03 <= self.transform().m11() * multiplier <= 6:
            self.scale(multiplier, multiplier)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom(1.15 ** (delta / 120))
            event.accept()
        else:
            super().wheelEvent(event)


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.language = "ja"
        self.resize(1180, 780)
        self.state = ViewState()
        self.layout_engine = None
        self.diagram = None
        self.filename = None
        self.scene = DiagramScene(self.state, self.toggle_instance, self.toggle_bus,
                                  self.toggle_wiring, self.highlight_signal, self)
        self.canvas = Canvas(self.scene)
        self.setCentralWidget(self.canvas)
        self.toolbar = self.addToolBar("回路図")
        self.toolbar.setMovable(False)
        self.open_action = self.toolbar.addAction("開く…", self.open_dialog)
        self.open_action.setShortcut(QtGui.QKeySequence.Open)
        self.save_action = self.toolbar.addAction("PNG保存…", self.save_dialog)
        self.save_action.setShortcut(QtGui.QKeySequence.Save)
        self.save_action.setEnabled(False)
        self.toolbar.addSeparator()
        zoom_out = self.toolbar.addAction("縮小", lambda: self.canvas.zoom(1 / 1.25))
        zoom_in = self.toolbar.addAction("拡大", lambda: self.canvas.zoom(1.25))
        fit = self.toolbar.addAction("全体表示", self.fit)
        self.toolbar.addSeparator()
        self.clear_highlight_action = self.toolbar.addAction("強調を解除", self.clear_highlight)
        self.clear_highlight_action.setEnabled(False)
        self.toolbar.addSeparator()
        self.language_action = self.toolbar.addAction("English", self.toggle_language)
        self.menu_labels = (
            (self.open_action, "開く…", "Open…"),
            (self.save_action, "PNG保存…", "Save PNG…"),
            (zoom_out, "縮小", "Zoom Out"),
            (zoom_in, "拡大", "Zoom In"),
            (fit, "全体表示", "Fit View"),
            (self.clear_highlight_action, "強調を解除", "Clear Highlights"),
            (self.language_action, "English", "日本語"),
        )
        self.update_menu_language()
        self.statusBar()
        self.setStyleSheet("""
            QToolBar { background: #eef3f5; border: 0; padding: 8px; spacing: 7px; }
            QToolButton { color: #25414c; background: #ffffff; border: 1px solid #d3dfe3;
                          border-radius: 5px; padding: 6px 11px; }
            QToolButton:hover { background: #e1edf2; border-color: #9cb8c3; }
            QToolButton:disabled { color: #97a4a8; }
            QStatusBar { color: #3f5963; background: #eef3f5; }
        """)

    def ui_text(self, japanese, english):
        return english if self.language == "en" else japanese

    def update_window_title(self):
        title = self.ui_text("SPICE / CDL ネットリストビューアー", "SPICE / CDL Netlist Viewer")
        self.setWindowTitle(f"{self.filename.name} — {title}" if self.filename else title)

    def update_menu_language(self):
        self.toolbar.setWindowTitle(self.ui_text("回路図", "Schematic"))
        for action, japanese, english in self.menu_labels:
            action.setText(self.ui_text(japanese, english))
        self.language_action.setToolTip(self.ui_text("英語に切り替え", "Switch to Japanese"))
        self.update_window_title()

    def toggle_language(self):
        self.language = "en" if self.language == "ja" else "ja"
        self.update_menu_language()

    def open_dialog(self):
        all_files = self.ui_text("すべてのファイル (*)", "All Files (*)")
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.ui_text("ネットリストを開く", "Open Netlist"),
            str(self.filename.parent) if self.filename else "", f"{INPUT_FILES};;{all_files}",
        )
        return bool(filename) and self.open_file(filename)

    def open_file(self, filename):
        path = Path(filename)
        try:
            netlist = read_netlist(path)
            top = netlist.candidates[0]
            if len(netlist.candidates) > 1:
                choices = [candidate.name for candidate in netlist.candidates]
                name, accepted = QtWidgets.QInputDialog.getItem(
                    self, self.ui_text("最上位回路", "Top-Level Circuit"),
                    self.ui_text("表示するサブ回路", "Subcircuit to display"), choices, 0, False,
                )
                if not accepted:
                    return False
                top = netlist.candidates[choices.index(name)]
            state = ViewState()
            engine = Layout(Connectivity(netlist, top), state)
            diagram = engine.build()
        except (NetlistError, RoutingError) as error:
            print(error, file=sys.stderr)
            QtWidgets.QMessageBox.critical(self, self.ui_text("回路図を開けません", "Cannot Open Schematic"), str(error))
            QtCore.QCoreApplication.exit(1)
            return False
        self.state, self.layout_engine, self.diagram = state, engine, diagram
        self.filename = path
        self.scene.state = state
        self.scene.rebuild(diagram)
        self.save_action.setEnabled(True)
        self.clear_highlight_action.setEnabled(False)
        self.update_window_title()
        self.statusBar().showMessage(f"{path.name}  /  {top.name}")
        self.fit()
        return True

    def fit(self):
        if self.diagram:
            self.canvas.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
            if self.canvas.transform().m11() > 1.5:
                self.canvas.resetTransform()
                self.canvas.centerOn(self.scene.sceneRect().center())

    def toggle_instance(self, path):
        self.update_layout(self.state.instances, path)

    def toggle_bus(self, path, name):
        self.update_layout(self.state.buses, (path, name))

    def update_layout(self, selections, key):
        selections.symmetric_difference_update((key,))
        try:
            diagram = self.layout_engine.build()
        except RoutingError as error:
            selections.symmetric_difference_update((key,))
            QtWidgets.QMessageBox.warning(self, self.ui_text("配線を更新できません", "Cannot Update Wiring"), str(error))
            return
        center = self.canvas.mapToScene(self.canvas.viewport().rect().center())
        self.diagram = diagram
        self.scene.rebuild(diagram)
        self.canvas.centerOn(center)

    def toggle_wiring(self, key):
        self.state.hidden_wiring.symmetric_difference_update((key,))
        self.scene.rebuild(self.diagram)

    def highlight_signal(self, keys):
        if not keys:
            return
        color = HIGHLIGHT_COLORS[self.state.highlight_index]
        self.state.highlights.append((color, keys))
        self.state.highlight_index = (self.state.highlight_index + 1) % len(HIGHLIGHT_COLORS)
        if len(self.state.highlights) > len(HIGHLIGHT_COLORS):
            self.state.highlights.pop(0)
        self.clear_highlight_action.setEnabled(True)
        self.scene.rebuild(self.diagram)

    def clear_highlight(self):
        self.state.highlights.clear()
        self.state.highlight_index = 0
        self.clear_highlight_action.setEnabled(False)
        self.scene.rebuild(self.diagram)

    def save_dialog(self):
        if self.filename is None:
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.ui_text("回路図をPNGとして保存", "Save Schematic as PNG"),
            str(self.filename.with_suffix(".png")), self.ui_text("PNG画像 (*.png)", "PNG Image (*.png)"),
        )
        if not filename:
            return
        if not filename.lower().endswith(".png"):
            filename += ".png"
        try:
            width, height = self.scene.save_png(filename)
        except (OSError, MemoryError) as error:
            QtWidgets.QMessageBox.warning(self, self.ui_text("PNG保存に失敗しました", "Could Not Save PNG"), str(error))
            return
        saved = self.ui_text("保存しました", "Saved")
        self.statusBar().showMessage(f"{saved}  {Path(filename).name}  ({width} × {height})", 7000)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", nargs="?", help="SPICE or CDL file to open")
    args = parser.parse_args(argv)
    application = QtWidgets.QApplication([sys.argv[0]])
    window = Window()
    window.show()
    if args.netlist:
        QtCore.QTimer.singleShot(0, lambda: window.open_file(args.netlist))
    else:
        QtCore.QTimer.singleShot(0, window.open_dialog)
    run = getattr(application, "exec", None) or application.exec_
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
