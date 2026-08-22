"""PipeWire Controller — application shell."""

from __future__ import annotations

import signal
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .backend import graph, levels, prefs, presets, pw, system
from .ui.chains_page import ChainsPage
from .ui.dashboard import Dashboard
from .ui.devices import DevicesPage
from .ui.effects_page import EffectsPage
from .ui.enhance_page import EnhancePage
from .ui.paths_page import PathsPage
from .ui.graph_page import GraphPage
from .ui.hrir_page import HrirPage
from .ui.monitor_page import MonitorPage
from .ui.policy_page import PolicyPage
from .ui.settings_pages import ServerPage, StreamsPage, WirePlumberPage
from .ui.surround_page import SurroundPage
from .ui.tools_page import ToolsPage
from .ui.virtual_page import VirtualPage
from .ui.widgets import async_call, micro

# (name, title, icon, section).  Sixteen rows in one flat list read as
# "this is complicated" before a single click, so they are grouped — but every
# page stays present and reachable; nothing is hidden behind Advanced.
# Order within the list *is* the sidebar order; `goto()` and the startup
# restore both look pages up by name, so regrouping is safe.
PAGES = [
    ('dashboard', 'Dashboard', 'view-grid-symbolic', 'Mix'),
    ('paths', 'Signal Paths', 'network-transmit-receive-symbolic', 'Mix'),
    ('enhance', 'Equalizer', 'audio-x-generic-symbolic', 'Mix'),

    ('graph', 'Patchbay', 'network-workgroup-symbolic', 'Route'),
    ('devices', 'Devices', 'audio-speakers-symbolic', 'Route'),
    ('virtual', 'Virtual Devices', 'insert-object-symbolic', 'Route'),
    ('surround', 'Surround Setup', 'audio-card-symbolic', 'Route'),

    ('chains', 'Filter Chains', 'audio-headphones-symbolic', 'Process'),
    ('effects', 'Effects', 'applications-multimedia-symbolic', 'Process'),
    ('hrir', 'HRIR Library', 'folder-music-symbolic', 'Process'),

    ('server', 'Server', 'preferences-system-symbolic', 'Configure'),
    ('streams', 'Streams', 'emblem-music-symbolic', 'Configure'),
    ('policy', 'App Policies', 'system-users-symbolic', 'Configure'),
    ('wireplumber', 'Session & Bluetooth', 'bluetooth-active-symbolic',
     'Configure'),

    ('monitor', 'Monitor', 'utilities-system-monitor-symbolic', 'System'),
    ('tools', 'Tools', 'applications-utilities-symbolic', 'System'),
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
        for name, title, icon, section in PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10,
                          margin_start=6, margin_end=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0))
            row.set_child(box)
            row.page_name = name
            row.section = section
            self.listbox.append(row)
        # Section headings are ListBox *headers*, not rows: they carry no
        # index, so `get_row_at_index()` in goto() keeps matching PAGES, and
        # keyboard navigation skips straight over them.
        self.listbox.set_header_func(self._sidebar_header)

        side_view = Adw.ToolbarView()
        # The sidebar is a slightly lifted surface, the way the Overview's
        # cards are, so the two panes read as foreground and background rather
        # than one flat field.  The tone and the footer hairline are in
        # style.css; this is only where the classes go on.
        side_view.add_css_class('pwctl-sidebar')
        side_header = Adw.HeaderBar()
        side_header.add_css_class('pwctl-sidebar-header')
        side_header.add_css_class('pwctl-header')
        app_title = Adw.WindowTitle(title='PipeWire Controller',
                                    subtitle='audio control center')
        app_title.add_css_class('app-title')
        side_header.set_title_widget(app_title)
        side_view.add_top_bar(side_header)
        # C3 and C14 are one line of chrome, so their hairlines must land on
        # one y.  Both are now drawn by the same libadwaita style rather than
        # by a hand-written border on one side and a toolbar style on the
        # other — that pairing is what put them 6px apart (the sidebar header
        # measured 46, the content header 40).  Equal heights come from
        # `.pwctl-header` in style.css.
        side_view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
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
        adv_box.add_css_class('pwctl-sidebar-footer')
        side_view.add_bottom_bar(adv_box)

        # content
        self.banner = Adw.Banner(revealed=False, button_label='Restart now')
        self.banner.connect('button-clicked', self._restart_pending)
        self.content_title = Adw.WindowTitle(title='Dashboard')
        self.content_title.add_css_class('page-title')
        content_header = Adw.HeaderBar()
        content_header.add_css_class('pwctl-header')
        self.content_header = content_header
        content_header.set_title_widget(self.content_title)
        content_header.pack_end(self._build_presets_button())
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(content_header)
        content_view.add_top_bar(self.banner)
        content_view.set_content(self.stack)
        # The hairline under the header bar (and under the restart banner when
        # it is showing).  libadwaita's own top-bar style, so it follows the
        # theme instead of guessing at a border colour.
        content_view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)

        split = Adw.NavigationSplitView(
            min_sidebar_width=210, max_sidebar_width=240)
        split.set_sidebar(Adw.NavigationPage.new(side_view, 'Menu'))
        split.set_content(Adw.NavigationPage.new(content_view, 'Content'))
        self.toaster.set_child(split)
        self.set_content(self.toaster)

        # pages
        self.pages = {
            'dashboard': Dashboard(self),
            'graph': GraphPage(self),
            'devices': DevicesPage(self),
            'virtual': VirtualPage(self),
            'surround': SurroundPage(self),
            'server': ServerPage(self),
            'streams': StreamsPage(self),
            'policy': PolicyPage(self),
            'wireplumber': WirePlumberPage(self),
            'chains': ChainsPage(self),
            'effects': EffectsPage(self),
            'enhance': EnhancePage(self),
            'paths': PathsPage(self),
            'hrir': HrirPage(self),
            'monitor': MonitorPage(self),
            'tools': ToolsPage(self),
        }
        for name, page in self.pages.items():
            self.stack.add_named(page.widget, name)

        # Two Dashboard-owned controls that live in the window's header bar.
        # The Overview/Mixer switcher is packed at the start, where the mockup
        # puts it, and stays there on every page: on another page neither
        # segment is lit and clicking one brings you back to the Dashboard
        # showing that view, so the mixer is always one click away.  The
        # volume-style picker is a global preference (it changes the sliders
        # on Devices, the Equalizer and Signal Paths too), so it sits beside
        # Device Presets rather than floating over the Dashboard.
        dash = self.pages['dashboard']
        content_header.pack_start(dash.view_switcher)
        content_header.pack_end(dash.style_button)

        start = 0
        import os
        self._debug_page = bool(os.environ.get('PWCTL_PAGE'))
        want = os.environ.get('PWCTL_PAGE') or prefs.get('last_page')
        if want:
            for i, (n, _t, _i2, _s) in enumerate(PAGES):
                if n == want:
                    start = i
        self.listbox.select_row(self.listbox.get_row_at_index(start))
        GLib.timeout_add_seconds(5, self._autoload_tick)

        # link / service watcher for system notifications
        self._last_patch_ts = 0.0
        self._watch_links = None       # set of (out_name, port, in_name, port)
        self._watch_states: dict[str, str] = {}
        self._watch_busy = False
        GLib.timeout_add_seconds(5, self._watch_tick)

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
        btn = Gtk.MenuButton(tooltip_text='Device presets')
        btn.set_child(Adw.ButtonContent(icon_name='user-bookmarks-symbolic',
                                        label='Device Presets'))
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
    def _sidebar_header(self, row, before):
        """One micro heading above the first row of each section."""
        if before is not None and before.section == row.section:
            row.set_header(None)
            return
        lbl = micro(row.section)
        lbl.add_css_class('sidebar-section')
        lbl.set_margin_top(4 if before is None else 14)
        lbl.set_margin_bottom(2)
        lbl.set_margin_start(14)
        row.set_header(lbl)

    def _on_select(self, _lb, row):
        if row:
            self.stack.set_visible_child_name(row.page_name)
            title = next(t for n, t, _i, _s in PAGES if n == row.page_name)
            self._set_header_widget(row.page_name, title)
            dash = getattr(self, 'pages', {}).get('dashboard')
            if dash is not None:
                dash.set_on_page(row.page_name == 'dashboard')
            if not self._debug_page:
                prefs.save(last_page=row.page_name)

    def _set_header_widget(self, page_name, title):
        """A page may own the header bar's title slot.

        Nothing claims it at the moment — the Dashboard's Overview/Mixer
        switcher moved to `pack_start`, so it stays visible on every page —
        but the hook is how a page gets a control into the header bar without
        the window knowing anything about it, and it costs no page height.
        """
        page = self.pages.get(page_name) if hasattr(self, 'pages') else None
        widget = getattr(page, 'header_widget', None)
        if widget is not None:
            self.content_header.set_title_widget(widget)
        else:
            self.content_title.set_title(title)
            self.content_header.set_title_widget(self.content_title)

    def _save_window_state(self, *_a):
        prefs.save(win_width=self.get_width() or 1080,
                   win_height=self.get_height() or 760,
                   win_maximized=self.is_maximized())
        return False

    def goto(self, name):
        for i, (n, _t, _i2, _s) in enumerate(PAGES):
            if n == name:
                self.listbox.select_row(self.listbox.get_row_at_index(i))
                return

    # ------------------------------------------------------- notifications --
    def notify_user(self, title: str, body: str):
        """Desktop notification (used for broken links, failures, xruns)."""
        app = self.get_application()
        if not app:
            return
        note = Gio.Notification.new(title)
        note.set_body(body)
        note.set_priority(Gio.NotificationPriority.NORMAL)
        app.send_notification(None, note)

    def mark_user_patch(self):
        """Patch changes we made ourselves shouldn't raise link warnings."""
        import time
        self._last_patch_ts = time.monotonic()

    def _watch_tick(self):
        import time
        want_links = bool(prefs.get('notify_links'))
        want_services = bool(prefs.get('notify_services'))
        if (not want_links and not want_services) or self._watch_busy:
            return True
        self._watch_busy = True

        def collect():
            states = {}
            if want_services:
                for unit in ('pipewire.service', 'wireplumber.service',
                             'pipewire-pulse.service'):
                    states[unit] = system.unit_state(unit)
            links, names = None, {}
            if want_links:
                g = graph.snapshot()
                ports = {p.id: p for n in g.nodes.values()
                         for p in n.inputs + n.outputs}
                links = set()
                for link in g.links:
                    op, ip = ports.get(link.out_port), ports.get(link.in_port)
                    if not op or not ip:
                        continue
                    key = (g.nodes[link.out_node].name, op.name,
                           g.nodes[link.in_node].name, ip.name)
                    links.add(key)
                names = {n.name: n.label for n in g.nodes.values()}
            return states, links, names

        def done(result, error):
            self._watch_busy = False
            if error or result is None:
                return
            states, links, names = result
            for unit, state in states.items():
                prev = self._watch_states.get(unit)
                if state == 'failed' and prev not in (None, 'failed'):
                    self.notify_user('Audio service failed',
                                     f'{unit} entered the failed state — '
                                     'check the Monitor page.')
                self._watch_states[unit] = state
            if links is not None:
                prev_links = self._watch_links
                self._watch_links = links
                recent_patch = (time.monotonic() - self._last_patch_ts) < 15
                if prev_links is not None and not recent_patch:
                    gone = prev_links - links
                    # only links whose endpoints still exist: a closed app
                    # takes its node away and is not a broken link
                    broken = [(o, i) for o, _op, i, _ip in gone
                              if o in names and i in names]
                    if broken:
                        o, i = broken[0]
                        extra = (f' (and {len(broken) - 1} more)'
                                 if len(broken) > 1 else '')
                        self.notify_user(
                            'Audio link disconnected',
                            f'{names.get(o, o)} → {names.get(i, i)} was '
                            f'disconnected{extra}.')
            return
        async_call(collect, done)
        return True

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
        self.mark_user_patch()      # don't report the restart as broken links
        self.banner.set_revealed(False)
        self._pending_restarts.clear()
        self.toast('Restarting audio services…')
        async_call(work, lambda r, e: self.toast(
            'Audio services restarted' if not e else f'Restart failed: {e}'))


class App(Adw.Application):
    def __init__(self):
        # Matches the desktop entry filename, so the shell can associate the
        # window with its launcher icon, and is the id the Flatpak will use.
        super().__init__(
            application_id='io.github.knightinfected.PipeWireControlCenter',
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Meters are child processes and outlive a crash or a kill, so clear
        # out anything an earlier run left behind before starting new ones.
        levels.reap_orphans()
        # ...and make sure this run leaves nothing behind either: a plain
        # `kill` (or Ctrl-C) would otherwise skip every Gtk shutdown path.
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
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

    def _on_signal(self):
        self.quit()                 # runs do_shutdown, which stops the meters
        return GLib.SOURCE_REMOVE

    def do_shutdown(self):
        levels.stop_all()
        Adw.Application.do_shutdown(self)


def main():
    import sys
    app = App()
    return app.run(sys.argv)
