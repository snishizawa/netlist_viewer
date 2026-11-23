"""Hierarchy, terminal roles and local bus membership."""

from collections import defaultdict
from dataclasses import dataclass
import re

from .netlist import Circuit, Element, Netlist


POWER_NAMES = frozenset(("VDD", "!VDD", "VSS", "GND"))
MOS_ROLES = ("drain", "gate", "source", "bulk")
BUS_BIT = re.compile(r"(.+)(?:\[([0-9]+)\]|<([0-9]+)>)")


@dataclass(frozen=True)
class Bus:
    name: str
    bits: tuple[str, ...]


def find_buses(nets):
    groups = defaultdict(list)
    for net in nets:
        match = BUS_BIT.fullmatch(net)
        if match:
            name, square, angle = match.groups()
            # Leading zeros do not affect the number. Comparing length and digits
            # also supports indices wider than Python's integer conversion limit.
            digits = (square or angle).lstrip("0")
            groups[name].append((len(digits), digits, net))
    return tuple(Bus(name, tuple(bit for _, _, bit in sorted(bits)))
                 for name, bits in groups.items() if len(bits) >= 2)


@dataclass(frozen=True)
class WiringKey:
    net: tuple[tuple[str, ...], str]
    bulk_only: bool = False


@dataclass
class Scope:
    circuit: Circuit
    local_roles: dict[str, frozenset[str]]
    buses: tuple[Bus, ...]


class Connectivity:
    def __init__(self, netlist: Netlist, top: Circuit):
        self.control_lines = netlist.control_lines
        definitions = {}
        for circuit in (*netlist.dependency_order, netlist.root):
            roles = {net: set() for net in circuit.nets}
            for part in circuit.elements:
                if part.kind == "X":
                    child = definitions[id(netlist.target(part))]
                    for net, port in zip(part.nets, child.circuit.ports):
                        roles[net].update(child.local_roles[port])
                else:
                    terminal_roles = MOS_ROLES if part.kind in ("P", "N") else ("passive", "passive")
                    for net, role in zip(part.nets, terminal_roles):
                        roles[net].add(role)
            definitions[id(circuit)] = Scope(circuit, {n: frozenset(r) for n, r in roles.items()},
                                             find_buses(circuit.nets))

        self.scopes = {}
        self.electrical = {}
        self.roles = {}
        self.power = set()
        pending = [((), top, {})]
        while pending:
            path, circuit, parent_ports = pending.pop()
            scope = self.scopes[path] = definitions[id(circuit)]
            local = self.electrical[path] = {net: parent_ports.get(net, (path, net)) for net in scope.local_roles}
            for net, key in local.items():
                if net in POWER_NAMES:
                    self.power.add(key)
            for part in circuit.elements:
                if part.kind == "X":
                    child = netlist.target(part)
                    ports = {port: local[net] for port, net in zip(child.ports, part.nets)}
                    pending.append((path + (part.name,), child, ports))
                else:
                    terminal_roles = MOS_ROLES if part.kind in ("P", "N") else ("passive", "passive")
                    for net, role in zip(part.nets, terminal_roles):
                        self.roles.setdefault(local[net], set()).add(role)

    def is_power_or_bulk(self, path, net):
        key = self.electrical[path][net]
        return key in self.power or self.roles.get(key) == {"bulk"}

    def direction(self, path, net):
        if self.is_power_or_bulk(path, net):
            return None
        core = self.scopes[path].local_roles[net].intersection(("gate", "drain"))
        if core == {"gate"}:
            return "input"
        if core == {"drain"}:
            return "output"
        return "unknown"

    def port_toggle(self, path, net):
        key = self.electrical[path][net]
        if self.is_power_or_bulk(path, net):
            return WiringKey(key)
        # A child boundary serving only bulk pins is a branch of a shared net.
        if path and self.scopes[path].local_roles[net] == {"bulk"}:
            return WiringKey(key, bulk_only=True)
        return None

    def device_toggle(self, path, part: Element, pin):
        if part.kind == "X":
            child_path = path + (part.name,)
            port = self.scopes[child_path].circuit.ports[pin]
            return self.port_toggle(child_path, port)
        net = part.nets[pin]
        key = self.electrical[path][net]
        if self.is_power_or_bulk(path, net):
            return WiringKey(key)
        if part.kind in ("P", "N") and pin == 3:
            return WiringKey(key, bulk_only=True)
        return None
