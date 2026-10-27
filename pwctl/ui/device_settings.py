"""Per-device settings for a hardware endpoint: two sections of its detail.

The same WirePlumber node rules the Devices page always wrote (rename, hide
the endpoint or the whole card, rate, sample format, period size, headroom,
preferred quantum, suspend timeout), as two groups of the detail pane:
**Name & visibility** and **Format & timing**.  Each change is saved the
moment it is made, as before, and takes effect when WirePlumber restarts:
the window's restart banner says so, and each group's heading carries an
"Applies after restart" marker.  Until 2026-10-26 this was a dialog behind a
gear; the detail pane is where a device's settings live now.

Rules are written as `monitor.alsa.rules` / `monitor.bluez.rules`
(`backend/rules.py`), so this exists for hardware only: a software device's
settings live on the page that made it.

What shows:

* **Bluetooth gets no `api.alsa.*` rows** (period size, headroom): they are
  ALSA properties and a Bluetooth node has no ALSA device under it.  A value
  already saved still shows, so it can be seen and undone.
* **Buffering and power are Advanced**, as the app's other expert settings
  are; again a value already saved shows anyway.

Every explanation is a tooltip, not a line under its row (the user's call,
2026-10-23: "the idea is to look clean").  Tooltip text is plain text, never
markup, so a device name goes in unescaped.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import rules  # noqa: E402
from .widgets import async_call  # noqa: E402

# Format & timing, in display order: what anyone may want, then what only
# matters for crackling or power (Advanced).  Priority keys are left to App
# Policies, as they always were.
BASIC_KEYS = ('audio.rate', 'audio.format', 'node.latency')
ADVANCED_KEYS = ('api.alsa.period-size', 'api.alsa.headroom',
                 'session.suspend-timeout-seconds')
ALSA_ONLY = ('api.alsa.period-size', 'api.alsa.headroom')

# The hover text per row.  The schema's own sentences are shared with App
# Policies and say "0 = …" where these rows show "Auto", so the rows here
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

RESTART_TIP = ('Saved as a WirePlumber rule for this device. It takes '
               'effect when WirePlumber restarts.')


def restart_marker():
    """'⟳ Applies after restart', for a group heading's suffix."""
    box = Gtk.Box(spacing=5, valign=Gtk.Align.END)
    box.add_css_class('devl-restart-mark')
    icon = Gtk.Image.new_from_icon_name('view-refresh-symbolic')
    icon.set_pixel_size(14)
    box.append(icon)
    lbl = Gtk.Label(label='Applies after restart')
    lbl.add_css_class('caption')
    box.append(lbl)
    box.set_tooltip_text(RESTART_TIP)
    return box


class DeviceSettings:
    """The two groups for one hardware endpoint.

    `groups` is what the detail appends.  `set_advanced()` shows or hides
    the Advanced rows; the detail calls it when the sidebar switch moves.
    """

    def __init__(self, page, node, rule, card, card_rule, bluetooth=False):
        self.page = page
        self.window = page.window
        self.node = node
        self.updating = True
        self._advanced = []         # rows shown only with Advanced on
        what = 'output' if node.is_sink else 'input'
        schema = {k: (title, sub, kind, extra)
                  for k, title, sub, kind, extra in rules.DEVICE_PROP_SCHEMA}
        props = (rule or {}).get('props', {})

        # -- name and visibility ------------------------------------------
        # Titles are Pango markup: an unescaped '&' blanks the heading.
        g = Adw.PreferencesGroup(title=GLib.markup_escape_text(
            'Name & visibility'))
        g.set_header_suffix(restart_marker())
        rename = Adw.EntryRow(title='Name')
        rename.set_tooltip_text(f'Leave it empty to use the device’s own '
                                f'name, “{node.description}”.')
        rename.set_text((rule or {}).get('rename', ''))
        rename.set_show_apply_button(True)
        rename.connect('apply', lambda r: self._save(
            rename=r.get_text().strip()))
        g.add(rename)
        self.rename = rename

        hide = Adw.SwitchRow(title=f'Hide this {what}')
        hide.set_tooltip_text('It disappears from every app. Unhide it from '
                              'Hidden devices at the bottom of the list.')
        hide.set_active(bool((rule or {}).get('hide')))
        hide.connect('notify::active', lambda r, _p: (
            None if self.updating else self._save(hide=r.get_active())))
        g.add(hide)
        self.hide = hide

        self.card_hide = None
        if card:
            card_name, card_desc = card
            word = 'headset' if bluetooth else 'sound card'
            card_hide = Adw.SwitchRow(title=f'Hide the whole {word}')
            card_hide.set_tooltip_text(
                f'Disables {card_desc} and every input and output it '
                'provides, everywhere.')
            card_hide.set_active(bool((card_rule or {}).get('hide')))
            card_hide.connect('notify::active', lambda r, _p: (
                None if self.updating else self._save_card(
                    card_name, card_desc, r.get_active())))
            g.add(card_hide)
            self.card_hide = card_hide
        self.identity = g

        # -- format and timing --------------------------------------------
        g = Adw.PreferencesGroup(title=GLib.markup_escape_text(
            'Format & timing'))
        g.set_header_suffix(restart_marker())
        self.rows = {}
        for key in BASIC_KEYS + ADVANCED_KEYS:
            if key not in schema:
                continue
            saved = key in props
            if bluetooth and key in ALSA_ONLY and not saved:
                continue
            row = self._prop_row(key, *schema[key], props, node)
            self.rows[key] = row
            g.add(row)
            if key in ADVANCED_KEYS and not saved:
                self._advanced.append(row)

        prio = Adw.ActionRow(title='Default and clock priority',
                             activatable=True)
        prio.set_tooltip_text('Which device wins automatic selection, and '
                              'which one drives the clock, are set in App '
                              'Policies.')
        where = Gtk.Label(label='App Policies', valign=Gtk.Align.CENTER)
        where.add_css_class('dim-label')
        prio.add_suffix(where)
        prio.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
        prio.connect('activated', lambda _r: self.window.goto('policy'))
        g.add(prio)
        self._advanced.append(prio)
        self.format = g
        hidden = [k for k, r in self.rows.items() if r in self._advanced]
        what = [w for w, keys in (('buffering', ALSA_ONLY),
                                  ('power', ('session.suspend-timeout-'
                                             'seconds',)))
                if any(k in hidden for k in keys)]
        self._hint = (f'Turn on Advanced for {" and ".join(what)}.'
                      if what else '')
        self.groups = [self.identity, self.format]
        self.updating = False
        self.set_advanced(bool(getattr(self.window, 'advanced', False)))

    def set_advanced(self, on):
        for row in self._advanced:
            row.set_visible(on)
        self.format.set_description(None if on or not self._hint
                                    else self._hint)

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
                self.window.toast(f'Failed: {error}')
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
                self.window.toast(f'Failed: {error}')
                return
            self._saved()
            self.page.refresh()
        async_call(work, done)


def reset_device(page, node):
    """Clear every rule of this endpoint, then show its defaults again."""
    name = node.name
    window = page.window

    def done(_r, error):
        if error:
            window.toast(f'Failed: {error}')
            return
        window.flag_restart('wireplumber')
        page.rule_changed(name)
        page.reload_detail()
        window.toast('Device settings reset. Restart WirePlumber to apply.')
    async_call(lambda: rules.clear_node_rule(name) or True, done)
