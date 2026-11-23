"""Draw the same interactive schematic for display and raster export."""

import math

from .layout import GRID, MOS_DS_X
from .qt import QtCore, QtGui, QtWidgets
from .routing import junctions, segments


Qt = QtCore.Qt
Rect = QtCore.QRectF
Point = QtCore.QPointF
INK = "#293a48"
WIRE = "#2c5564"
HIGHLIGHT_COLORS = ("#d21f28", "#65d61a", "#1677d2", "#f28c18")
POWER = "#a64d23"
BUS = "#6556a1"
PAPER = "#ffffff"
FRAME = "#6e8991"
FONT = "Sans Serif"


def pixel(at, offset=(0, 0)):
    return (at[0] + offset[0]) * GRID, (at[1] + offset[1]) * GRID


def pen(color, width=1.5):
    result = QtGui.QPen(QtGui.QColor(color))
    result.setWidthF(width)
    result.setCapStyle(Qt.RoundCap)
    result.setJoinStyle(Qt.RoundJoin)
    return result


class ClickArea(QtWidgets.QGraphicsRectItem):
    def __init__(self, rect, action, brush=Qt.NoBrush, outline=Qt.NoPen):
        super().__init__(rect)
        self.action = action
        self.setPen(QtGui.QPen(outline))
        self.setBrush(QtGui.QBrush(brush))
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()
        if self.contains(event.pos()):
            action = self.action
            point = event.scenePos()
            QtCore.QTimer.singleShot(0, lambda: action(point))


class SignalLine(QtWidgets.QGraphicsLineItem):
    def __init__(self, a, b, select):
        super().__init__(*a, *b)
        self.select = select
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def shape(self):
        path = QtGui.QPainterPath(self.line().p1())
        path.lineTo(self.line().p2())
        stroke = QtGui.QPainterPathStroker()
        stroke.setWidth(8)
        return stroke.createStroke(path)

    def boundingRect(self):
        return self.shape().boundingRect()

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()
        movement = event.screenPos() - event.buttonDownScreenPos(Qt.LeftButton)
        if movement.manhattanLength() <= QtWidgets.QApplication.startDragDistance() and self.contains(event.pos()):
            QtCore.QTimer.singleShot(0, self.select)


class DiagramScene(QtWidgets.QGraphicsScene):
    def __init__(self, state, on_instance, on_bus, on_wiring, on_signal, parent=None):
        super().__init__(parent)
        self.state = state
        self.on_instance, self.on_bus, self.on_wiring = on_instance, on_bus, on_wiring
        self.on_signal = on_signal
        self.setBackgroundBrush(QtGui.QColor(PAPER))
        self.label_rects = []
        self.blocked_labels = []
        self.wire_captions = []

    def rebuild(self, diagram):
        self.clear()
        self.label_rects.clear()
        self.blocked_labels.clear()
        self.wire_captions.clear()
        pending = [(diagram, (0, 0), 0)]
        while pending:
            current, offset, depth = pending.pop()
            self.draw_scope(current, offset, depth)
            for dev in reversed(current.devices):
                if dev.child:
                    pending.append((dev.child, (offset[0] + dev.box.x, offset[1] + dev.box.y), depth + 1))
        for args in self.wire_captions:
            self.route_label(*args)
        if diagram.control_lines:
            below = self.itemsBoundingRect().bottom()
            font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
            font.setPixelSize(12)
            commands = self.addText("\n".join(diagram.control_lines), font)
            commands.setDefaultTextColor(QtGui.QColor(INK))
            commands.setPos(0, max(diagram.height * GRID, below) + GRID)
            commands.setAcceptedMouseButtons(Qt.NoButton)
        self.setSceneRect(self.itemsBoundingRect().adjusted(-18, -18, 18, 18))

    def wire_visible(self, pin):
        return pin.toggle not in self.state.hidden_wiring

    def line(self, a, b, z, color=WIRE, width=1.5):
        if a == b:
            return
        item = self.addLine(*a, *b, pen(color, width))
        item.setZValue(z)

    def wire_line(self, a, b, z, color, width, keys):
        if not keys:
            self.line(a, b, z, color, width)
            return
        if a == b:
            return
        highlight = self.state.highlight_color(keys)
        item = SignalLine(a, b, lambda: self.on_signal(keys))
        item.setPen(pen(highlight or color, width + 1.5 if highlight else width))
        item.setZValue(z + 0.25 if highlight else z)
        self.addItem(item)

    def dot(self, at, z, radius, fill, outline=None):
        x, y = at
        item = self.addEllipse(x - radius, y - radius, 2 * radius, 2 * radius,
                               pen(outline) if outline else QtGui.QPen(Qt.NoPen),
                               QtGui.QBrush(QtGui.QColor(fill)))
        item.setZValue(z)

    def text(self, text, at, z, color=INK, size=12, bold=False, align="left", action=None, backing=False):
        item = self.addSimpleText(text)
        font = QtGui.QFont(FONT)
        font.setPixelSize(size)
        font.setBold(bold)
        item.setFont(font)
        item.setBrush(QtGui.QColor(color))
        item.setAcceptedMouseButtons(Qt.NoButton)
        x, y = at
        width = item.boundingRect().width()
        shift = {"left": 0, "center": width / 2, "right": width}[align]
        item.setPos(x - shift, y - item.boundingRect().height())
        item.setZValue(z + 1)
        rect = item.sceneBoundingRect().adjusted(-2, 0, 2, 0)
        self.label_rects.append(rect)
        if action:
            self.click(rect.adjusted(-3, -2, 3, 2), z, action, "#eeebfb", "#d7d1ef")
        elif backing:
            bg = self.addRect(rect, QtGui.QPen(Qt.NoPen), QtGui.QColor(PAPER))
            bg.setZValue(z)
            bg.setAcceptedMouseButtons(Qt.NoButton)
        return item

    def click(self, rect, z, callback, color=None, outline=None):
        brush = QtGui.QColor(color) if color else Qt.NoBrush
        item = ClickArea(rect, callback, brush, pen(outline, 1) if outline else Qt.NoPen)
        self.addItem(item)
        item.setZValue(z)
        return item

    def marker(self, pin, offset, z):
        at = pixel(pin.tip, offset)
        x, y = at
        restore = not self.wire_visible(pin)
        rect = Rect(x - 9, y - 9, 18, 18)
        item = self.click(rect, z, lambda _: self.on_wiring(pin.toggle))
        target = f"{pin.net} の基板配線" if pin.toggle.bulk_only else f"{pin.net} の配線"
        verb = "再表示" if restore else "隠す"
        item.setToolTip(f"{target}を{verb}")
        self.dot(at, z + 0.1, 5 if restore else 4, PAPER, POWER)
        if restore:
            self.line((x - 2.5, y), (x + 2.5, y), z + 0.2, POWER, 1.2)
            self.line((x, y - 2.5), (x, y + 2.5), z + 0.2, POWER, 1.2)

    def draw_scope(self, diagram, offset, depth):
        z = depth * 10
        ox, oy = pixel((0, 0), offset)
        w, h = pixel((diagram.width, diagram.height))
        bounds = Rect(ox, oy, w, h)
        collapsible = bool(diagram.path)
        fill = "#eef5f7" if diagram.collapsed else PAPER
        if diagram.collapsed:
            item = self.click(bounds, z, lambda _: self.on_instance(diagram.path), fill, FRAME)
            item.setToolTip(f"{diagram.title} を展開")
        else:
            frame = self.addRect(bounds, pen(FRAME if collapsible else "#d7e0e2", 1.2),
                                 QtGui.QColor(fill))
            frame.setZValue(z)
            if collapsible:
                callback = lambda _: self.on_instance(diagram.path)
                for box in (Rect(ox, oy, w, 25), Rect(ox, oy + h - 5, w, 10),
                            Rect(ox - 4, oy + 25, 9, h - 30), Rect(ox + w - 4, oy + 25, 9, h - 30)):
                    self.click(box, z + 2, callback).setToolTip(f"{diagram.title} を格納")
        self.text(diagram.title, (ox + w / 2, oy + 21), z + 2, size=14, bold=True, align="center")

        for wire in diagram.wires:
            active = [pin for pin in wire.pins if self.wire_visible(pin)]
            tree = wire.tree.visible(pin.anchor for pin in active)
            color = BUS if wire.bus else (POWER if all(p.toggle is not None for p in wire.pins) else WIRE)
            anchors = [pin.anchor for pin in active]
            keys = frozenset(wire.signals[p.net] for p in active if p.net in wire.signals)
            for a, b in segments(tree, anchors):
                self.wire_line(pixel(a, offset), pixel(b, offset), z + 1, color, 4 if wire.bus else 1.55, keys)
            if not wire.bus:
                leads = (pin.anchor for pin in active if pin.tip != pin.anchor)
                junction_color = self.state.highlight_color(keys) or color
                for point in junctions(tree, leads):
                    self.dot(pixel(point, offset), z + 1.5, 2.8, junction_color)
                if active and not any(p.boundary or p.virtual for p in active):
                    self.wire_captions.append((wire.name, offset, z, color, tree, active))
            for pin in active:
                pin_keys = frozenset((wire.signals[pin.net],)) if pin.net in wire.signals else frozenset()
                self.wire_line(pixel(pin.tip, offset), pixel(pin.anchor, offset), z + 1, color, 1.45, pin_keys)

        for port in diagram.ports.values():
            at = pixel(port.tip, offset)
            if port.toggle is not None:
                self.marker(port, offset, z + 5)
            else:
                self.dot(at, z + 3, 2.7, PAPER, WIRE)

        for label in diagram.labels:
            if not any(p.boundary or self.wire_visible(p) for p in label.pins):
                continue
            x, y = pixel(label.at, offset)
            direction = "right" if label.right else "left"
            trigger = None
            if label.action is not None:
                trigger = lambda _, name=label.action: self.on_bus(diagram.path, name)
            color = BUS if trigger else (POWER if all(p.toggle is not None for p in label.pins) else INK)
            self.text(label.text, (x, y - 3), z + 4, color, bold=bool(trigger),
                      align=direction, action=trigger, backing=not diagram.collapsed)

        for dev in diagram.devices:
            b = dev.box
            x, y = pixel((b.x, b.y), offset)
            self.blocked_labels.append((z, Rect(x, y, b.w * GRID, b.h * GRID)))
            if dev.child:
                # Show the parent net name beside the child's port name if they differ.
                ports = {pin.tip: pin.net for pin in dev.child.ports.values()}
                for pin in dev.pins:
                    local_tip = pin.tip[0] - b.x, pin.tip[1] - b.y
                    if not self.wire_visible(pin) and pin.net != ports[local_tip]:
                        self.endpoint_label(pin, offset, z + 4)
            else:
                self.draw_device(dev, offset, z + 2)
                for pin in dev.pins:
                    if pin.toggle is None:
                        continue
                    self.marker(pin, offset, z + 5)
                    if not self.wire_visible(pin):
                        self.endpoint_label(pin, offset, z + 4)

    def endpoint_label(self, pin, offset, z):
        x, y = pixel(pin.tip, offset)
        left_of_pin = pin.anchor[0] < pin.tip[0]
        dx, align = (-8, "right") if left_of_pin else (8, "left")
        self.text(pin.net, (x + dx, y - 4), z, POWER, size=11, align=align, backing=True)

    def route_label(self, name, offset, z, color, tree, pins):
        def score(a, b):
            return -(abs(a[0] - b[0]) + abs(a[1] - b[1]))

        lines = sorted(segments(tree), key=lambda pair: score(*pair))
        positions = []
        for a, b in lines:
            x1, y1 = pixel(a, offset)
            x2, y2 = pixel(b, offset)
            if y1 == y2:
                positions.append(((x1 + x2) / 2, y1 - 3, "center"))
            else:
                positions.append((x1 + 4, (y1 + y2) / 2, "left"))
        for pin in pins:
            x, y = pixel(pin.anchor, offset)
            positions.append((x + 4, y - 4, "left"))
        font = QtGui.QFont(FONT)
        font.setPixelSize(10)
        metrics = QtGui.QFontMetricsF(font)
        width = metrics.horizontalAdvance(name) + 4
        for x, y, align in positions:
            box = Rect(x - width / 2 if align == "center" else x, y - metrics.height(), width, metrics.height())
            if any(box.intersects(rect) for rect in self.label_rects):
                continue
            if any(level == z and box.intersects(rect) for level, rect in self.blocked_labels):
                continue
            self.text(name, (x, y), z + 3, color, size=10, align=align, backing=True)
            return
        # Keep the label readable even when all adjacent tracks are occupied.
        if positions:
            x, y, align = positions[0]
            self.text(name, (x, y), z + 3, color, size=10, align=align, backing=True)

    def draw_mos(self, dev, offset, z):
        # Vector coordinates from the PMOS and NMOS stencils and bulk arrows in MOSFET.drawio.
        b = dev.box
        scale = MOS_DS_X / 70

        def at(sx, sy):
            return pixel(dev.mos_point(sx * scale, (sy - 55) * scale), offset)

        def path(points):
            for a, b in zip(points, points[1:]):
                self.line(at(*a), at(*b), z, INK, 1.8)

        path(((41, 35), (41, 75)))
        path(((70, 0), (70, 35), (45, 35), (45, 75), (70, 75), (70, 110)))
        pmos = dev.part.kind == "P"
        path(((0, 55), (30 if pmos else 41, 55)))
        if pmos:
            self.dot(at(35, 55), z, 5 * scale * GRID, PAPER, INK)
        self.line(pixel(dev.mos_point(MOS_DS_X, -b.h / 2), offset), at(70, 0), z, INK, 1.8)
        self.line(at(70, 110), pixel(dev.mos_point(MOS_DS_X, b.h / 2), offset), z, INK, 1.8)
        if dev.rotated:
            x, y = pixel((b.x + b.h, b.y + b.h / 2), offset)
            y -= 8
        else:
            x, y = at(70, 0)
            y += 14
        self.text(dev.part.name, (x + 10, y), z, size=10, bold=True)

        # The attached diagram draws each bulk arrow as an edge over the stencil.
        channel, edge = at(46, 55), at(70, 55)
        self.line(channel, edge, z, INK, 1.8)
        head = 46 if pmos else 70
        tail = head + (9 if pmos else -9)
        arrow = QtGui.QPolygonF([Point(*at(head, 55)), Point(*at(tail, 50)), Point(*at(tail, 60))])
        self.addPolygon(arrow, QtGui.QPen(Qt.NoPen), QtGui.QColor(INK)).setZValue(z + 0.1)
        bulk = pixel(dev.pins[3].tip, offset)
        self.line(edge, bulk, z, INK, 1.8)
        self.text("B", (bulk[0] + 7, bulk[1] + 14), z, size=10)

    def draw_device(self, dev, offset, z):
        part = dev.part
        b = dev.box
        x, y = pixel((b.x + 2, b.y + 2), offset)
        top, bottom = y - 24, y + 24

        def line(a, c):
            self.line(a, c, z, INK, 1.8)

        def polyline(points):
            for a, c in zip(points, points[1:]):
                line(a, c)

        if part.kind in ("P", "N"):
            self.draw_mos(dev, offset, z)
            return
        self.text(part.name, (x + 13, y - 11), z, size=12, bold=True)
        self.text(part.argument, (x + 13, y + 13), z, size=11)
        line((x, top), (x, y - 12))
        line((x, y + 12), (x, bottom))
        if part.kind == "R":
            polyline([(x, y - 12), (x - 5, y - 8), (x + 5, y - 4), (x - 5, y),
                      (x + 5, y + 4), (x - 5, y + 8), (x, y + 12)])
        elif part.kind == "L":
            curve = QtGui.QPainterPath(Point(x, y - 12))
            for k in range(4):
                a = y - 12 + 6 * k
                curve.cubicTo(x + 9, a, x + 9, a + 6, x, a + 6)
            self.addPath(curve, pen(INK, 1.8)).setZValue(z)
        elif part.kind == "C":
            polyline([(x, y - 12), (x, y - 4)])
            line((x - 9, y - 4), (x + 9, y - 4))
            line((x - 9, y + 4), (x + 9, y + 4))
            line((x, y + 4), (x, y + 12))
        elif part.kind == "V":
            self.dot((x, y), z, 12, PAPER, INK)
            for mark_y in (y - 5, y + 5):
                line((x - 3, mark_y), (x + 3, mark_y))
            line((x, y - 8), (x, y - 2))
        else:
            line((x, y - 12), (x, y - 8))
            polyline([(x - 8, y - 8), (x + 8, y - 8), (x, y + 6), (x - 8, y - 8)])
            line((x - 9, y + 6), (x + 9, y + 6))
            line((x, y + 6), (x, y + 12))

    def save_png(self, filename):
        source = self.sceneRect()
        width, height = math.ceil(source.width()), math.ceil(source.height())
        if width * height > 80_000_000:
            raise OSError(f"画像の描画領域が大きすぎます ({width} × {height} px)。階層やバスを格納してください。")
        image = QtGui.QImage(width, height, QtGui.QImage.Format_ARGB32_Premultiplied)
        if image.isNull():
            raise OSError(f"画像のメモリを確保できません ({width} × {height} px)")
        image.fill(QtGui.QColor(PAPER))
        painter = QtGui.QPainter(image)
        try:
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
            self.render(painter, Rect(0, 0, source.width(), source.height()), source)
        finally:
            painter.end()
        file = QtCore.QSaveFile(str(filename))
        if not file.open(QtCore.QIODevice.WriteOnly):
            raise OSError(file.errorString())
        if not image.save(file, "PNG"):
            file.cancelWriting()
            raise OSError("PNGへの書き込みに失敗しました")
        if not file.commit():
            raise OSError(file.errorString())
        return width, height
