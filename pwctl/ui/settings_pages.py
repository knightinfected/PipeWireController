"""Server, Streams and WirePlumber settings pages, driven by the schema."""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import bluetooth, config, pw, schema
from .widgets import (async_call, group, icon_button, page_scroller,
                      prompt_number)

# both native and pulse clients get the same stream defaults
STREAM_CONFS = ('client.conf', 'pipewire-pulse.conf')

CUSTOM_LABEL = 'Custom…'
# PipeWire clamps any quantum to CLOCK_QUANTUM_LIMIT (2^16) internally.
QUANTUM_MAX = 65536


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
        cur = self._peek()
        if s.kind == 'bool':
            row = Adw.SwitchRow(title=s.title, subtitle=s.subtitle)
            row.set_active(bool(cur) if not isinstance(cur, str)
                           else cur == 'true')
            row.connect('notify::active', self._changed_bool)
        elif s.kind == 'enum':
            labels, sel = self._enum_model(cur)
            row = Adw.ComboRow(title=s.title, subtitle=s.subtitle,
                               model=Gtk.StringList.new(labels))
            row.set_selected(sel)
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

    def _enum_model(self, cur):
        """Labels + selected index for an enum row.

        For custom-enabled rows the model gains the current out-of-list value
        (as an "N (custom)" item) and a trailing "Custom…" entry.  Records the
        shown custom value in self._custom_val so _changed_enum can tell the
        three item classes (preset / current-custom / Custom…) apart.
        """
        coerced = self._coerce(cur)
        labels = [str(c) for c in self.s.choices]
        self._custom_val = None
        if not self.s.custom:
            try:
                return labels, self.s.choices.index(coerced)
            except ValueError:
                return labels, 0
        if coerced in self.s.choices:
            sel = self.s.choices.index(coerced)
        elif isinstance(coerced, int):
            self._custom_val = coerced
            labels.append(f'{coerced} (custom)')
            sel = len(labels) - 1
        else:
            sel = 0
        labels.append(CUSTOM_LABEL)
        return labels, sel

    def _rebuild_enum_model(self, val):
        """Swap the ComboRow model to reflect val (custom enums only)."""
        labels, sel = self._enum_model(val)
        self.row.set_model(Gtk.StringList.new(labels))
        self.row.set_selected(sel)

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
            if s.custom:
                self._rebuild_enum_model(val)
            else:
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
        if self.updating:
            return
        sel = row.get_selected()
        if not self.s.custom:
            self.write(self.s.choices[sel])
            return
        n = len(self.s.choices)
        if sel < n:
            self.write(self.s.choices[sel])
        elif self._custom_val is not None and sel == n:
            return                      # re-picked the current custom value
        else:
            self._prompt_custom()       # the trailing "Custom…" item

    def _prompt_custom(self):
        # Revert the combo off "Custom…" back to the persisted value; we only
        # re-select on a valid accept.
        cur = self._coerce(self.current()[0])
        self.updating = True
        try:
            self._rebuild_enum_model(self.current()[0])
        finally:
            self.updating = False
        initial = cur if isinstance(cur, int) else self.s.default
        prompt_number(
            self.window, f'Custom {self.s.title.lower()}',
            'Frames per cycle (1–65536). Non-power-of-two values are '
            'allowed but get rounded down when “Power-of-two quantum” is on.',
            initial, self._accept_custom)

    def _accept_custom(self, text):
        try:
            val = int(text.strip())
        except (TypeError, ValueError):
            self.window.toast('Not a whole number — nothing changed')
            return
        clamped = max(1, min(QUANTUM_MAX, val))
        self.updating = True
        try:
            self._rebuild_enum_model(clamped)
        finally:
            self.updating = False
        self.write(clamped)
        if clamped != val:
            self.window.toast(f'Clamped to {clamped} (PipeWire limit)')
        elif clamped & (clamped - 1):
            self.window.toast(f'Set to {clamped} (not a power of two)')

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

        self.bt_devs = group(
            'Connected Bluetooth devices',
            'Profile switches instantly through WirePlumber; the codec list '
            'shows what the device supports in its current profile.')
        self._bt_rows = []
        self.widget = page_scroller(power, bt, self.bt_devs)
        self.widget.connect('map', lambda *_: self.refresh_bt())

    def _toggled(self, row, _p, key):
        if self.updating:
            return
        state = config.read_wp_toggles()
        state[key] = row.get_active()
        config.write_wp_toggles(state)
        self.window.flag_restart('wireplumber')

    # ------------------------------------------------- per-device bluetooth --
    def refresh_bt(self):
        async_call(bluetooth.list_devices, self._apply_bt)

    def _apply_bt(self, devices, error):
        if error or devices is None:
            return
        for row in self._bt_rows:
            self.bt_devs.remove(row)
        self._bt_rows = []
        if not devices:
            row = Adw.ActionRow(
                title='No Bluetooth audio devices connected',
                subtitle='Pair and connect a device; it appears here with '
                         'its profile and codec options.')
            self.bt_devs.add(row)
            self._bt_rows.append(row)
            return
        for dev in devices:
            self._bt_rows.append(self._bt_device_rows(dev))

    def _bt_device_rows(self, dev):
        exp = Adw.ExpanderRow(
            title=dev.description,
            subtitle=dev.name + (f' · battery {dev.battery}' if dev.battery
                                 else ''))
        exp.set_expanded(True)
        exp.add_prefix(Gtk.Image.new_from_icon_name(
            'bluetooth-active-symbolic'))

        profile = Adw.ComboRow(
            title='Profile',
            subtitle='A2DP for listening quality; HFP/HSP when the '
                     'microphone is needed.')
        labels = [desc + (' (unavailable)' if avail == 'no' else '')
                  for _i, desc, avail in dev.profiles]
        profile.set_model(Gtk.StringList.new(labels))
        idx = next((i for i, (pidx, _d, _a) in enumerate(dev.profiles)
                    if pidx == dev.active_profile), None)
        updating = {'v': True}
        if idx is not None:
            profile.set_selected(idx)

        def profile_changed(row, _p):
            if updating['v']:
                return
            sel = row.get_selected()
            if sel >= len(dev.profiles):
                return
            pidx, desc, _a = dev.profiles[sel]
            async_call(lambda: bluetooth.set_profile(dev.id, pidx),
                       lambda ok, e: (self.window.toast(
                           f'Profile: {desc}' if ok and not e
                           else 'Profile change failed'),
                           self.refresh_bt()))
        profile.connect('notify::selected', profile_changed)
        exp.add_row(profile)

        codec = Adw.ComboRow(
            title='Codec',
            subtitle='Higher-quality codecs need support on both ends.')
        if dev.codecs:
            codec.set_model(Gtk.StringList.new(
                [f'{desc} ({name})' if desc != name else name
                 for name, desc in dev.codecs]))
            cur = next((i for i, (name, _d) in enumerate(dev.codecs)
                        if name == dev.active_codec), None)
            if cur is not None:
                codec.set_selected(cur)

            def codec_changed(row, _p):
                if updating['v']:
                    return
                sel = row.get_selected()
                if sel >= len(dev.codecs):
                    return
                name = dev.codecs[sel][0]
                async_call(lambda: bluetooth.switch_codec(dev.name, name),
                           lambda res, e: (self.window.toast(
                               f'Codec: {name}' if res and res[0]
                               else 'Codec switch failed — the device may '
                                    'not support it in this profile'),
                               self.refresh_bt()))
            codec.connect('notify::selected', codec_changed)
        else:
            codec.set_subtitle('Codec switching not available for this '
                               'device/profile')
            codec.set_sensitive(False)
        exp.add_row(codec)
        updating['v'] = False
        self.bt_devs.add(exp)
        return exp
