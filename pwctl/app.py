"""PipeWire Controller — application shell."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .backend import presets, prefs, pw, system
from .ui.chains_page import ChainsPage
from .ui.dashboard import Dashboard
from .ui.devices import DevicesPage
from .ui.hrir_page import HrirPage
from .ui.settings_pages import ServerPage, StreamsPage, WirePlumberPage
from .ui.surround_page import SurroundPage
from .ui.tools_page import ToolsPage
from .ui.widgets import async_call

PAGES = [
    ('dashboard', 'Dashboard', 'utilities-system-monitor-symbolic'),
    ('devices', 'Devices', 'audio-speakers-symbolic'),
    ('surround', 'Surround Setup', 'audio-card-symbolic'),
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
                         default_width=int(prefs.get('win_width') or 1080),
                         default_height=int(prefs.get('win_height') or 760))
        if prefs.get('win_maximized'):
            self.maximize()
        self.connect('close-request', self._save_window_state)
        self._pending_restarts: set[str] = set()
        self.advanced = bool(prefs.get('advanced'))
        self._advanced_widgets: list = []
        self._last_default_sink = None

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

        # advanced-settings toggle, bottom-left
        adv_box = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10,
                          margin_start=16, margin_end=16)
        adv_box.append(Gtk.Image.new_from_icon_name(
            'applications-engineering-symbolic'))
        adv_label = Gtk.Label(label='Advanced', xalign=0, hexpand=True)
        adv_box.append(adv_label)
        self.adv_switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                     active=self.advanced,
                                     tooltip_text='Show advanced settings '
                                                  'throughout the app')
        self.adv_switch.connect('notify::active', self._on_advanced)
        adv_box.append(self.adv_switch)
        side_view.add_bottom_bar(adv_box)

        # content
        self.banner = Adw.Banner(revealed=False, button_label='Restart now')
        self.banner.connect('button-clicked', self._restart_pending)
        self.content_title = Adw.WindowTitle(title='Dashboard')
        content_header = Adw.HeaderBar()
        content_header.set_title_widget(self.content_title)
        content_header.pack_end(self._build_presets_button())
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
            'surround': SurroundPage(self),
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
        self._debug_page = bool(os.environ.get('PWCTL_PAGE'))
        want = os.environ.get('PWCTL_PAGE') or prefs.get('last_page')
        if want:
            for i, (n, _t, _i2) in enumerate(PAGES):
                if n == want:
                    start = i
        self.listbox.select_row(self.listbox.get_row_at_index(start))
        GLib.timeout_add_seconds(5, self._autoload_tick)

    # ------------------------------------------------------------ advanced --
    def register_advanced(self, widget):
        """Track a widget that is only visible while Advanced mode is on."""
        self._advanced_widgets.append(widget)
        widget.set_visible(self.advanced)

    def _on_advanced(self, switch, _p):
        self.advanced = switch.get_active()
        prefs.save(advanced=self.advanced)
        for w in self._advanced_widgets:
            w.set_visible(self.advanced)

    # ------------------------------------------------------ device presets --
    def _build_presets_button(self):
        btn = Gtk.MenuButton(icon_name='user-bookmarks-symbolic',
                             tooltip_text='Device presets')
        self._presets_popover = Gtk.Popover()
        self._presets_popover.connect('show', self._fill_presets_popover)
        btn.set_popover(self._presets_popover)
        return btn

    def _fill_presets_popover(self, _pop):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12, width_request=340)
        title = Gtk.Label(label='Device presets', xalign=0)
        title.add_css_class('heading')
        box.append(title)
        hint = Gtk.Label(
            label='A preset stores channel-mix settings, volume and card '
                  'profile for one output device.',
            xalign=0, wrap=True, max_width_chars=42)
        hint.add_css_class('caption')
        hint.add_css_class('dim-label')
        box.append(hint)

        auto = Gtk.Box(spacing=8, margin_top=6)
        auto_lbl = Gtk.Label(label='Auto-load when default output changes',
                             xalign=0, hexpand=True, wrap=True)
        sw = Gtk.Switch(valign=Gtk.Align.CENTER,
                        active=bool(prefs.get('autoload_presets')))
        sw.connect('notify::active',
                   lambda s, _p: prefs.save(autoload_presets=s.get_active()))
        auto.append(auto_lbl)
        auto.append(sw)
        box.append(auto)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))

        save_btn = Gtk.Button()
        save_btn.set_child(Adw.ButtonContent(
            icon_name='document-save-symbolic',
            label='Save preset for current output'))
        save_btn.connect('clicked', self._save_preset)
        box.append(save_btn)

        saved = presets.all_presets()
        if saved:
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
            for name, p in sorted(saved.items()):
                row = Gtk.Box(spacing=8)
                labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 hexpand=True)
                t = Gtk.Label(label=p.get('description', name), xalign=0)
                labels.append(t)
                sub = Gtk.Label(label=f'saved {p.get("saved", "?")}',
                                xalign=0)
                sub.add_css_class('caption')
                sub.add_css_class('dim-label')
                labels.append(sub)
                row.append(labels)
                apply_b = Gtk.Button(icon_name='media-playback-start-symbolic',
                                     tooltip_text='Apply now')
                apply_b.add_css_class('flat')
                apply_b.connect('clicked', self._apply_preset, p)
                del_b = Gtk.Button(icon_name='user-trash-symbolic',
                                   tooltip_text='Delete preset')
                del_b.add_css_class('flat')
                del_b.connect('clicked', self._delete_preset, name)
                row.append(apply_b)
                row.append(del_b)
                box.append(row)
        self._presets_popover.set_child(box)

    def _save_preset(self, _b):
        self._presets_popover.popdown()
        async_call(presets.snapshot,
                   lambda p, e: self.toast(
                       f'Preset saved for {p["description"]}' if p and not e
                       else 'Could not save preset'))

    def _apply_preset(self, _b, preset):
        self._presets_popover.popdown()

        def done(actions, e):
            if e or actions is None:
                self.toast('Preset failed')
                return
            if any('channel-mix' in a for a in actions):
                self.flag_restart('pulse')
            self.toast(f'Applied preset for {preset["description"]}: '
                       + (', '.join(actions) if actions else 'nothing to do'))
        async_call(lambda: presets.apply(preset), done)

    def _delete_preset(self, _b, name):
        presets.delete(name)
        self._presets_popover.popdown()
        self.toast('Preset deleted')

    def _autoload_tick(self):
        if not prefs.get('autoload_presets'):
            return True

        def check():
            return pw.read_default_names().get('default.audio.sink')

        def done(name, e):
            if e or not name:
                return
            prev, self._last_default_sink = self._last_default_sink, name
            if prev is None or name == prev:
                return
            preset = presets.preset_for(name)
            if preset:
                self._apply_preset(None, preset)
        async_call(check, done)
        return True

    # ---------------------------------------------------------- navigation --
    def _on_select(self, _lb, row):
        if row:
            self.stack.set_visible_child_name(row.page_name)
            title = next(t for n, t, _i in PAGES if n == row.page_name)
            self.content_title.set_title(title)
            if not self._debug_page:
                prefs.save(last_page=row.page_name)

    def _save_window_state(self, *_a):
        prefs.save(win_width=self.get_width() or 1080,
                   win_height=self.get_height() or 760,
                   win_maximized=self.is_maximized())
        return False

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
