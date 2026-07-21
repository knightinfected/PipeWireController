"""Server, Streams and WirePlumber settings pages, driven by the schema."""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import config, pw, schema
from .widgets import group, icon_button, page_scroller

# both native and pulse clients get the same stream defaults
STREAM_CONFS = ('client.conf', 'pipewire-pulse.conf')


class SettingRow:
    """One schema Setting bound to an Adwaita row with override tracking."""

    def __init__(self, setting: schema.Setting, window):
        self.s = setting
        self.window = window
        self.updating = False
        self.row = self._build()
        self.reset_btn = icon_button(
            'edit-undo-symbolic', 'Reset to default', self._on_reset)
        self.row.add_suffix(self.reset_btn)
        self.refresh()

    # ---- value plumbing ----
    def _confs(self):
        if self.s.conf == 'client.conf':
            return STREAM_CONFS
        return (self.s.conf,)

    def current(self):
        ov = config.get_override(self._confs()[0], self.s.section, self.s.key)
        if ov is not None:
            return ov, True
        merged = config.read_merged_section(self._confs()[0], self.s.section)
        val = merged.get(self.s.key)
        # ignore our own dropin already counted; merged includes it, but if
        # override existed we returned above, so a merged hit here is foreign
        if val is not None:
            return val, False
        return self.s.default, False

    def write(self, value):
        for conf in self._confs():
            config.set_override(conf, self.s.section, self.s.key, value)
        self.window.flag_restart(self.s.restart)
        self.refresh()

    def _on_reset(self, _b):
        for conf in self._confs():
            config.set_override(conf, self.s.section, self.s.key, None)
        self.window.flag_restart(self.s.restart)
        self.updating = True
        try:
            self._set_widget(self.current()[0])
        finally:
            self.updating = False
        self.refresh()

    def refresh(self):
        _, overridden = self.current()
        self.reset_btn.set_visible(overridden)
        if overridden:
            self.row.add_css_class('overridden')
        else:
            self.row.remove_css_class('overridden')

    # ---- widgets ----
    def _build(self):
        s = self.s
        val, _ = (None, None)
        cur = self._peek()
        if s.kind == 'bool':
            row = Adw.SwitchRow(title=s.title, subtitle=s.subtitle)
            row.set_active(bool(cur) if not isinstance(cur, str)
                           else cur == 'true')
            row.connect('notify::active', self._changed_bool)
        elif s.kind == 'enum':
            labels = [str(c) for c in s.choices]
            row = Adw.ComboRow(title=s.title, subtitle=s.subtitle,
                               model=Gtk.StringList.new(labels))
            try:
                row.set_selected(s.choices.index(self._coerce(cur)))
            except ValueError:
                pass
            row.connect('notify::selected', self._changed_enum)
        elif s.kind in ('int', 'float'):
            adj = Gtk.Adjustment(lower=s.min, upper=s.max,
                                 step_increment=s.step, page_increment=s.step)
            row = Adw.SpinRow(title=s.title, subtitle=s.subtitle,
                              adjustment=adj,
                              digits=0 if s.kind == 'int' else 1)
            try:
                row.set_value(float(cur))
            except (TypeError, ValueError):
                row.set_value(float(s.default))
            row.connect('notify::value', self._changed_spin)
        elif s.kind == 'rates':
            row = Adw.ExpanderRow(title=s.title, subtitle=s.subtitle)
            self.checks = {}
            active = self._rates_value(cur)
            for rate in schema.RATES:
                cb = Adw.SwitchRow(title=f'{rate} Hz')
                cb.set_active(rate in active)
                cb.connect('notify::active', self._changed_rates)
                row.add_row(cb)
                self.checks[rate] = cb
        else:
            row = Adw.ActionRow(title=s.title, subtitle=s.subtitle)
        return row

    def _peek(self):
        ov = config.get_override(self._confs()[0], self.s.section, self.s.key)
        if ov is not None:
            return ov
        merged = config.read_merged_section(self._confs()[0], self.s.section)
        return merged.get(self.s.key, self.s.default)

    def _coerce(self, val):
        if self.s.choices and isinstance(self.s.choices[0], int):
            try:
                return int(val)
            except (TypeError, ValueError):
                return val
        return str(val)

    @staticmethod
    def _rates_value(val):
        if isinstance(val, list):
            return [int(x) for x in val]
        if isinstance(val, str):
            return [int(x) for x in val.strip('[] ').split() if x.isdigit()]
        return []

    def _set_widget(self, val):
        s = self.s
        if s.kind == 'bool':
            self.row.set_active(val is True or val == 'true')
        elif s.kind == 'enum':
            try:
                self.row.set_selected(s.choices.index(self._coerce(val)))
            except ValueError:
                pass
        elif s.kind in ('int', 'float'):
            try:
                self.row.set_value(float(val))
            except (TypeError, ValueError):
                pass
        elif s.kind == 'rates':
            active = self._rates_value(val)
            for rate, cb in self.checks.items():
                cb.set_active(rate in active)

    # ---- change handlers ----
    def _changed_bool(self, row, _p):
        if not self.updating:
            self.write(row.get_active())

    def _changed_enum(self, row, _p):
        if not self.updating:
            self.write(self.s.choices[row.get_selected()])

    def _changed_spin(self, row, _p):
        if self.updating:
            return
        val = row.get_value()
        self.write(int(val) if self.s.kind == 'int' else round(val, 2))

    def _changed_rates(self, *_a):
        if self.updating:
            return
        rates = [r for r, cb in self.checks.items() if cb.get_active()]
        self.write(rates if rates else None)


def _schema_group(title, desc, settings, window):
    g = group(title, desc)
    for s in settings:
        row = SettingRow(s, window).row
        g.add(row)
        if s.advanced:
            window.register_advanced(row)
    if all(s.advanced for s in settings):
        window.register_advanced(g)
    return g


# ------------------------------------------------------------ server page --

class ServerPage:
    def __init__(self, window):
        self.window = window
        self.runtime_rows = {}

        runtime = group(
            'Runtime overrides (instant, lost on restart)',
            'Applied immediately through the settings metadata — perfect for '
            'experiments before making anything permanent.')
        self.force_rate = Adw.ComboRow(
            title='Force sample rate',
            subtitle='Locks the graph rate right now, overriding everything.',
            model=Gtk.StringList.new(['Off'] + [str(r) for r in schema.RATES]))
        self.force_rate.connect('notify::selected', self._force_rate)
        runtime.add(self.force_rate)

        quanta = [str(q) for q in schema.QUANTA]
        self.force_quantum = Adw.ComboRow(
            title='Force quantum',
            subtitle='Locks the buffer size right now. Great for testing how '
                     'low your hardware can go before xruns.',
            model=Gtk.StringList.new(['Off'] + quanta))
        self.force_quantum.connect('notify::selected', self._force_quantum)
        runtime.add(self.force_quantum)

        clock = _schema_group(
            'Clock and buffering (persistent)',
            'Written to a drop-in in ~/.config/pipewire/pipewire.conf.d — '
            'takes effect after a PipeWire restart.',
            schema.PIPEWIRE_CLOCK, window)
        adv = _schema_group(
            'Scheduling and tuning', '',
            schema.PIPEWIRE_ADVANCED, window)

        self.widget = page_scroller(runtime, clock, adv)
        self.updating = False
        self.widget.connect('map', lambda *_: self.refresh())

    def refresh(self):
        vals = pw.read_settings()
        self.updating = True
        try:
            fr = int(vals.get('clock.force-rate', '0') or 0)
            fq = int(vals.get('clock.force-quantum', '0') or 0)
            self.force_rate.set_selected(
                schema.RATES.index(fr) + 1 if fr in schema.RATES else 0)
            self.force_quantum.set_selected(
                schema.QUANTA.index(fq) + 1 if fq in schema.QUANTA else 0)
        finally:
            self.updating = False

    def _force_rate(self, row, _p):
        if self.updating:
            return
        idx = row.get_selected()
        val = 0 if idx == 0 else schema.RATES[idx - 1]
        pw.set_setting('clock.force-rate', val)
        self.window.toast(f'Graph rate {"unlocked" if not val else f"forced to {val} Hz"}')

    def _force_quantum(self, row, _p):
        if self.updating:
            return
        idx = row.get_selected()
        val = 0 if idx == 0 else schema.QUANTA[idx - 1]
        pw.set_setting('clock.force-quantum', val)
        self.window.toast(f'Quantum {"unlocked" if not val else f"forced to {val}"}')


# ----------------------------------------------------------- streams page --

class StreamsPage:
    def __init__(self, window):
        g = _schema_group(
            'Stream processing defaults',
            'Applied to every new native and PulseAudio stream (written to '
            'client.conf.d and pipewire-pulse.conf.d drop-ins). Restart '
            'PipeWire-Pulse and reopen apps to apply.',
            schema.STREAM, window)
        self.widget = page_scroller(g)


# ------------------------------------------------------- wireplumber page --

class WirePlumberPage:
    def __init__(self, window):
        self.window = window
        self.updating = False
        self.rows = {}
        power = group('Device power',
                      'Session-manager policy, written to '
                      '~/.config/wireplumber/wireplumber.conf.d.')
        bt = group('Bluetooth audio')
        state = config.read_wp_toggles()
        for key, title, subtitle in schema.WIREPLUMBER:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(bool(state.get(key)))
            row.connect('notify::active', self._toggled, key)
            self.rows[key] = row
            in_power = key in ('disable_suspend', 'alsa_headroom')
            (power if in_power else bt).add(row)
            if key == 'alsa_headroom':
                window.register_advanced(row)
        self.widget = page_scroller(power, bt)

    def _toggled(self, row, _p, key):
        if self.updating:
            return
        state = config.read_wp_toggles()
        state[key] = row.get_active()
        config.write_wp_toggles(state)
        self.window.flag_restart('wireplumber')
