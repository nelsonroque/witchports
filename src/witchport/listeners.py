from __future__ import annotations

import ast
import os
import re
import shlex
import signal
import stat
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


class ListenerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PortRange:
    first: int = 1
    last: int = 65535

    @classmethod
    def parse(cls, value: str) -> PortRange:
        match = re.fullmatch(r"([0-9]{1,5})(?:\s*-\s*([0-9]{1,5}))?", value.strip())
        if match:
            first = int(match[1])
            last = int(match[2] or match[1])
            if 1 <= first <= last <= 65535:
                return cls(first, last)
        raise ValueError(
            "Enter a port or an ascending range from 1 to 65535 (e.g. 3000-9000)."
        )

    def __contains__(self, port: int) -> bool:
        return self.first <= port <= self.last


@dataclass(frozen=True)
class Process:
    parent: int
    started: str
    command: str


@dataclass(frozen=True)
class Listener:
    port: int
    pid: int
    addresses: tuple[str, ...]
    name: str
    command: str
    framework: str | None
    started: str | None

    @property
    def label(self) -> str:
        return self.framework or self.name


# Match launch commands and process titles. Port numbers alone identify nothing.
FRAMEWORKS = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        (
            "FastAPI",
            r"(?:^|[ /])fastapi(?:\.exe)?\s+(?:dev|run)\b|(?:^|\s)-m\s+fastapi\b",
        ),
        (
            "Next.js",
            r"^next-server(?:\s|$)|(?:^|[ /])next(?:/dist/bin/next)?\s+(?:dev|start)\b",
        ),
        (
            "Django",
            r"(?:^|[ /])(?:manage\.py|django-admin(?:\.py)?|django)\s+runserver\b",
        ),
        ("Flask", r"(?:^|[ /])flask\s+(?:(?![;&|])\S+\s+)*run\b"),
        ("Nuxt", r"(?:^|[ /])(?:nuxt|nuxi)(?:\.mjs)?\s+(?:dev|start|preview)\b"),
        ("Astro", r"(?:^|[ /])astro(?:\.mjs)?(?:\s+(?:dev|preview)\b|$)"),
        ("Angular", r"(?:^|[ /])ng(?:\.js)?\s+serve\b"),
        (
            "Create React App",
            r"(?:^|[ /])react-scripts(?:/scripts/start\.js|\s+start)\b",
        ),
        ("Vite", r"(?:^|[ /])vite(?:\.js|/bin/vite\.js)?(?:\s|$)"),
        ("Remix", r"(?:^|[ /])remix(?:-serve)?\s+(?:vite:dev|dev|\S+\.js)\b"),
        ("Rails", r"(?:^|[ /])rails\s+(?:server|s)\b"),
        ("Laravel", r"(?:^|[ /])artisan\s+serve\b"),
        ("Phoenix", r"(?:^|[ /])(?:mix\s+)?phx\.server\b"),
        ("Spring Boot", r"org\.springframework\.boot\.loader\.|spring-boot:run\b"),
        ("Streamlit", r"(?:^|[ /])streamlit\s+run\b"),
        ("Uvicorn (ASGI)", r"(?:^|[ /])uvicorn(?:\s|$)"),
        ("Hypercorn (ASGI)", r"(?:^|[ /])hypercorn(?:\s|$)"),
        ("Daphne (ASGI)", r"(?:^|[ /])daphne(?:\s|$)"),
        ("Gunicorn", r"(?:^|[ /])gunicorn(?:\s|:|$)"),
        ("Puma", r"(?:^|[ /])puma(?:\s|$)"),
        ("Python HTTP server", r"(?:^|\s)-m\s+http\.server\b"),
    )
)

TERMINAL_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def display(value: str) -> str:
    return TERMINAL_CONTROL.sub(" ", value)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=8,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError as error:
        raise ListenerError(
            f"{args[0]} is required and was not found on PATH."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ListenerError(f"{args[0]} timed out.") from error
    except OSError as error:
        raise ListenerError(f"Could not run {args[0]}: {error}") from error


def _processes() -> dict[int, Process]:
    result = _run(
        "ps", "-axww", "-o", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "command="
    )
    if result.returncode:
        raise ListenerError(
            display(result.stderr.strip()) or "Could not read the process table."
        )
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=7)
        if len(fields) == 8 and fields[0].isdigit() and fields[1].isdigit():
            pid, parent, *start, command = fields
            processes[int(pid)] = Process(int(parent), " ".join(start), command)
    return processes


def _sockets(
    ports: PortRange,
) -> tuple[dict[int, str], dict[tuple[int, int], set[str]]]:
    selector = "-iTCP"
    if ports != PortRange():
        selector += f":{ports.first}"
        if ports.last != ports.first:
            selector += f"-{ports.last}"
    result = _run("lsof", "-nP", "-a", selector, "-sTCP:LISTEN", "-F0pcfn")
    # lsof exits 1 on an empty selection and can also return partial results.
    if result.returncode not in (0, 1) or (
        result.returncode == 1 and result.stderr.strip() and not result.stdout
    ):
        raise ListenerError(
            display(result.stderr.strip()) or "Could not read local TCP listeners."
        )

    commands = {}
    addresses = defaultdict(set)
    pid = None
    for raw in result.stdout.split("\0"):
        field = raw.lstrip("\n")
        tag, value = field[:1], field[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
        elif tag == "c" and pid is not None:
            commands[pid] = value
        elif tag == "n" and pid is not None:
            address, separator, number = value.rpartition(":")
            if separator and number.isdigit() and int(number) in ports:
                addresses[int(number), pid].add(address)
    return commands, addresses


def _uvicorn_serves_fastapi(pid: int, command: str) -> bool:
    """Recognize a direct FastAPI instance in a local ASGI entry point."""
    try:
        args = shlex.split(command)
        module, _, app = next(
            arg.partition(":")
            for arg in args
            if re.fullmatch(r"\w+(?:\.\w+)*:\w+", arg)
        )
    except (ValueError, StopIteration):
        return False

    app_dir = "."
    for index, arg in enumerate(args):
        if arg.startswith("--app-dir="):
            app_dir = arg.partition("=")[2]
        elif arg == "--app-dir" and index + 1 < len(args):
            app_dir = args[index + 1]

    try:
        cwd = Path(app_dir)
        if not cwd.is_absolute():
            result = _run("lsof", "-nP", "-a", "-p", str(pid), "-d", "cwd", "-F0pn")
            directory = next(
                field[1:]
                for field in result.stdout.split("\0")
                if field.startswith("n/")
            )
            cwd = Path(directory) / cwd
        entry = cwd.joinpath(*module.split("."))
        package = entry / "__init__.py"
        source = package if package.is_file() else entry.with_suffix(".py")
        if not source.is_file():
            return False
        maximum = 256 * 1024
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                return False
            data = file.read(maximum + 1)
        if len(data) > maximum:
            return False
        tree = ast.parse(data)
    except (
        ListenerError,
        OSError,
        StopIteration,
        SyntaxError,
        ValueError,
        RecursionError,
    ):
        return False

    constructors = set()
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "fastapi"
            and node.level == 0
        ):
            constructors.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "FastAPI"
            )
        elif isinstance(node, ast.Import):
            constructors.update(
                f"{alias.asname or alias.name}.FastAPI"
                for alias in node.names
                if alias.name == "fastapi"
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, ast.Call
        ):
            target = node.targets if isinstance(node, ast.Assign) else [node.target]
            function = node.value.func
            if any(isinstance(name, ast.Name) and name.id == app for name in target):
                if isinstance(function, ast.Name) and function.id in constructors:
                    return True
                if (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and f"{function.value.id}.{function.attr}" in constructors
                ):
                    return True
    return False


def _framework(
    pid: int,
    processes: dict[int, Process],
    fallback: str,
    fastapi_checks: dict[int, bool],
) -> str | None:
    generic = None
    # Reloaders and worker processes often inherit the socket from a launcher.
    for _ in range(5):
        process = processes.get(pid)
        command = process.command if process else fallback
        # Shell wrappers can contain commands unrelated to the actual listener.
        executable = (
            command.split(maxsplit=1)[0].rsplit("/", 1)[-1] if command.strip() else ""
        )
        if executable in {"bash", "sh", "zsh", "fish", "dash", "osascript"}:
            break
        label = next(
            (name for name, pattern in FRAMEWORKS if pattern.search(command)), None
        )
        if label == "Uvicorn (ASGI)" and process:
            if pid not in fastapi_checks:
                fastapi_checks[pid] = _uvicorn_serves_fastapi(pid, command)
            if fastapi_checks[pid]:
                return "FastAPI"
        if label:
            if label not in {
                "Uvicorn (ASGI)",
                "Hypercorn (ASGI)",
                "Daphne (ASGI)",
                "Gunicorn",
            }:
                return label
            generic = generic or label
        if not process or process.parent <= 1:
            break
        pid = process.parent
    return generic


def find_listeners(ports: PortRange = PortRange()) -> list[Listener]:
    commands, addresses = _sockets(ports)
    if not addresses:
        return []
    processes = _processes()
    fastapi_checks: dict[int, bool] = {}
    listeners = []
    for (port, pid), bindings in sorted(addresses.items()):
        process = processes.get(pid)
        name = commands.get(pid, "unknown")
        listeners.append(
            Listener(
                port,
                pid,
                tuple(sorted(bindings)),
                name,
                process.command if process else name,
                _framework(pid, processes, name, fastapi_checks),
                process.started if process else None,
            )
        )
    return listeners


def stop_listener(listener: Listener, force: bool = False) -> bool:
    """Signal the selected owner and report whether it stopped listening."""
    if listener.pid <= 1 or not listener.started:
        raise ListenerError("The process identity could not be verified for stopping.")

    ports = PortRange(listener.port, listener.port)
    _, bindings = _sockets(ports)
    processes = _processes()
    current = processes.get(listener.pid)
    if (
        (listener.port, listener.pid) not in bindings
        or current is None
        or current.started != listener.started
    ):
        raise ListenerError(
            f"PID {listener.pid} no longer matches the listener on port {listener.port}. Scan again."
        )

    ancestor = os.getpid()
    while ancestor > 1:
        if listener.pid == ancestor:
            raise ListenerError("Cannot stop Witchport or a process that launched it.")
        process = processes.get(ancestor)
        if process is None or process.parent == ancestor:
            break
        ancestor = process.parent

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(listener.pid, sig)
    except ProcessLookupError as error:
        raise ListenerError(
            f"PID {listener.pid} exited before it could be signaled."
        ) from error
    except PermissionError as error:
        raise ListenerError(f"Permission denied for PID {listener.pid}.") from error

    deadline = time.monotonic() + 2
    while True:
        _, bindings = _sockets(ports)
        if (listener.port, listener.pid) not in bindings:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)
