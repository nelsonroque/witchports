from __future__ import annotations

import asyncio
from typing import ClassVar

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Static

from .listeners import (
    Listener,
    ListenerError,
    PortRange,
    display,
    find_listeners,
    stop_listener,
)

# These shades are in the xterm 256-color palette used by many terminals.
WITCHPORTS_DARK = Theme(
    name="witchports-dark",
    primary="#87d7ff",
    secondary="#bcbcbc",
    foreground="#eeeeee",
    background="#121212",
    surface="#1c1c1c",
    panel="#262626",
    error="#ff8787",
    warning="#ffd787",
    success="#afd787",
    variables={
        "border": "#87d7ff",
        "border-blurred": "#6c6c6c",
        "block-cursor-background": "#87d7ff",
        "block-cursor-foreground": "#121212",
        "block-cursor-blurred-background": "#3a3a3a",
        "block-cursor-blurred-foreground": "#eeeeee",
        "input-cursor-background": "#87d7ff",
        "input-cursor-foreground": "#121212",
        "footer-background": "#262626",
        "footer-key-foreground": "#87d7ff",
        "footer-description-foreground": "#eeeeee",
    },
)


class ConfirmStop(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss(False)", "Cancel")
    ]

    def __init__(self, listener: Listener) -> None:
        super().__init__()
        self.listener = listener

    def compose(self) -> ComposeResult:
        item = self.listener
        with Vertical(id="dialog"):
            yield Static(
                f"Stop {display(item.label)}?", id="dialog-title", markup=False
            )
            yield Static(f"PID {item.pid} · TCP {item.port} · SIGTERM", markup=False)
            yield Static(display(item.command), id="dialog-command", markup=False)
            yield Static("This can close other ports held by the process.")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Stop process", variant="error", id="confirm")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class WitchportsApp(App[None]):
    TITLE = "Witchports"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f5", "scan", "Rescan"),
        Binding("slash", "focus_search", "Search", key_display="/"),
        Binding("ctrl+s", "stop_selected", "Stop"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    CSS = """
    Screen { background: $background; color: $foreground; }
    #masthead { height: 2; padding: 0 1; background: $panel; }
    #title { width: auto; height: 100%; content-align: left middle; text-style: bold; }
    #status { width: 1fr; height: 100%; content-align: right middle; color: $secondary; }
    #controls { height: 3; margin: 1 1 0 1; }
    #range-tools { height: 3; width: 29; margin-right: 1; }
    #filter-tools { height: 3; width: 1fr; }
    #range { width: 1fr; }
    #search { width: 1fr; }
    Screen.compact #controls { layout: vertical; height: 6; margin-top: 0; }
    Screen.compact #range-tools, Screen.compact #filter-tools { width: 100%; }
    Screen.compact #range-tools { margin-right: 0; }
    Screen.compact #results { margin-top: 0; }
    Input { padding: 0 1; border: round $border-blurred; }
    Input:focus { border: round $primary; background-tint: transparent; }
    Input > .input--placeholder { color: $secondary; }
    #web { width: auto; margin-left: 1; border: round $border-blurred; }
    #web:focus { border: round $primary; }
    #web > .toggle--button { color: $border-blurred; background: $border-blurred; }
    #web.-on > .toggle--button { color: $background; background: $primary; }
    #web:focus > .toggle--label { background: $surface; color: $foreground; }

    Button { height: 3; border: round $border-blurred; background: $surface; color: $foreground; }
    Button:hover { border: round $secondary; background: $panel; }
    Button:focus { border: round $primary; text-style: bold; }
    Button:disabled { color: $secondary; text-opacity: 1; border: round $panel; }
    #scan { margin-left: 1; min-width: 8; background: $panel; color: $primary; }
    #scan:hover { background: $primary; color: $background; border: round $primary; }

    #results { height: 1fr; margin: 1 1 0 1; border: round $border-blurred; background: $background; }
    #listeners { height: 1fr; background: $background; scrollbar-size: 1 1; }
    #listeners:focus { background-tint: transparent; }
    #listeners > .datatable--header, #listeners:focus > .datatable--header {
        background: $panel; color: $foreground; background-tint: transparent;
    }
    #listeners > .datatable--even-row, #listeners:dark > .datatable--even-row { background: $surface; }
    #listeners > .datatable--odd-row { background: $background; }
    #listeners > .datatable--hover { background: $panel; }
    #empty { height: 1fr; padding: 0 1; content-align: center middle; color: $secondary; }

    #selection { height: 3; padding: 0 1; align: left middle; }
    #details { width: 1fr; height: 3; padding: 0 1; color: $secondary; content-align: left middle; }
    #stop { min-width: 16; margin-left: 1; }
    #stop:hover { color: $error; border: round $error; }
    Footer { padding: 0 1; }

    ConfirmStop { align: center middle; background: $background 72%; }
    #dialog {
        width: 62;
        max-width: 95%;
        height: auto;
        padding: 1 2;
        border: round $border-blurred;
        background: $surface;
    }
    #dialog-title { text-style: bold; color: $error; }
    #dialog-command { height: auto; max-height: 4; margin: 1 0; color: $secondary; }
    #dialog-buttons { height: 3; align: right middle; margin-top: 1; }
    #confirm { margin-left: 1; }
    #dialog Button { min-width: 10; }
    #dialog #confirm { background: $error; color: $background; border: round $error; }
    #dialog #confirm:focus { border: round $foreground; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.register_theme(WITCHPORTS_DARK)
        self.theme = WITCHPORTS_DARK.name
        self.listeners: list[Listener] = []
        self.visible_listeners: dict[str, Listener] = {}
        self.ports = PortRange()
        self.scanning = False
        self.stopping = False
        self.scan_error: str | None = None
        self.show_addresses = False
        self.layout_ready = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static("Witchports", id="title")
            yield Static("", id="status", markup=False)
        with Horizontal(id="controls"):
            with Horizontal(id="range-tools"):
                yield Input(
                    placeholder="All ports",
                    id="range",
                    tooltip="Port or inclusive range, e.g. 3000-9000",
                )
                yield Button("Scan", id="scan")
            with Horizontal(id="filter-tools"):
                yield Input(placeholder="Search listeners", id="search")
                yield Checkbox("Web only", id="web")
        with Vertical(id="results"):
            yield DataTable(id="listeners", cursor_type="row", zebra_stripes=True)
            yield Static("Scanning local listeners…", id="empty", markup=False)
        with Horizontal(id="selection"):
            yield Static("", id="details", markup=False)
            yield Button("Stop selected", id="stop", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Port", width=6, key="port")
        table.add_column("App / process", width=22, key="process")
        table.add_column("PID", width=7, key="pid")
        self.layout_ready = True
        self.set_compact_layout(self.size.width)
        table.focus()
        self.action_scan()

    def on_resize(self, event: events.Resize) -> None:
        if self.layout_ready:
            self.set_compact_layout(event.size.width)

    def set_compact_layout(self, width: int) -> None:
        self.screen_stack[0].set_class(width < 64, "compact")
        show_addresses = width >= 78
        if self.show_addresses == show_addresses:
            return
        self.show_addresses = show_addresses
        table = self.query_one(DataTable)
        if show_addresses:
            table.add_column("Bind address", width=30, key="address")
        else:
            table.remove_column("address")
        self.update_table()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_scan(self) -> None:
        if self.stopping:
            return
        value = self.query_one("#range", Input).value.strip()
        try:
            ports = PortRange.parse(value) if value else PortRange()
        except ValueError as error:
            self.query_one("#range", Input).focus()
            self.notify(str(error), severity="error")
            return
        self.ports = ports
        self.refresh_listeners()

    def refresh_listeners(self) -> None:
        self.scanning = True
        self.scan_error = None
        self.update_results_status()
        self.query_one("#stop", Button).disabled = True
        self.scan(self.ports)

    @work(exclusive=True, group="scan")
    async def scan(self, ports: PortRange) -> None:
        try:
            self.listeners = await asyncio.to_thread(find_listeners, ports)
        except ListenerError as error:
            self.listeners = []
            self.scan_error = str(error)
        self.scanning = False
        self.update_table()

    def update_table(self) -> None:
        table = self.query_one(DataTable)
        previous = (
            table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            if table.row_count
            else None
        )
        table.clear()
        self.visible_listeners.clear()
        search = self.query_one("#search", Input).value.strip().casefold()
        web_only = self.query_one("#web", Checkbox).value
        for item in self.listeners:
            if web_only and not item.framework:
                continue
            address = ", ".join(item.addresses)
            if (
                search
                and search
                not in f"{item.port} {item.pid} {item.label} {item.name} {address} {item.command}".casefold()
            ):
                continue
            key = f"{item.port}/{item.pid}"
            self.visible_listeners[key] = item
            cells: list[str | Text] = [
                Text(str(item.port), style="bold"),
                Text(display(item.label), no_wrap=True, overflow="ellipsis"),
                str(item.pid),
            ]
            if self.show_addresses:
                cells.append(Text(display(address), no_wrap=True, overflow="ellipsis"))
            table.add_row(*cells, key=key)
        if previous is not None and previous in self.visible_listeners:
            table.move_cursor(row=table.get_row_index(previous))
        self.update_results_status()
        self.show_selected()

    def update_results_status(self) -> None:
        shown = len(self.visible_listeners)
        total = len(self.listeners)
        if self.ports == PortRange():
            scope = "All ports"
        elif self.ports.first == self.ports.last:
            scope = f"Port {self.ports.first}"
        else:
            scope = f"{self.ports.first}–{self.ports.last}"
        status = f"{shown}/{total} shown" if shown != total else f"{total} listening"
        if self.scanning:
            status = "Scanning…"
        elif self.scan_error:
            status = "Scan failed"
        self.query_one("#status", Static).update(f"{status}  ·  {scope}")

        empty = self.query_one("#empty", Static)
        empty.display = not shown
        if not shown:
            if self.scanning:
                message = "Scanning local listeners…"
            elif self.scan_error:
                message = self.scan_error
            elif total:
                message = "No listeners match your filters."
            else:
                message = "No TCP listeners in this range."
            empty.update(message)
        self.query_one(DataTable).styles.height = "auto" if not shown else "1fr"

    def selected(self) -> Listener | None:
        table = self.query_one(DataTable)
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            return self.visible_listeners.get(key) if key is not None else None
        return None

    @on(DataTable.RowHighlighted)
    def show_selected(self) -> None:
        item = self.selected()
        details = Text(no_wrap=True, overflow="ellipsis")
        if item:
            details.append(display(item.label), style="bold")
            details.append(f"  ·  {display(', '.join(item.addresses))}\n")
            details.append(display(item.command))
        self.query_one("#details", Static).update(details)
        self.query_one("#stop", Button).disabled = (
            item is None or self.scanning or self.stopping
        )

    @on(Input.Changed, "#search")
    @on(Checkbox.Changed, "#web")
    def change_filter(self) -> None:
        self.update_table()

    @on(Input.Submitted, "#range")
    @on(Button.Pressed, "#scan")
    def scan_requested(self) -> None:
        self.action_scan()
        if self.scanning:
            self.query_one(DataTable).focus()

    @on(DataTable.RowSelected)
    @on(Button.Pressed, "#stop")
    def action_stop_selected(self) -> None:
        item = self.selected()
        if item is None or self.scanning or self.stopping:
            return

        def confirmed(result: bool | None) -> None:
            if result:
                self.stopping = True
                self.show_selected()
                self.stop_process(item)

        self.push_screen(ConfirmStop(item), confirmed)

    @work
    async def stop_process(self, item: Listener) -> None:
        try:
            stopped = await asyncio.to_thread(stop_listener, item)
        except ListenerError as error:
            self.notify(str(error), severity="error")
        else:
            if stopped:
                self.notify(f"PID {item.pid} stopped listening on port {item.port}.")
            else:
                self.notify(
                    f"Sent SIGTERM. PID {item.pid} is still listening on port {item.port}.",
                    severity="warning",
                )
        finally:
            self.stopping = False
            self.refresh_listeners()
