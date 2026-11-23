"""Place symbols and ports independently of visibility, then route the scopes."""

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

from .connectivity import Connectivity, WiringKey
from .netlist import Element
from .placement import align_drain_rows, gate_drain, mos_columns, order_mos_columns
from .routing import Point, Router, RoutingError, WireTree


GRID = 12
MOS_HEIGHT = 6
MOS_DS_X = 3


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Pin:
    net: str
    tip: Point
    anchor: Point
    toggle: WiringKey | None
    boundary: bool = False
    virtual: bool = False


@dataclass(frozen=True)
class Label:
    text: str
    at: Point
    pins: tuple[Pin, ...]
    right: bool = False
    action: str | None = None


@dataclass(frozen=True)
class PortRow:
    bus: str | None
    bits: tuple[str, ...]
    side: str
    y: int


@dataclass
class Device:
    part: Element
    box: Box
    child: "Diagram | None" = None
    pins: tuple[Pin, ...] = ()
    flipped: bool = False
    rotated: bool = False

    def mos_point(self, x, y):
        """Map symbol coordinates measured from the gate tip in grid units."""
        b = self.box
        if not self.rotated:
            return b.x + x, b.y + b.h // 2 + y
        if self.part.kind == "P":
            return b.x + b.h // 2 - y, b.y + x
        return b.x + b.h // 2 + y, b.y + b.h - x


@dataclass
class Wire:
    name: str
    pins: list[Pin]
    tree: WireTree
    bus: bool = False
    signals: dict[str, tuple[tuple[str, ...], str]] = field(default_factory=dict)


@dataclass
class Diagram:
    path: tuple[str, ...]
    title: str
    width: int
    height: int
    ports: dict[str, Pin] = field(default_factory=dict)
    labels: list[Label] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    wires: list[Wire] = field(default_factory=list)
    collapsed: bool = False
    control_lines: tuple[str, ...] = ()


@dataclass
class ViewState:
    instances: set[tuple[str, ...]] = field(default_factory=set)
    buses: set[tuple[tuple[str, ...], str]] = field(default_factory=set)
    hidden_wiring: set[WiringKey] = field(default_factory=set)
    highlights: list[tuple[str, frozenset[tuple[tuple[str, ...], str]]]] = field(default_factory=list)
    highlight_index: int = 0

    def highlight_color(self, keys):
        for color, selected in reversed(self.highlights):
            if not selected.isdisjoint(keys):
                return color
        return None


class Layout:
    def __init__(self, topology: Connectivity, state: ViewState):
        self.topology = topology
        self.state = state

    def build(self):
        # Lay out expanded children before their parents, including deep designs.
        diagrams = {}
        pending = [((), False)]
        while pending:
            path, ready = pending.pop()
            scope = self.topology.scopes[path]
            children = [path + (p.name,) for p in scope.circuit.elements if p.kind == "X"]
            if ready:
                diagrams[path] = self.try_scope(path, diagrams)
            elif path and path not in self.state.instances:
                diagrams[path] = self.collapsed(path)
            else:
                pending.append((path, True))
                pending.extend((child, False) for child in reversed(children))
        diagram = diagrams[()]
        diagram.control_lines = self.topology.control_lines
        return diagram

    def port_rows(self, path):
        scope = self.topology.scopes[path]
        by_bit = {bit: bus for bus in scope.buses for bit in bus.bits}
        seen = set()
        y = {"left": 5, "right": 5}
        result = []
        for port in scope.circuit.ports:
            bus = by_bit.get(port)
            if bus and bus.name in seen:
                continue
            if bus:
                seen.add(bus.name)
            bits = bus.bits if bus else (port,)
            side = "left" if all(self.topology.direction(path, b) == "input" for b in bits) else "right"
            result.append(PortRow(bus.name if bus else None, bits, side, y[side]))
            y[side] += len(bits) * 2 + (2 if bus else 1)
        return result, max(y.values()) + 2

    def port_pins(self, diagram, rows, title_width):
        path = diagram.path
        circuit = self.topology.scopes[path].circuit
        port_names = set(circuit.ports)
        labels, internal = diagram.labels, []
        for row in rows:
            right = row.side == "right"
            edge = diagram.width if right else 0
            inside = -1 if right else 1
            shown_bits = row.bus is None or (path, row.bus) in self.state.buses
            row_pins = []
            for index, bit in enumerate(row.bits):
                y = row.y + index * 2 + (2 if row.bus else 0)
                toggle = self.topology.port_toggle(path, bit)
                anchor = (edge + inside * 2, y)
                if bit in port_names:
                    pin = Pin(bit, (edge, y), anchor, toggle, boundary=True)
                    diagram.ports[bit] = pin
                elif not diagram.collapsed:
                    anchor = (edge + inside * max(3, title_width - 2), y)
                    pin = Pin(bit, anchor, anchor, toggle, virtual=True)
                    internal.append(pin)
                else:
                    continue
                row_pins.append(pin)
                if shown_bits or (pin.boundary and pin.toggle is not None):
                    labels.append(Label(bit, (edge + inside, y), (pin,), right))
            if row.bus:
                action = row.bus if not diagram.collapsed else None
                prefix = ("− " if shown_bits else "+ ") if action is not None else ""
                labels.append(Label(prefix + row.bus, (edge + inside, row.y), tuple(row_pins), right, action))
        return internal

    def collapsed(self, path):
        scope = self.topology.scopes[path]
        rows, height = self.port_rows(path)
        title = f"{path[-1]} · {scope.circuit.name}"
        labels = [len(bit) for row in rows for bit in row.bits]
        width = max(14, len(title) + 4, 2 * max(labels, default=0) + 6)
        result = Diagram(path, title, width, height, collapsed=True)
        self.port_pins(result, rows, 0)
        return result

    def try_scope(self, path, children):
        for gap in (6, 10, 16, 24):
            try:
                return self.scope(path, children, gap)
            except RoutingError as error:
                failure = error
        circuit = self.topology.scopes[path].circuit
        raise RoutingError(f"{circuit.name} / {'/'.join(path)}: {failure}") from failure

    def scope(self, path, children, gap):
        scope = self.topology.scopes[path]
        circuit = scope.circuit
        devices = []
        rows, height = self.port_rows(path)
        middle = [part for part in circuit.elements if part.kind not in ("P", "N")]
        mos = [part for part in circuit.elements if part.kind in ("P", "N")]
        power = {net for net in circuit.nets if self.topology.is_power_or_bulk(path, net)}
        electrical = self.topology.electrical[path]
        paired, mixed, dedicated = mos_columns(circuit, power, electrical)
        columns = [(upper, lower, []) for upper, lower in paired]
        columns.extend(([], [], stack) for stack in mixed + dedicated)
        dedicated_names = {stack[0].part.name for stack in dedicated}
        inputs, outputs = set(), set()
        for port in circuit.ports:
            direction = self.topology.direction(path, port)
            if direction == "input":
                inputs.add(port)
            elif direction == "output":
                outputs.add(port)
        columns = order_mos_columns(columns, inputs, outputs, power)
        label_width = 4 + max((len(n) for n in circuit.nets), default=0)
        column_width = max(8, max((len(p.name) for p in mos), default=0) + 5)
        left = label_width + gap
        pitch = MOS_HEIGHT + gap
        upper_y = gap + 1
        upper_count = max((len(upper) for upper, _, _ in columns), default=0)
        upper_end = upper_y + pitch * upper_count

        def pair_key(position):
            return tuple(electrical[net] for net in gate_drain(position))

        p_keys = {pair_key(position) for upper, _, _ in columns for position in upper}
        n_keys = {pair_key(position) for _, lower, _ in columns for position in lower}
        complementary = p_keys & n_keys

        right = left + len(columns) * (column_width + gap)
        # Keep the space between complementary rows free of other symbols.
        middle_on_right = bool(mixed or complementary or dedicated)
        near = right if middle_on_right else left
        middle_height = 0
        for part in middle:
            child = children.get(path + (part.name,)) if part.kind == "X" else None
            w, h = (child.width, child.height) if child else (max(6, len(part.name) + 5, len(part.argument) + 5), 4)
            devices.append(Device(part, Box(near, upper_end, w, h), child))
            near += w + gap
            middle_height = max(middle_height, h)
        lower_y = upper_end + (middle_height + gap if middle and not middle_on_right else 0)
        lower_count = max((len(lower) for _, lower, _ in columns), default=0)
        lower_end = lower_y + pitch * lower_count
        mixed_end = upper_y + pitch * max((len(stack) for stack in mixed), default=0)
        dedicated_y = upper_end - pitch if upper_count else upper_y
        dedicated_end = dedicated_y + 2 * pitch if dedicated else 0
        y = max(lower_end, mixed_end, dedicated_end, upper_end + middle_height + gap if middle else 0)

        placements = []
        for column, (upper, lower, mixed_stack) in enumerate(columns):
            x = left + column * (column_width + gap)
            if upper:
                end = upper_end - pitch * len(upper)
                paired_row = any(pair_key(position) in complementary for position in upper)
                first = end if paired_row else upper_y
                placements.append((x, (upper, end, first, end)))
            if lower:
                paired_row = any(pair_key(position) in complementary for position in lower)
                end = lower_y if paired_row else lower_end - pitch * len(lower)
                placements.append((x, (lower, lower_y, lower_y, end)))
            if mixed_stack:
                if mixed_stack[0].part.name in dedicated_names:
                    for index, position in enumerate(mixed_stack):
                        box = Box(x, dedicated_y + index * pitch, column_width, MOS_HEIGHT)
                        devices.append(Device(position.part, box, flipped=position.flipped, rotated=True))
                else:
                    end = mixed_end - pitch * len(mixed_stack)
                    placements.append((x, (mixed_stack, upper_y, upper_y, end)))

        def drain_key(position):
            net = position.part.nets[0]
            return None if net in power else self.topology.electrical[path][net]

        starts = align_drain_rows([group for _, group in placements], pitch, drain_key)
        for (x, (stack, _, _, _)), start in zip(placements, starts):
            for index, position in enumerate(stack):
                box = Box(x, start + index * pitch, column_width, MOS_HEIGHT)
                devices.append(Device(position.part, box, flipped=position.flipped))
        far = max(near, right)

        port_bits = {bit for row in rows for bit in row.bits}
        internal_buses = [bus for bus in scope.buses if not port_bits.intersection(bus.bits)]
        row_count = sum(len(bus.bits) * 2 + 4 for bus in internal_buses)
        title = circuit.name if not path else f"{path[-1]} · {circuit.name}"
        height = max(height + row_count + 3, y + 2, 12)
        width = max(far + label_width, len(title) + 8, 20)
        diagram = Diagram(path, title, width, height, devices=devices)
        internal = self.port_pins(diagram, rows, label_width)
        pins = list(diagram.ports.values()) + internal
        for dev in devices:
            dev.pins = self.device_pins(path, dev)
            pins.extend(dev.pins)
        self.internal_bus_labels(diagram, internal_buses, pins, label_width, rows)

        grouped = defaultdict(list)
        compressed = {bit: bus.name for bus in scope.buses if (path, bus.name) not in self.state.buses
                      for bit in bus.bits}
        for pin in pins:
            bus = compressed.get(pin.net)
            grouped[(bus is not None, bus if bus is not None else pin.net)].append(pin)
        blocked = self.blocked_points(diagram, pins)
        router = Router(width, height, blocked, {pin.anchor for pin in pins})
        # Route the most constrained groups first. Nets keep a deterministic order on ties.
        order = sorted(grouped, key=lambda key: -len(grouped[key]))
        for bus, name in order:
            terminals = grouped[bus, name]
            tree = router.route([p.anchor for p in terminals])
            signals = {pin.net: self.topology.electrical[path][pin.net] for pin in terminals if pin.net not in power}
            diagram.wires.append(Wire(name, terminals, tree, bus, signals))
        return diagram

    def internal_bus_labels(self, diagram, buses, pins, label_width, rows):
        path = diagram.path
        available = {"left": set(range(4, diagram.height - 1)), "right": set(range(4, diagram.height - 1))}
        for row in rows:
            available[row.side].difference_update(range(row.y - 1, row.y + 2 * len(row.bits) + 4))
        by_net = defaultdict(list)
        for pin in pins:
            by_net[pin.net].append(pin)
        for bus in buses:
            endpoints = [p for bit in bus.bits for p in by_net[bit]]
            center_x = median(p.anchor[0] for p in endpoints)
            center_y = median(p.anchor[1] for p in endpoints)
            right = center_x >= diagram.width / 2
            bank = available["right" if right else "left"]
            span = 2 * len(bus.bits) + 3
            choices = [i for i in sorted(bank) if all(y in bank for y in range(i, i + span))]
            if not choices:
                # The second bank can take groups when the first is fragmented.
                right = not right
                bank = available["right" if right else "left"]
                choices = [i for i in sorted(bank) if all(y in bank for y in range(i, i + span))]
            if not choices:
                raise RoutingError(f"{bus.name} のラベル領域を確保できません")
            start = min(choices, key=lambda i: abs(i + span / 2 - center_y))
            bank.difference_update(range(start, start + span))
            x = diagram.width - label_width + 2 if right else label_width - 2
            expanded = (path, bus.name) in self.state.buses
            bit_pins = []
            for index, bit in enumerate(bus.bits):
                y = start + 2 + 2 * index if expanded else start + 1
                point = x, y
                status = self.topology.port_toggle(path, bit)
                pin = Pin(bit, point, point, status, virtual=True)
                bit_pins.append(pin)
                pins.append(pin)
                if expanded:
                    diagram.labels.append(Label(bit, (x, y), (pin,), not right))
            diagram.labels.append(Label(("− " if expanded else "+ ") + bus.name, (x, start),
                                        tuple(bit_pins), not right, bus.name))

    def device_pins(self, path, dev):
        part = dev.part
        b = dev.box
        cx, cy = b.x + 2, b.y + 2
        if dev.child:
            ports = self.topology.scopes[dev.child.path].circuit.ports
            child_pins = [dev.child.ports[port] for port in ports]
            points = [(b.x + p.tip[0], b.y + p.tip[1], -1 if p.tip[0] == 0 else 1, 0)
                      for p in child_pins]
        elif part.kind in ("P", "N"):
            half = b.h // 2
            upper, lower = (MOS_DS_X, -half), (MOS_DS_X, half)
            upper_anchor, lower_anchor = (MOS_DS_X, -half - 1), (MOS_DS_X, half + 1)
            drain_is_bottom = (part.kind == "P") != dev.flipped
            vertical = [(upper, upper_anchor), (lower, lower_anchor)]
            if drain_is_bottom:
                vertical.reverse()
            bulk_x = b.h + 1 if dev.rotated else b.w + 1
            pins = [vertical[0], ((0, 0), (-1, 0)), vertical[1],
                    ((MOS_DS_X + 1, 0), (bulk_x, 0))]
            return tuple(Pin(net, dev.mos_point(*tip), dev.mos_point(*anchor),
                             self.topology.device_toggle(path, part, i))
                         for i, (net, (tip, anchor)) in enumerate(zip(part.nets, pins)))
        else:
            points = [(cx, b.y, 0, -1), (cx, b.y + b.h, 0, 1)]
        return tuple(Pin(net, (x, y), (x + dx, y + dy), self.topology.device_toggle(path, part, i))
                     for i, (net, (x, y, dx, dy)) in enumerate(zip(part.nets, points)))

    @staticmethod
    def blocked_points(diagram, pins):
        blocked = set()
        for dev in diagram.devices:
            b = dev.box
            symbol_width = b.h if dev.rotated else b.w
            blocked.update((x, y) for x in range(b.x, b.x + symbol_width + 1)
                           for y in range(b.y, b.y + b.h + 1))
        for pin in pins:
            x, y = pin.tip
            ax, ay = pin.anchor
            blocked.update((u, v) for u in range(min(x, ax), max(x, ax) + 1)
                           for v in range(min(y, ay), max(y, ay) + 1))
        blocked.difference_update(pin.anchor for pin in pins)
        return blocked
