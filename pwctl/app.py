"""PipeWire Controller — application shell."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from .backend import system
from .ui.chains_page import ChainsPage
from .ui.dashboard import Dashboard
from .ui.devices import DevicesPage
from .ui.hrir_page import HrirPage
from .ui.settings_pages import ServerPage, StreamsPage, WirePlumberPage
from .ui.tools_page import ToolsPage
from .ui.widgets import async_call

PAGES = [
    ('dashboard', 'Dashboard', 'utilities-system-monitor-symbolic'),
    ('devices', 'Devices', 'audio-speakers-symbolic'),
    ('server', 'Server', 'preferences-system-symbolic'),
    ('streams', 'Streams', 'emblem-music-symbolic'),
    ('wireplumber', 'Session & Bluetooth', 'bluetooth-active-symbolic'),
    ('chains', 'Filter Chains', 'audio-headphones-symbolic'),
    ('hrir', 'HRIR Library', 'folder-music-symbolic'),
    ('tools', 'Tools', 'applications-utilities-symbolic'),
]

RESTART_UNITS = {
    'pipewire': ('PipeWire', system.restart_pipewire),
    'pulse': ('PipeWire-Pulse',
              lambda: system.restart_unit('pipewire-pulse.service')),
    'wireplumber': ('WirePlumber', system.restart_wireplumber),
}


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='PipeWire Controller',
                         default_width=1080, default_height=760)
        self._pending_restarts: set[str] = set()

        self.toaster = Adw.ToastOverlay()
        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE)

        # sidebar
        self.listbox = Gtk.ListBox(css_classes=['navigation-sidebar'])
        self.listbox.connect('row-selected', self._on_select)
        for name, title, icon in PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10,
                          margin_start=6, margin_end=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0))
            row.set_child(box)
            row.page_name = name
            self.listbox.append(row)

        side_view = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        side_header.set_title_widget(Adw.WindowTitle(
            title='PipeWire Controller', subtitle='audio control center'))
        side_view.add_top_bar(side_header)
        side_sw = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_sw.set_child(self.listbox)
        side_view.set_content(side_sw)

        # content
        self.banner = Adw.Banner(revealed=False, button_label='Restart now')
        self.banner.connect('button-clicked', self._restart_pending)
        self.content_title = Adw.WindowTitle(title='Dashboard')
        content_header = Adw.HeaderBar()
        content_header.set_title_widget(self.content_title)
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(content_header)
        content_view.add_top_bar(self.banner)
        content_view.set_content(self.stack)

        split = Adw.NavigationSplitView(
            min_sidebar_width=210, max_sidebar_width=240)
        split.set_sidebar(Adw.NavigationPage.new(side_view, 'Menu'))
        split.set_content(Adw.NavigationPage.new(content_view, 'Content'))
        self.toaster.set_child(split)
        self.set_content(self.toaster)

        # pages
        self.pages = {
            'dashboard': Dashboard(self),
            'devices': DevicesPage(self),
            'server': ServerPage(self),
            'streams': StreamsPage(self),
            'wireplumber': WirePlumberPage(self),
            'chains': ChainsPage(self),
            'hrir': HrirPage(self),
            'tools': ToolsPage(self),
        }
        for name, page in self.pages.items():
            self.stack.add_named(page.widget, name)
        start = 0
        import os
        want = os.environ.get('PWCTL_PAGE')
        if want:
            for i, (n, _t, _i2) in enumerate(PAGES):
                if n == want:
                    start = i
        self.listbox.select_row(self.listbox.get_row_at_index(start))

    # ---------------------------------------------------------- navigation --
    def _on_select(self, _lb, row):
        if row:
            self.stack.set_visible_child_name(row.page_name)
            title = next(t for n, t, _i in PAGES if n == row.page_name)
            self.content_title.set_title(title)

    def goto(self, name):
        for i, (n, _t, _i2) in enumerate(PAGES):
            if n == name:
                self.listbox.select_row(self.listbox.get_row_at_index(i))
                return

    # ------------------------------------------------------------ feedback --
    def toast(self, message, timeout=3):
        self.toaster.add_toast(Adw.Toast(title=message, timeout=timeout))

    def flag_restart(self, which: str):
        self._pending_restarts.add(which)
        labels = ' + '.join(RESTART_UNITS[w][0]
                            for w in ('pipewire', 'pulse', 'wireplumber')
                            if w in self._pending_restarts)
        self.banner.set_title(f'Saved. Restart {labels} to apply.')
        self.banner.set_revealed(True)

    def _restart_pending(self, _b):
        pending = set(self._pending_restarts)
        if 'pipewire' in pending:      # full stack restart covers the others
            pending = {'pipewire'}

        def work():
            for w in pending:
                RESTART_UNITS[w][1]()
            return True
        self.banner.set_revealed(False)
        self._pending_restarts.clear()
        self.toast('Restarting audio services…')
        async_call(work, lambda r, e: self.toast(
            'Audio services restarted' if not e else f'Restart failed: {e}'))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.pwctl.PipeWireController',
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css_file = Path(__file__).parent / 'style.css'
        if css_file.is_file():
            css.load_from_path(str(css_file))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


def main():
    import sys
    app = App()
    return app.run(sys.argv)
