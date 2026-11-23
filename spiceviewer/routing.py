"""Grid-based rectilinear routing, with explicit electrical junctions."""

from collections import defaultdict
from dataclasses import dataclass
from heapq import heappop, heappush, nsmallest
from itertools import count


Point = tuple[int, int]
Edge = tuple[Point, Point]
STEPS = ((1, 0), (0, 1), (-1, 0), (0, -1))


def edge(a, b):
    return (a, b) if a < b else (b, a)


def axis(a, b):
    return 0 if a[1] == b[1] else 1


def distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class RoutingError(Exception):
    pass


@dataclass
class WireTree:
    adjacency: dict[Point, set[Point]]

    def visible(self, terminals):
        """Remove leaves that do not lead to a visible terminal."""
        terminals = set(terminals)
        neighbors = {p: set(branches) for p, branches in self.adjacency.items()}
        leaves = [p for p, branches in neighbors.items() if len(branches) < 2 and p not in terminals]
        while leaves:
            point = leaves.pop()
            for neighbor in neighbors.pop(point, ()):
                neighbors[neighbor].remove(point)
                if len(neighbors[neighbor]) < 2 and neighbor not in terminals:
                    leaves.append(neighbor)
        return neighbors


class Router:
    def __init__(self, width, height, blocked, terminals):
        self.width, self.height = width, height
        self.blocked = blocked
        self.terminals = terminals
        self.used = {}

    def route(self, anchors):
        points = list(dict.fromkeys(anchors))
        adjacency = defaultdict(set)
        if not points:
            return WireTree({})
        adjacency[points.pop(0)]
        own = set(anchors)
        while points:
            # Prefer the nearest unused anchor to the tree's bounding box.
            xs, ys = zip(*adjacency)
            limits = min(xs), min(ys), max(xs), max(ys)
            best = min(range(len(points)), key=lambda i: self.bound_distance(points[i], limits))
            point = points.pop(best)
            if point in adjacency:
                continue
            goals = {p for p in adjacency if p not in self.used}
            walk = self.simple_path(point, goals, own) or self.search(point, goals, own, limits)
            if walk is None:
                raise RoutingError(f"配線経路を確保できません ({point})")
            for a, b in zip(walk, walk[1:]):
                adjacency[a].add(b)
                adjacency[b].add(a)

        for point, branches in adjacency.items():
            axes = {axis(point, branch) for branch in branches}
            straight = len(branches) == 2 and len(axes) == 1 and point not in own
            if point in self.used or not straight:
                self.used[point] = None
            else:
                self.used[point] = next(iter(axes))
        return WireTree(dict(adjacency))

    def simple_path(self, start, goals, own):
        """Try the straight and one-bend routes before searching the grid."""
        nearest = nsmallest(4, goals, key=lambda p: (distance(start, p), p))
        x, y = start
        for end in nearest:
            ex, ey = end
            for turn in ((ex, y), (x, ey)):
                walk = [start]
                for target in (turn, end):
                    a, b = walk[-1]
                    sx = (target[0] > a) - (target[0] < a)
                    sy = (target[1] > b) - (target[1] < b)
                    for i in range(1, distance((a, b), target) + 1):
                        point = (a + i * sx, b + i * sy)
                        if point in goals:
                            return walk + [point]
                        if point in self.used or point in self.blocked or (
                            point in self.terminals and point not in own
                        ):
                            break
                        walk.append(point)
                    if walk[-1] != target:
                        break
        return None

    @staticmethod
    def bound_distance(point, limits):
        x, y = point
        left, top, right, bottom = limits
        return max(left - x, 0, x - right) + max(top - y, 0, y - bottom)

    def search(self, start, goals, own, limits):
        serial = count()
        initial = (start, -1)
        queue = [(0, 0, next(serial), initial)]
        costs = {initial: 0}
        previous = {}
        while queue:
            _, cost, _, state = heappop(queue)
            if cost != costs[state]:
                continue
            point, incoming = state
            if point in goals:
                result = [point]
                while state in previous:
                    state = previous[state]
                    result.append(state[0])
                return result
            for index, (dx, dy) in enumerate(STEPS):
                new_axis = index % 2
                if point in self.used and index != incoming:
                    continue
                if incoming >= 0 and index == (incoming + 2) % 4:
                    continue
                target = point[0] + dx, point[1] + dy
                if not (1 <= target[0] < self.width and 1 <= target[1] < self.height):
                    continue
                if target in self.blocked or (target in self.terminals and target not in own):
                    continue
                if target in self.used and self.used[target] != 1 - new_axis:
                    continue
                new_cost = cost + 10 + (8 if incoming >= 0 and incoming != index else 0)
                new_state = target, index
                if new_cost >= costs.get(new_state, float("inf")):
                    continue
                costs[new_state] = new_cost
                previous[new_state] = state
                estimate = self.bound_distance(target, limits) * 10
                heappush(queue, (new_cost + estimate, new_cost, next(serial), new_state))
        return None


def junctions(neighbors, leads):
    degree = {point: len(branches) for point, branches in neighbors.items()}
    for point in leads:
        degree[point] = degree.get(point, 0) + 1
    return [point for point, count in degree.items() if count >= 3]


def segments(neighbors, terminal_points=()):
    """Compress collinear grid edges while retaining terminal and branch vertices."""
    terminals = set(terminal_points)
    cut = {p for p, branches in neighbors.items()
           if p in terminals or len(branches) != 2 or len({axis(p, n) for n in branches}) != 1}
    visited = set()
    for start in sorted(cut):
        for neighbor in sorted(neighbors[start]):
            if edge(start, neighbor) in visited:
                continue
            previous, point = start, neighbor
            visited.add(edge(previous, point))
            while point not in cut:
                following = next(n for n in neighbors[point] if n != previous)
                previous, point = point, following
                visited.add(edge(previous, point))
            yield start, point
