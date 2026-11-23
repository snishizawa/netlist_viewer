"""Order series stacks first, then match PMOS and NMOS columns."""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from itertools import zip_longest
from math import isqrt

from .netlist import Element


@dataclass(frozen=True)
class MosPosition:
    part: Element
    flipped: bool = False


def series_stacks(circuit, power, reserved):
    mos = [part for part in circuit.elements if part.kind in ("P", "N") and part.name not in reserved]
    contacts = defaultdict(list)
    blocked = set(circuit.ports) | power
    for part in circuit.elements:
        if part.kind in ("P", "N") and part.name not in reserved:
            for pin in (0, 2):
                contacts[part.nets[pin]].append((part.name, pin))
            blocked.add(part.nets[1])
        else:
            blocked.update(part.nets)

    links = defaultdict(list)
    for net, endpoints in contacts.items():
        if net in blocked or len(endpoints) != 2:
            continue
        (a, pa), (b, pb) = endpoints
        if a != b:
            links[a].append((b, pa, pb))
            links[b].append((a, pb, pa))

    by_name = {part.name: part for part in mos}
    visited = set()
    stacks = []
    # Begin with ends of chains. Remaining components are closed loops.
    for start in sorted(by_name, key=lambda name: len(links[name]) == 2):
        chain, incoming = [], None
        name = start
        while name not in visited:
            visited.add(name)
            successor = next((link for link in links[name] if link[0] not in visited), None)
            part = by_name[name]
            natural_bottom = 0 if part.kind == "P" else 2
            if incoming is not None:
                flipped = incoming == natural_bottom
            else:
                flipped = successor is not None and successor[1] != natural_bottom
            chain.append(MosPosition(part, flipped))
            if successor is None:
                break
            name, _, incoming = successor
        if chain:
            stacks.append(min((chain, reverse_stack(chain)), key=stack_score))
    return stacks


def reverse_stack(stack):
    return [MosPosition(position.part, not position.flipped) for position in reversed(stack)]


def facing_source_drain(pmos, nmos, power, electrical):
    p_pin = 2 if pmos.flipped else 0
    n_pin = 2 if nmos.flipped else 0
    net = pmos.part.nets[p_pin]
    return p_pin != n_pin and net not in power and electrical[net] == electrical[nmos.part.nets[n_pin]]


def join_source_drain(upper, lower, power, electrical):
    for p_stack in (upper, reverse_stack(upper)):
        for n_stack in (lower, reverse_stack(lower)):
            if facing_source_drain(p_stack[-1], n_stack[0], power, electrical):
                return p_stack, n_stack
    return None


def match_source_drain_columns(upper, lower, power, electrical):
    """Match remaining MOSFETs with a shared cross-polarity source/drain net."""
    candidates = []
    for p_stack in upper:
        options = {}
        for j, n_stack in enumerate(lower):
            joined = join_source_drain(p_stack, n_stack, power, electrical)
            if joined:
                options[j] = joined
        candidates.append(options)

    partner = {}
    owner = {}
    for start, options in enumerate(candidates):
        if not options:
            continue
        # Seek a free NMOS stack. A previous join can move to another valid
        # partner, but no gate/drain match can take a reserved stack.
        pending = deque((start,))
        previous = {}
        visited = {start}
        free = None
        while pending and free is None:
            i = pending.popleft()
            for j in candidates[i]:
                if j in previous:
                    continue
                previous[j] = i
                if j not in owner:
                    free = j
                    break
                if owner[j] not in visited:
                    visited.add(owner[j])
                    pending.append(owner[j])
        while free is not None:
            i = previous[free]
            old = partner.get(i)
            partner[i] = free
            owner[free] = i
            free = old

    columns = [candidates[i][partner[i]] for i in sorted(partner)]
    return columns


def stack_score(stack):
    # Prefer fewer NMOS above PMOS, then the natural D/S orientation.
    nmos = inversions = 0
    for position in stack:
        if position.part.kind == "N":
            nmos += 1
        else:
            inversions += nmos
    return inversions, sum(position.flipped for position in stack)


def stack_signals(stack, power):
    gates = {p.part.nets[1] for p in stack}
    signals = {net for p in stack for net in p.part.nets[:3]}
    return gates - power, signals - power


def gate_drain(position):
    drain, gate = position.part.nets[:2]
    return gate, drain


def transmission_gate_columns(p_devices, n_devices, electrical):
    """Match identical drain/source nets in terminal order, without flipping."""
    def terminal_pair(position):
        drain, _, source, _ = position.part.nets
        return electrical[drain], electrical[source]

    available = defaultdict(deque)
    for position in n_devices:
        drain, source = terminal_pair(position)
        if drain != source:
            available[drain, source].append(position)

    pairs = []
    for position in p_devices:
        matches = available.get(terminal_pair(position))
        if matches:
            pairs.append(([position], [matches.popleft()]))
    return pairs


def mos_columns(circuit, power, electrical):
    p_devices = [MosPosition(part) for part in circuit.elements if part.kind == "P"]
    n_devices = [MosPosition(part) for part in circuit.elements if part.kind == "N"]
    dedicated = transmission_gate_columns(p_devices, n_devices, electrical)
    reserved = {position.part.name for pair in dedicated for stack in pair for position in stack}
    series_pairs = match_source_drain_columns(
        [[position] for position in p_devices if position.part.name not in reserved],
        [[position] for position in n_devices if position.part.name not in reserved],
        power, electrical,
    )
    reserved.update(position.part.name for pair in series_pairs for stack in pair for position in stack)
    stacks = series_stacks(circuit, power, reserved)
    limit = isqrt(len(p_devices) + len(n_devices))
    upper, lower, mixed = [], [], []
    for stack in stacks:
        for start in range(0, len(stack), limit):
            chunk = stack[start:start + limit]
            kinds = {position.part.kind for position in chunk}
            if kinds == {"P"}:
                upper.append(chunk)
            elif kinds == {"N"}:
                lower.append(chunk)
            else:
                mixed.append(chunk)

    upper.sort(key=len, reverse=True)
    lower.sort(key=len, reverse=True)
    columns = []
    available = set(range(len(lower)))

    # Gate/drain matches only use stacks left after the series pairing.
    remaining_upper = []
    for stack in upper:
        pairs = {gate_drain(position) for position in stack}
        match = max(
            (i for i in available if len(stack) + len(lower[i]) <= limit),
            default=None,
            key=lambda i: (sum(gate_drain(position) in pairs for position in lower[i]), -i),
        )
        if match is None or not any(gate_drain(position) in pairs for position in lower[match]):
            remaining_upper.append(stack)
            continue
        columns.append((stack, lower[match]))
        available.remove(match)

    remaining_lower = [stack for i, stack in enumerate(lower) if i in available]
    for pair in zip_longest(remaining_upper, remaining_lower):
        for side, stack in enumerate(pair):
            if stack is None:
                continue
            gates, signals = stack_signals(stack, power)
            drains = {position.part.nets[0] for position in stack} - power
            pairs = {gate_drain(position) for position in stack}
            best = (0, 0, 0)
            target = None
            for column in columns:
                opposite = column[1 - side]
                if not opposite or len(column[0]) + len(column[1]) + len(stack) > limit:
                    continue
                # Spread parallel drains across columns to keep them at similar heights.
                if any(position.part.nets[0] in drains for position in column[side]):
                    continue
                other_gates, other_signals = stack_signals(opposite, power)
                exact = sum(gate_drain(position) in pairs for position in opposite)
                score = exact, len(gates & other_gates), len(signals & other_signals)
                if score > best:
                    best, target = score, column
            if target is None:
                target = ([], [])
                columns.append(target)
            opposite = target[1 - side]
            if side == 0 and opposite and target[0] and gate_drain(target[0][-1]) == gate_drain(opposite[0]):
                target[0][:0] = stack
            elif side == 1 and opposite and gate_drain(stack[0]) == gate_drain(opposite[-1]):
                target[1][:0] = stack
            else:
                target[side].extend(stack)
    # Dedicated columns are stored in screen order from top to bottom.
    dedicated_columns = [p_stack + n_stack for p_stack, n_stack in dedicated]
    dedicated_columns.extend(n_stack + p_stack for p_stack, n_stack in series_pairs)
    return columns, mixed, dedicated_columns


def order_mos_columns(columns, inputs, outputs, power):
    """Keep port affinity first, then shorten the horizontal spans of signal nets."""
    nets = []
    sides = []
    for column in columns:
        parts = [position.part for stack in column for position in stack]
        signals = {net for part in parts for net in part.nets[:3]} - power
        nets.append(signals)
        input_gate = any(part.nets[1] in inputs for part in parts)
        output_drain = any(part.nets[0] in outputs for part in parts)
        sides.append(int(output_drain) - int(input_gate))

    order = sorted(range(len(columns)), key=sides.__getitem__)
    if len(order) < 2:
        return [columns[index] for index in order]
    anchors = {net: -1 for net in inputs}
    anchors.update(dict.fromkeys(outputs, len(columns)))

    def spans(indices):
        bounds = {net: (x, x) for net, x in anchors.items()}
        for x, index in enumerate(indices):
            for net in nets[index]:
                left, right = bounds.get(net, (x, x))
                bounds[net] = min(left, x), max(right, x)
        return bounds

    bounds = spans(order)
    best_order = order
    best_span = sum(right - left for left, right in bounds.values())
    # Fixed sweeps through the pins. Keep the shortest candidate, including the
    # initial order, since a barycenter sweep can increase the total span.
    for _ in range(4):
        def barycenter(index):
            signals = nets[index]
            if not signals:
                return len(columns) / 2
            return sum(sum(bounds[net]) for net in signals) / (2 * len(signals))

        candidate = sorted(order, key=lambda index: (sides[index], barycenter(index)))
        if candidate == order:
            break
        order = candidate
        bounds = spans(order)
        length = sum(right - left for left, right in bounds.values())
        if length < best_span:
            best_order, best_span = order, length
    return [columns[index] for index in best_order]


def align_drain_rows(groups, pitch, drain_key):
    """Shift whole groups within their row bands using shared drain positions."""
    starts = [start for _, start, _, _ in groups]
    members = [[(drain_key(position), index) for index, position in enumerate(stack)]
               for stack, _, _, _ in groups]
    counts = Counter(key for group in members for key, _ in group if key is not None)
    fixed_sum, fixed_count = Counter(), Counter()
    row_sum, row_count = Counter(), Counter()
    for (_, start, first, last), group in zip(groups, members):
        for key, index in group:
            if counts[key] > 1:
                y = start + index * pitch
                row_sum[key] += y
                row_count[key] += 1
                if first == last:
                    fixed_sum[key] += y
                    fixed_count[key] += 1

    for _ in range(3):
        changed = False
        for i, ((_, _, first, last), group) in enumerate(zip(groups, members)):
            shared = [(key, index) for key, index in group if counts[key] > 1]
            if first == last or not shared:
                continue
            current = starts[i]
            for key, index in shared:
                row_sum[key] -= current + index * pitch
                row_count[key] -= 1

            targets = []
            for key, index in shared:
                if fixed_count[key]:
                    center = fixed_sum[key] / fixed_count[key]
                elif row_count[key]:
                    center = row_sum[key] / row_count[key]
                else:
                    continue
                targets.append(center - index * pitch)
            if targets:
                center = sum(targets) / len(targets)
                slot = round((center - first) / pitch)
                starts[i] = max(first, min(last, first + slot * pitch))

            changed |= starts[i] != current
            for key, index in shared:
                row_sum[key] += starts[i] + index * pitch
                row_count[key] += 1
        if not changed:
            break
    return starts
