"""Read and validate the viewer's supported subset of SPICE and CDL."""

from dataclasses import dataclass, field, replace
from pathlib import Path
import re


@dataclass(frozen=True)
class Source:
    file: Path
    line: int

    def __str__(self):
        return f"{self.file}:{self.line}"


class NetlistError(Exception):
    def __init__(self, source: Source, reason: str):
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


@dataclass(frozen=True)
class Element:
    name: str
    kind: str
    nets: tuple[str, ...]
    argument: str
    parameters: tuple[str, ...]
    source: Source
    explicit_subcircuit: bool = False


@dataclass
class Circuit:
    name: str
    ports: tuple[str, ...]
    source: Source
    elements: list[Element] = field(default_factory=list)
    _element_names: set[str] = field(default_factory=set, init=False, repr=False)

    def add(self, element: Element):
        key = element.name.casefold()
        if key in self._element_names:
            raise NetlistError(element.source, f"素子名 {element.name} が重複しています")
        self._element_names.add(key)
        self.elements.append(element)

    @property
    def nets(self):
        names = dict.fromkeys(self.ports)
        for part in self.elements:
            names.update(dict.fromkeys(part.nets))
        return tuple(names)


@dataclass
class Netlist:
    root: Circuit
    definitions: dict[str, Circuit]
    candidates: tuple[Circuit, ...]
    dependency_order: tuple[Circuit, ...]
    control_lines: tuple[str, ...] = ()

    def target(self, element: Element) -> Circuit:
        return self.definitions[element.argument.casefold()]


@dataclass
class Statement:
    source: Source
    parts: list[tuple[str, Source]]
    raw_lines: list[str]

    def fields(self):
        return [(word, source) for text, source in self.parts for word in text.split()]

    @property
    def text(self):
        return " ".join(text for text, _ in self.parts)


IGNORED_DIRECTIVE = re.compile(r"\.(?:model|param)(?=\s|[(]|$)", re.IGNORECASE)


def statements(path, included_at):
    pending = None
    skipping = False
    try:
        with path.open("rb") as stream:
            for number, raw in enumerate(stream, 1):
                source = Source(path, number)
                try:
                    original = raw.decode("utf-8-sig" if number == 1 else "utf-8").rstrip("\r\n")
                    text = original.strip()
                except UnicodeDecodeError as error:
                    raise NetlistError(source, "UTF-8 として読み込めません") from error
                if not text or text.startswith("*"):
                    continue
                if text.startswith("+"):
                    if skipping:
                        continue
                    if pending is None:
                        raise NetlistError(source, "継続する行がありません")
                    pending.parts.append((text[1:].strip(), source))
                    pending.raw_lines.append(original)
                    continue
                if pending is not None:
                    yield pending
                # A concatenated include can contain a BOM before an internal line.
                skipping = bool(IGNORED_DIRECTIVE.match(text.lstrip("\ufeff").lstrip()))
                pending = None if skipping else Statement(source, [(text, source)], [original])
    except OSError as error:
        raise NetlistError(included_at, f"{path} を開けません ({error.strerror})") from error
    if pending is not None:
        yield pending


INCLUDE_PATH = re.compile(r"""(?:"([^"]+)"|'([^']+)'|([^\s"']+))""")
PARAMETER = re.compile(r"[A-Za-z_][\w.]*=\S+")
POSITIONAL = re.compile(r"""[^\s=/;'"]+""")


def include_statements(path):
    """Expand includes in place. Track only files on the current include path."""
    opened = set()
    stack = []

    def push(filename, source):
        try:
            identity = filename.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise NetlistError(source, f"{filename} のパスを解決できません ({error})") from error
        if identity in opened:
            chain = " → ".join(str(frame[2]) for frame in stack)
            raise NetlistError(source, f".INCLUDE が循環しています ({chain} → {filename})")
        opened.add(identity)
        stack.append((identity, iter(statements(filename, source)), filename))

    push(path, Source(path, 1))
    while stack:
        identity, stream, _ = stack[-1]
        entry = next(stream, None)
        if entry is None:
            stack.pop()
            opened.remove(identity)
            continue

        first, *rest = entry.text.split(maxsplit=1)
        directive = first.upper()
        if directive in (".INC", ".INCLUDE"):
            match = INCLUDE_PATH.fullmatch(rest[0]) if rest else None
            if match is None:
                raise NetlistError(entry.source, ".INCLUDE にはファイル名を1個指定してください")
            filename = next(group for group in match.groups() if group is not None)
            push(entry.source.file.parent / filename, entry.source)
        else:
            yield entry


def check_positions(fields):
    for token, source in fields:
        if not POSITIONAL.fullmatch(token):
            raise NetlistError(source, f"未対応の記述です ({token})")


def before_assignments(fields):
    end = next((i for i, (token, _) in enumerate(fields) if "=" in token), len(fields))
    if 0 < end < len(fields) and fields[end][0].startswith("="):
        end -= 1
    positional = fields[:end]
    if positional and positional[-1][0].casefold() == "params:":
        positional.pop()
    return positional


def parse_element(entry):
    fields = entry.fields()
    name = fields[0][0]
    kind = name[0].upper()
    if kind not in ("M", "X", "R", "L", "C", "D", "V"):
        raise NetlistError(entry.source, f"未対応の素子です ({name})")

    arguments = fields[1:]
    if kind == "V":
        if len(arguments) < 3:
            raise NetlistError(entry.source, f"{name} には2端子と電圧指定が必要です")
        check_positions(fields[:3])
        nodes = tuple(word for word, _ in arguments[:2])
        value = " ".join(word for word, _ in arguments[2:])
        return Element(name, kind, nodes, value, (), entry.source)
    if kind in ("M", "X"):
        positional = before_assignments(arguments)
        parameter_fields = []
    else:
        split = next((i for i, (token, _) in enumerate(arguments) if "=" in token), len(arguments))
        positional, parameter_fields = arguments[:split], arguments[split:]
        for parameter, source in parameter_fields:
            if not PARAMETER.fullmatch(parameter):
                raise NetlistError(source, f"未対応のパラメータです ({parameter})")

    explicit_subcircuit = False
    if kind == "X":
        explicit_subcircuit = len(positional) >= 2 and positional[-2][0] == "/"
        if explicit_subcircuit:
            positional.pop(-2)
        if not positional:
            raise NetlistError(entry.source, f"{name} のサブ回路参照名がありません")
    else:
        count = 4 if kind == "M" else 2
        if len(positional) != count + 1:
            raise NetlistError(entry.source, f"{name} には {count} 端子と値またはモデル名が必要です")

    check_positions(fields[:1] + positional)
    model, model_source = positional[-1]
    polarity = model[0].upper()
    if kind == "M":
        if polarity not in ("N", "P"):
            raise NetlistError(model_source, f"{name} のMOSFET極性を判定できません (モデル名: {model})")
        kind = polarity
    return Element(name, kind, tuple(word for word, _ in positional[:-1]), model,
                   tuple(word for word, _ in parameter_fields), entry.source, explicit_subcircuit)


def dependency_order(definitions):
    """Return children before parents and report even unreachable recursion."""
    done = set()
    result = []
    for start in definitions:
        if start in done:
            continue
        active = {start}
        stack = [(start, iter(part for part in definitions[start].elements if part.kind == "X"))]
        while stack:
            key, children = stack[-1]
            part = next(children, None)
            if part is None:
                stack.pop()
                active.remove(key)
                done.add(key)
                result.append(definitions[key])
                continue
            target = part.argument.casefold()
            if target in active:
                chain = " → ".join(definitions[node].name for node, _ in stack)
                raise NetlistError(part.source, f"サブ回路が再帰しています ({chain} → {part.argument})")
            if target not in done:
                active.add(target)
                stack.append((target, iter(p for p in definitions[target].elements if p.kind == "X")))
    return tuple(result)


def read_netlist(filename):
    path = Path(filename).absolute()
    root = Circuit(path.stem, (), Source(path, 1))
    definitions = {}
    current = root
    control_lines = []
    for entry in include_statements(path):
        head = entry.text.split(maxsplit=1)[0]
        directive = head.upper()
        if not head.startswith("."):
            current.add(parse_element(entry))
            continue
        control_lines.extend(entry.raw_lines)
        if directive == ".SUBCKT":
            fields = before_assignments(entry.fields()[1:])
            if current is not root or not fields:
                raise NetlistError(entry.source, ".SUBCKT の位置または宣言が不正です")
            check_positions(fields)
            name, *ports = (word for word, _ in fields)
            key = name.casefold()
            if key in definitions:
                raise NetlistError(entry.source, f".SUBCKT {name} が重複しています ({definitions[key].source})")
            if len(set(ports)) != len(ports):
                raise NetlistError(entry.source, f".SUBCKT {name} の端子名が重複しています")
            current = definitions[key] = Circuit(name, tuple(ports), entry.source)
        elif directive == ".ENDS":
            words = entry.text.split()
            if current is root or len(words) > 2:
                raise NetlistError(entry.source, ".ENDS に対応する .SUBCKT がありません")
            current = root
    if current is not root:
        raise NetlistError(current.source, f".SUBCKT {current.name} の .ENDS がありません")

    called = set()
    for circuit in (root, *definitions.values()):
        for index, part in enumerate(circuit.elements):
            if part.kind != "X":
                continue
            key = part.argument.casefold()
            if key not in definitions:
                polarity = part.argument[0].upper()
                if not part.explicit_subcircuit and len(part.nets) == 4 and polarity in ("P", "N"):
                    circuit.elements[index] = replace(part, kind=polarity)
                    continue
                raise NetlistError(part.source, f"未定義のサブ回路参照です ({part.argument})")
            target = definitions[key]
            if len(part.nets) != len(target.ports):
                raise NetlistError(part.source, f"{part.name} の端子数が一致しません ({len(part.nets)} / {len(target.ports)})")
            called.add(key)

    order = dependency_order(definitions)
    candidates = (root,) if root.elements else tuple(c for name, c in definitions.items() if name not in called)
    if not candidates:
        raise NetlistError(root.source, "表示できる回路がありません")
    return Netlist(root, definitions, candidates, order, tuple(control_lines))
