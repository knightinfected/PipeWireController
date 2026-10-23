"""Per-device settings for a hardware endpoint, as a dialog.

The same WirePlumber node rules the Devices page always wrote (rename, hide
the endpoint or the whole card, rate, sample format, period size, headroom,
preferred quantum, suspend timeout), grouped by what they are for.  Each
change is saved the moment it is made, as before; it takes effect when
WirePlumber restarts, which the dialog's own banner offers, because the
window's banner sits behind the dialog.

Rules are written as `monitor.alsa.rules` / `monitor.bluez.rules`
(`backend/rules.py`), so this exists for hardware only: a software device's
settings live on the page that made it.

Every explanation is a tooltip, not a line under its row (the user's call,
2026-10-23: "the idea is to look clean"): a row's on the row, a group's on
the group, which is what its heading shows when hovered.  Tooltip text is
plain text, never markup, so a device name goes in unescaped.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import rules  # noqa: E402
from .widgets import async_call  # noqa: E402

# Which group each schema key belongs to, in display order.  Priority keys
# are left to App Policies, as they always were.
GROUPS = (
    ('Format', 'What the device itself runs at. Auto lets PipeWire choose.',
     ('audio.rate', 'audio.format')),
    ('Buffering', 'For crackling or dropouts: larger values are steadier, '
                  'smaller ones lower the delay.',
     ('api.alsa.period-size', 'api.alsa.headroom', 'node.latency')),
    ('Power', '', ('session.suspend-timeout-seconds',)),
)

# The hover text per row.  The schema's own sentences are shared with App
# Policies and say "0 = …" where this dialog shows "Auto", so the rows here
# say it the way they are drawn; anything not listed uses the schema's.
TIPS = {
    'audio.rate': 'A fixed rate for this device only. Auto follows the '
                  'graph.',
    'audio.format': 'Force the ALSA sample format. Auto negotiates the best '
                    'available.',
    'api.alsa.period-size': 'The ALSA period in frames: the hardware chunk '
                            'size underneath the quantum. Auto is the '
                            'driver default.',
    'api.alsa.headroom': 'Extra buffered frames. Raise it to fix crackling '
                         'USB interfaces.',
    'node.latency': 'Ask the graph for this quantum while the device is in '
                    'use, for example 256/48000. Leave it empty for no '
                    'preference.',
}


class DeviceSettingsDialog:
    """`as_dialog=False` builds only the page (`self.content`), for a harness
    to show or drive without a dialog around it."""

    def __init__(self, page, node, rule, card, card_rule, as_dialog=True):
        self.page = page
        self.window = page.window
        self.node = node
        self.updating = True
        what = 'output' if node.is_sink else 'input'
        schema = {k: (title, sub, kind, extra)
                  for k, title, sub, kind, extra in rules.DEVICE_PROP_SCHEMA}
        props = (rule or {}).get('props', {})

        self.dialog = None
        if as_dialog:
            self.dialog = Adw.PreferencesDialog(title=node.description)
            self.dialog.set_search_enabled(False)
        pg = Adw.PreferencesPage()
        self.content = pg
        pg.set_description('Saved as a WirePlumber rule for this device. '
                           'Changes apply when WirePlumber restarts.')
        self.banner = Adw.Banner(title='Saved. Restart WirePlumber to apply.',
                                 button_label='Restart now', revealed=False)
        self.banner.connect('button-clicked', self._restart)
        pg.set_banner(self.banner)

        # -- name and visibility ------------------------------------------
        g = Adw.PreferencesGroup(title='Name and visibility')
        rename = Adw.EntryRow(title='Name')
        rename.set_tooltip_text('Leave it empty to use the device’s own '
                                'name.')
        rename.set_text((rule or {}).get('rename', ''))
        rename.set_show_apply_button(True)
        rename.connect('apply', lambda r: self._save(
            rename=r.get_text().strip()))
        g.add(rename)

        hide = Adw.SwitchRow(title=f'Hide this {what}')
        hide.set_tooltip_text('It disappears from every app. Unhide it from '
                              'the Hidden list at the bottom of the Devices '
                              'page.')
        hide.set_active(bool((rule or {}).get('hide')))
        hide.connect('notify::active', lambda r, _p: (
            None if self.updating else self._save(hide=r.get_active())))
        g.add(hide)

        if card:
            card_name, card_desc = card
            card_hide = Adw.SwitchRow(title='Hide the whole sound card')
            card_hide.set_tooltip_text(
                f'Disables {card_desc} and every input and output it '
                'provides, everywhere.')
            card_hide.set_active(bool((card_rule or {}).get('hide')))
            card_hide.connect('notify::active', lambda r, _p: (
                None if self.updating else self._save_card(
                    card_name, card_desc, r.get_active())))
            g.add(card_hide)
        pg.add(g)

        # -- format, buffering, power -------------------------------------
        for title, description, keys in GROUPS:
            g = Adw.PreferencesGroup(title=title)
            if description:
                # On the group: its rows carry their own, so this one shows
                # over the heading (GTK asks the innermost widget first).
                g.set_tooltip_text(description)
            for key in keys:
                if key in schema:
                    g.add(self._prop_row(key, *schema[key], props, node))
            pg.add(g)

        # -- elsewhere ----------------------------------------------------
        g = Adw.PreferencesGroup()
        prio = Adw.ActionRow(title='Default and clock priority',
                             activatable=True)
        prio.set_tooltip_text('Which device wins automatic selection, and '
                              'which one drives the clock, are set in App '
                              'Policies.')
        # Where the arrow goes, said in two words rather than a sentence.
        where = Gtk.Label(label='App Policies', valign=Gtk.Align.CENTER)
        where.add_css_class('dim-label')
        prio.add_suffix(where)
        prio.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
        prio.connect('activated', lambda _r: (self.close(),
                                              self.window.goto('policy')))
        g.add(prio)
        pg.add(g)

        if rule:
            g = Adw.PreferencesGroup()
            reset = Adw.ButtonRow(title='Reset this device to its defaults')
            reset.add_css_class('destructive-action')
            reset.connect('activated', self._reset)
            g.add(reset)
            pg.add(g)

        if self.dialog is not None:
            self.dialog.add(pg)
        self.updating = False

    def present(self):
        self.dialog.present(self.window)

    def close(self):
        if self.dialog is not None:
            self.dialog.close()

    def toast(self, text):
        """Plain text.  The dialog's own toasts do not go through
        `Window.toast()`, so they escape here: an Adw.Toast title is markup,
        and an error with `<` in it showed as a blank toast (main's
        `toast-markup` fix covers the window's toasts only)."""
        if self.dialog is not None:
            self.dialog.add_toast(Adw.Toast(
                title=GLib.markup_escape_text(str(text))))
        else:
            self.window.toast(text)

    def _prop_row(self, key, title, subtitle, kind, extra, props, node):
        current = props.get(key)
        tip = TIPS.get(key, subtitle)
        if kind == 'enum':
            labels = [('Auto' if c in (0, 'auto') else str(c)) for c in extra]
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
            row.set_tooltip_text(tip)
            try:
                row.set_selected(extra.index(current)
                                 if current is not None else 0)
            except ValueError:
                pass
            row.connect('notify::selected', lambda r, _p: (
                None if self.updating else self._save(
                    props={key: extra[r.get_selected()]})))
            return row
        if kind == 'latency':
            # The example lives in the tooltip with the rest; the title
            # alone, as every other row here.
            row = Adw.EntryRow(title=title)
            row.set_tooltip_text(tip)
            row.set_text(str(current or ''))
            row.set_show_apply_button(True)
            row.connect('apply', lambda r: self._save(
                props={key: r.get_text().strip()}))
            return row
        lo, hi = extra
        row = Adw.SpinRow.new_with_range(lo, hi, 1)
        row.set_title(title)
        row.set_tooltip_text(tip)
        try:
            row.set_value(float(current if current is not None
                                else node.props.get(key, 0) or 0))
        except (TypeError, ValueError):
            pass
        row.connect('notify::value', lambda r, _p: (
            None if self.updating else self._save(
                props={key: int(r.get_value())})))
        return row

    # -- writing ------------------------------------------------------------
    def _saved(self):
        self.banner.set_revealed(True)
        self.window.flag_restart('wireplumber')
        self.page.rule_changed(self.node.name)

    def _save(self, rename=None, hide=None, props=None):
        node = self.node

        def work():
            rules.set_node_rule(node.name, rename=rename, hide=hide,
                                props=props, desc=node.description)
            return True

        def done(_r, error):
            if error:
                self.toast(f'Failed: {error}')
                return
            self._saved()
            if hide is not None:
                self.page.refresh()     # the Hidden list changed
        async_call(work, done)

    def _save_card(self, card_name, card_desc, hide):
        def work():
            rules.set_device_rule(card_name, hide=hide, desc=card_desc)
            return True

        def done(_r, error):
            if error:
                self.toast(f'Failed: {error}')
                return
            self._saved()
            self.page.refresh()
        async_call(work, done)

    def _reset(self, _row):
        name = self.node.name

        def done(_r, error):
            if error:
                self.toast(f'Failed: {error}')
                return
            self.window.flag_restart('wireplumber')
            self.page.rule_changed(name)
            self.window.toast('Device settings reset. Restart WirePlumber '
                              'to apply.')
            self.close()
        async_call(lambda: rules.clear_node_rule(name) or True, done)

    def _restart(self, _banner):
        self.banner.set_revealed(False)
        restart = getattr(self.window, '_restart_pending', None)
        if callable(restart):
            restart(getattr(self.window, 'banner', None))
