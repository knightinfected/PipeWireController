"""Preferences: the app's own choices, in four tabs along the top (DR-010).

Not `Adw.PreferencesDialog`: that one moves its tabs to a bar along the
bottom when it is narrow, and the tabs belong at the top at every width.  So
an `Adw.Dialog` around an `Adw.ViewStack`: wide, the switcher is the
header's title (as on the Dashboard); narrow, it is a full-width row right
under the header.

PipeWire's own settings stay on their pages (Server, Streams, App Policies,
Session & Bluetooth).  This window holds how *the app* behaves.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import __version__
from ..backend import autostart, diagnostics, prefs
from .volume import VOLUME_STYLES, make_volume
from .widgets import async_call, confirm, esc, text_viewer_dialog

REPO = 'https://github.com/knightinfected/PipeWireController'
LINKS = {
    'sponsor': 'https://github.com/sponsors/knightinfected',
    'paypal': 'https://paypal.me/hmzknight',
    'issue': f'{REPO}/issues/new/choose',
    'known': f'{REPO}/issues/1',
    'discuss': f'{REPO}/discussions',
    'source': REPO,
    'changes': f'{REPO}/blob/main/CHANGELOG.md',
    'license': f'{REPO}/blob/main/LICENSE',
}

# (name, title, icon)
TABS = [
    ('general', 'General', 'pwctl-gear-symbolic'),
    ('startup', 'Startup', 'system-shutdown-symbolic'),
    ('notifications', 'Notifications', 'pwctl-bell-symbolic'),
    ('about', 'About', 'help-about-symbolic'),
]

# Below this the tabs leave the header for a row of their own.
NARROW_SP = 560


def open_url(parent, url):
    """In the browser.  Through the portal when there is one."""
    launcher = Gtk.UriLauncher.new(url)

    def done(src, res):
        try:
            src.launch_finish(res)
        except GLib.Error:
            import subprocess
            subprocess.Popen(['xdg-open', url])
    launcher.launch(parent, None, done)


def notification_service() -> bool | None:
    """Whether something on the session bus shows notifications, or None
    when the bus can't be asked."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            'org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'NameHasOwner',
            GLib.Variant('(s)', ('org.freedesktop.Notifications',)),
            GLib.VariantType('(b)'), Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        return None
    return bool(reply.unpack()[0])


def _group(title='', description=''):
    return Adw.PreferencesGroup(title=title, description=description)


def _link_row(title, subtitle, icon, on_click):
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
    if icon:
        row.add_prefix(Gtk.Image(icon_name=icon))
    row.add_suffix(Gtk.Image(icon_name='adw-external-link-symbolic'))
    row.connect('activated', lambda _r: on_click())
    return row


class PreferencesDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title='Preferences', content_width=640,
                         content_height=720)
        # A dialog with a breakpoint must state its minimum, or GTK can't
        # size it: the window's own (360 wide), shorter than any tab.
        self.set_size_request(360, 294)
        self.window = window
        self._updating = False
        self._tokens = []
        self.connect('closed', self._on_closed)

        self.stack = Adw.ViewStack(vexpand=True)
        self.pages = {}
        builders = {'general': self._general, 'startup': self._startup,
                    'notifications': self._notifications,
                    'about': self._about}
        for name, title, icon in TABS:
            page = Adw.PreferencesPage()
            for g in builders[name]():
                page.add(g)
            self.pages[name] = page
            self.stack.add_titled_with_icon(page, name, title, icon)

        # The tabs: in the header when wide, a row under it when narrow.
        self.title_stack = Gtk.Stack(hhomogeneous=False)
        self.wide_switcher = Adw.ViewSwitcher(
            stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.title_stack.add_named(self.wide_switcher, 'tabs')
        self.title_stack.add_named(Adw.WindowTitle(title='Preferences'),
                                   'title')
        header = Adw.HeaderBar()
        header.set_title_widget(self.title_stack)
        self.narrow_switcher = Adw.ViewSwitcher(
            stack=self.stack, policy=Adw.ViewSwitcherPolicy.NARROW,
            hexpand=True)
        self.narrow_bar = Gtk.Box(visible=False, margin_start=6,
                                  margin_end=6, margin_bottom=6)
        self.narrow_bar.add_css_class('pwctl-prefs-tabs')
        self.narrow_bar.append(self.narrow_switcher)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.narrow_bar)
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        view.set_content(self.stack)
        self.set_child(view)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(
            f'max-width: {NARROW_SP}sp'))
        bp.add_setter(self.title_stack, 'visible-child-name', 'title')
        bp.add_setter(self.narrow_bar, 'visible', True)
        self.add_breakpoint(bp)

        self._watch('volume_style', self._sync_style)
        self._probe()

    # ------------------------------------------------------------ helpers --
    def show_tab(self, name):
        if name in self.pages:
            self.stack.set_visible_child_name(name)

    def _watch(self, key, cb):
        self._tokens.append(prefs.watch(key, cb))

    def _on_closed(self, *_a):
        for t in self._tokens:
            prefs.unwatch(t)
        self._tokens = []

    def _switch(self, key, title, subtitle=''):
        """A switch bound to a boolean preference, both ways."""
        row = Adw.SwitchRow(title=title, subtitle=subtitle,
                            active=bool(prefs.get(key)))
        row.pref_key = key

        def changed(r, _p):
            if not self._updating:
                prefs.save(**{key: r.get_active()})
        row.connect('notify::active', changed)

        def follow(value):
            self._updating = True
            row.set_active(bool(value))
            self._updating = False
        self._watch(key, follow)
        return row

    def _choice(self, key):
        """A combo row for a preference with fixed choices."""
        p = prefs.BY_KEY[key]
        values = [v for v, _l in p.choices]
        row = Adw.ComboRow(title=p.title, subtitle=p.subtitle,
                           model=Gtk.StringList.new([lbl for _v, lbl
                                                     in p.choices]))
        row.pref_key = key
        cur = prefs.get(key)
        row.set_selected(values.index(cur) if cur in values else 0)

        def changed(r, _p):
            if not self._updating:
                prefs.save(**{key: values[r.get_selected()]})
        row.connect('notify::selected', changed)

        def follow(value):
            if value in values:
                self._updating = True
                row.set_selected(values.index(value))
                self._updating = False
        self._watch(key, follow)
        return row

    def _toast(self, text):
        self.window.toast(text)

    # ------------------------------------------------------------ General --
    def _general(self):
        look = _group('Appearance')
        look.add(self._choice('color_scheme'))

        sliders = _group('Volume sliders',
                         'How every volume control in the app looks. The '
                         'button in the header bar changes the same thing.')
        self._style_checks = {}
        first = None
        cur = prefs.get('volume_style')
        for key, title, sub, _icon in VOLUME_STYLES:
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            check = Gtk.CheckButton(active=key == cur,
                                    valign=Gtk.Align.CENTER)
            if first is None:
                first = check
            else:
                check.set_group(first)
            check.connect('toggled', self._on_style_toggled, key)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            # The style itself, to try: it moves, and changes nothing.
            sample = make_volume(key, lambda _v: None, compact=True)
            sample.set_value(0.7)
            sample.widget.set_size_request(150, -1)
            sample.widget.set_valign(Gtk.Align.CENTER)
            row.add_suffix(sample.widget)
            row._sample = sample
            self._style_checks[key] = check
            sliders.add(row)
        sliders.add(self._switch(
            'level_meters', prefs.BY_KEY['level_meters'].title,
            prefs.BY_KEY['level_meters'].subtitle))

        pages = _group('Pages')
        p = prefs.BY_KEY['show_advanced']
        pages.add(self._switch('show_advanced', p.title, p.subtitle))
        pages.add(self._choice('start_page'))

        reset = _group('Reset')
        row = Adw.ActionRow(
            title='Patchbay layout',
            subtitle='Put every node back in its column. Your cables stay.')
        btn = Gtk.Button(label='Reset', valign=Gtk.Align.CENTER)
        btn.connect('clicked', self._reset_patchbay)
        row.add_suffix(btn)
        reset.add(row)
        row = Adw.ActionRow(
            title='Preferences',
            subtitle='Every choice in this window back to how it was at '
                     'install. Favourites, presets and everything you '
                     'built stay.')
        btn = Gtk.Button(label='Reset', valign=Gtk.Align.CENTER)
        btn.add_css_class('destructive-action')
        btn.connect('clicked', self._reset_prefs)
        row.add_suffix(btn)
        reset.add(row)
        return [look, sliders, pages, reset]

    def _on_style_toggled(self, check, key):
        if check.get_active() and not self._updating:
            prefs.save(volume_style=key)

    def _sync_style(self, value):
        check = self._style_checks.get(value)
        if check is not None and not check.get_active():
            self._updating = True
            check.set_active(True)
            self._updating = False

    def _reset_patchbay(self, _b):
        def do():
            page = self.window.pages.get('graph')
            if page is not None:
                page.reset_layout()
            else:
                prefs.save(graph_positions={})
            self._toast('Patchbay layout reset')
        confirm(self, 'Reset the Patchbay layout?',
                'Every node goes back to its default place. Cables and '
                'devices are not touched.', 'Reset', do, destructive=False)

    def _reset_prefs(self, _b):
        def do():
            changed = prefs.reset_preferences()
            self._toast('Preferences reset' if changed
                        else 'Preferences were already at their defaults')
        confirm(self, 'Reset preferences?',
                'Every choice in this window goes back to its default. '
                'Start at login is not changed. Favourites, device presets, '
                'the Patchbay layout and everything you built stay.',
                'Reset', do)

    # ------------------------------------------------------------ Startup --
    def _startup(self):
        login = _group('Start with your computer')
        self.login_row = Adw.SwitchRow(
            title='Start at login',
            subtitle='Only needed for what is listed under “Needs the app '
                     'running” below.',
            active=autostart.is_enabled())
        self.login_row.connect('notify::active', self._on_login)
        login.add(self.login_row)
        self.min_row = Adw.SwitchRow(
            title='Start minimized',
            subtitle='No window opens at login: it runs in the background '
                     'until you quit it. Open it from your app menu.',
            active=bool(prefs.get('start_minimized')))
        self.min_row.connect('notify::active', self._on_minimized)
        login.add(self.min_row)
        # Filled by `_probe()`: whether this desktop runs login apps.
        self.support_row = Adw.ActionRow(visible=False)
        self.support_row.add_css_class('pwctl-prefs-note')
        self.support_copy = Gtk.Button(icon_name='edit-copy-symbolic',
                                       valign=Gtk.Align.CENTER,
                                       tooltip_text='Copy the line')
        self.support_copy.add_css_class('flat')
        self.support_copy.connect('clicked', self._copy_line)
        self.support_row.add_suffix(self.support_copy)
        login.add(self.support_row)
        self._line = None
        self._support = None
        self._sync_login()

        closing = _group('Closing the window')
        closing.add(self._switch(
            'keep_running', 'Keep running in the background',
            'Closing the window leaves the app running, so device presets '
            'keep switching and notifications keep coming. Open it again '
            'from your app menu.'))

        needs = _group(
            'Needs the app running',
            'Filter chains, virtual devices, Signal Paths, equalizers and '
            'mic cleanup work on their own, with the app closed. Only '
            'these need it running:')
        needs.add(self._switch(
            'autoload_presets', 'Switch device presets automatically',
            'When the default output changes, apply the preset saved for '
            'it (Device Presets in the header bar).'))
        row = Adw.ActionRow(title='Desktop notifications',
                            subtitle='Broken links, failed services, dropouts',
                            activatable=True)
        row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
        row.connect('activated', lambda _r: self.show_tab('notifications'))
        needs.add(row)

        quit_ = _group(description='Ctrl+Q quits too. While “Keep running” '
                                   'is on, closing the window only hides it.')
        q = Adw.ButtonRow(title='Quit PipeWire Controller')
        q.connect('activated', lambda _r: self.window.get_application()
                  .activate_action('quit', None))
        quit_.add(q)
        return [login, closing, needs, quit_]

    def _on_login(self, row, _p):
        if self._updating:
            return
        try:
            if row.get_active():
                autostart.enable(bool(prefs.get('start_minimized')))
            else:
                autostart.disable()
        except OSError as e:
            self._toast(f'Could not change the login item: {e}')
        self._sync_login()

    def _on_minimized(self, row, _p):
        if self._updating:
            return
        prefs.save(start_minimized=row.get_active())
        if autostart.is_enabled():
            try:
                autostart.enable(row.get_active())
            except OSError as e:
                self._toast(f'Could not change the login item: {e}')
        self._sync_login()

    def _sync_login(self):
        self._updating = True
        on = autostart.is_enabled()
        self.login_row.set_active(on)
        self.min_row.set_sensitive(on)
        self._updating = False
        self._paint_support()

    def _paint_support(self):
        support = self._support
        on = autostart.is_enabled()
        row = self.support_row
        if support is None or not on or support[0] in (autostart.YES,
                                                       autostart.MAYBE):
            row.set_visible(False)
            return
        answer, why = support
        line = autostart.compositor_line(bool(prefs.get('start_minimized')))
        self._line = line[2] if (answer == autostart.NO and line) else None
        if self._line:
            name, path = line[0], line[1]
            row.set_title(esc(f'{name} won’t start it by itself'))
            row.set_subtitle(esc(f'Add this line to {path}, or start {name} '
                                 'with uwsm:') + f'\n<tt>{esc(self._line)}</tt>')
            self.support_copy.set_visible(True)
        else:
            row.set_title('Your desktop may not start it')
            row.set_subtitle(esc(why))
            self.support_copy.set_visible(False)
        row.set_subtitle_selectable(True)
        row.set_visible(True)

    def _copy_line(self, _b):
        if self._line:
            Gdk.Display.get_default().get_clipboard().set(self._line)
            self._toast('Copied')

    # ------------------------------------------------------ Notifications --
    def _notifications(self):
        notes = _group('Notify me about',
                       'Desktop notifications, while the app is running '
                       '(see Startup).')
        notes.add(self._switch(
            'notify_links', 'Broken or disconnected links',
            'A link between two live nodes disappears, e.g. a device '
            'vanished mid-stream.'))
        notes.add(self._switch(
            'notify_services', 'Service failures',
            'PipeWire, WirePlumber or PipeWire-Pulse enters the failed '
            'state.'))
        notes.add(self._switch(
            'notify_xruns', 'Audio dropouts (xruns)',
            'Only while the Monitor page is open: it is the page that '
            'measures them.'))

        test = _group()
        self.test_row = Adw.ActionRow(title='Send a test notification',
                                      subtitle='Checking your desktop…')
        btn = Gtk.Button(label='Send', valign=Gtk.Align.CENTER)
        btn.connect('clicked', self._send_test)
        self.test_row.add_suffix(btn)
        test.add(self.test_row)
        return [notes, test]

    def _send_test(self, _b):
        self.window.notify_user('Test notification',
                                'If you can read this, notifications from '
                                'PipeWire Controller work.')

    # -------------------------------------------------------------- About --
    def _about(self):
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       halign=Gtk.Align.CENTER, margin_top=6,
                       margin_bottom=6)
        hero.add_css_class('pwctl-about-hero')
        icon = Gtk.Image(icon_name='audio-card', pixel_size=96)
        icon.add_css_class('icon-dropshadow')
        hero.append(icon)
        name = Gtk.Label(label='PipeWire Controller', wrap=True,
                         justify=Gtk.Justification.CENTER)
        name.add_css_class('title-1')
        hero.append(name)
        what = Gtk.Label(label='An audio control center for PipeWire',
                         wrap=True, justify=Gtk.Justification.CENTER)
        what.add_css_class('dim-label')
        hero.append(what)
        ver = Gtk.Label(label=f'Version {__version__}',
                        halign=Gtk.Align.CENTER)
        ver.add_css_class('pwctl-about-version')
        hero.append(ver)
        top = _group()
        top.add(hero)

        support = _group(
            'Support the project',
            'It is free, GPL-3.0, and nothing is ever locked behind a '
            'donation. Support gives me more time to maintain it, and if '
            'you can’t, feedback and bug reports help just as much.')
        for key, title, sub in (
                ('sponsor', 'Sponsor on GitHub',
                 'github.com/sponsors/knightinfected'),
                ('paypal', 'Donate with PayPal', 'paypal.me/hmzknight')):
            row = _link_row(title, sub, 'pwctl-heart-symbolic',
                            lambda k=key: open_url(self.window, LINKS[k]))
            row.add_css_class('pwctl-donate')
            support.add(row)

        involved = _group('Feedback and help')
        for key, title, sub, icon in (
                ('issue', 'Report a bug or suggest a feature',
                 'Opens a new issue on GitHub', 'pwctl-bug-symbolic'),
                ('discuss', 'Ask a question',
                 'GitHub Discussions', 'pwctl-chat-symbolic'),
                ('known', 'Known issues', 'What is being worked on', None),
                ('changes', 'What’s new', 'The changelog', None),
                ('source', 'Source code',
                 'github.com/knightinfected/PipeWireController', None)):
            involved.add(_link_row(title, sub, icon,
                                   lambda k=key: open_url(self.window,
                                                          LINKS[k])))

        trouble = _group('Troubleshooting')
        row = Adw.ActionRow(
            title='Debug information',
            subtitle='Versions, your distribution and what this system '
                     'supports: paste it into a bug report.')
        show = Gtk.Button(label='Show', valign=Gtk.Align.CENTER)
        show.connect('clicked', self._show_debug)
        copy = Gtk.Button(icon_name='edit-copy-symbolic',
                          valign=Gtk.Align.CENTER,
                          tooltip_text='Copy debug information')
        copy.connect('clicked', self._copy_debug)
        row.add_suffix(show)
        row.add_suffix(copy)
        trouble.add(row)

        credits = _group('Credits')
        for title, sub in (
                ('Created by knightinfected', ''),
                ('Code contributions', 'Igor Baranov, OnTheLink'),
                ('Thanks to Wim Taymans', 'creator of PipeWire, for '
                 'reviewing the Server page’s settings'),
                ('Icons', 'GNOME icon-development-kit, CC0 1.0')):
            credits.add(Adw.ActionRow(title=title, subtitle=sub))
        credits.add(_link_row('License', 'GPL-3.0-or-later', None,
                              lambda: open_url(self.window,
                                               LINKS['license'])))
        privacy = _group(description='PipeWire Controller never connects to '
                                     'the internet. These links open in '
                                     'your browser.')
        return [top, support, involved, trouble, credits, privacy]

    def _debug_text(self, cb):
        notes = self._notes

        def work():
            state = {True: 'running', False: 'none running',
                     None: 'can’t tell'}[notes]
            return diagnostics.collect(
                gtk=f'{Gtk.get_major_version()}.{Gtk.get_minor_version()}.'
                    f'{Gtk.get_micro_version()}',
                adw=f'{Adw.get_major_version()}.{Adw.get_minor_version()}.'
                    f'{Adw.get_micro_version()}',
                gi_version='.'.join(str(v) for v in gi.version_info),
                notifications=state)
        async_call(work, lambda text, e: cb(text if not e else
                                            f'Could not collect it: {e}'))

    def _show_debug(self, _b):
        self._debug_text(lambda text: text_viewer_dialog(
            self, 'Debug information', text))

    def _copy_debug(self, _b):
        def done(text):
            Gdk.Display.get_default().get_clipboard().set(text)
            self._toast('Debug information copied')
        self._debug_text(done)

    # -------------------------------------------------------------- probes --
    def _probe(self):
        """What the desktop can do: login apps, notifications.  Off the UI
        thread; both ask the session bus or systemd."""
        self._notes = None

        def work():
            return autostart.session_support(), notification_service()

        def done(result, error):
            if error or result is None:
                return
            self._support, self._notes = result
            self._paint_support()
            sub = {True: 'Your desktop shows notifications.',
                   False: 'No notification service is running, so '
                          'notifications can’t appear. Install one, such as '
                          'mako, swaync or dunst.',
                   None: 'Can’t tell whether your desktop shows '
                         'notifications.'}[self._notes]
            self.test_row.set_subtitle(sub)
        async_call(work, done)
