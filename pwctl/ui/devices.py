"""Devices page: endpoints with default selection, volume and, for hardware
devices, persistent per-device settings (rename, hide, rate, bit depth,
period size, headroom, preferred quantum, suspend timeout).

Per-device settings become WirePlumber node rules through backend.rules and
need a WirePlumber restart (the banner appears automatically).
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import prefs, pw, rules
from .volume import make_volume
from .widgets import async_call, esc, group, page_scroller, pill


class DevicesPage:
    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style')
        self.sinks = group('Outputs (sinks)',
                           'Star a device to make it the default output. '
                           'Expand a hardware device for per-device settings.')
        self.sources = group('Inputs (sources)')
        head = group('')
        refresh_row = Adw.ActionRow(
            title='Audio endpoints',
            subtitle='Includes virtual sinks created by filter chains and '
                     'virtual devices')
        btn = Gtk.Button(icon_name='view-refresh-symbolic',
                         tooltip_text='Refresh')
        btn.add_css_class('flat')
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect('clicked', lambda *_: self.refresh())
        refresh_row.add_suffix(btn)
        head.add(refresh_row)
        self.hidden = group(
            'Hidden devices',
            'A hidden endpoint or card is gone from the audio graph, so this '
            'is the only place it can still be listed. Unhiding takes effect '
            'after the WirePlumber restart.')
        self.hidden.set_visible(False)
        self.widget = page_scroller(head, self.sinks, self.sources,
                                    self.hidden)
        self._rows = []
        self._hidden_rows = []
        self.widget.connect('map', lambda *_: self.refresh())

    def refresh(self):
        def collect():
            dump = pw.pw_dump()
            return pw.list_audio_nodes(dump), pw.card_map(dump), rules.load()
        async_call(collect, self._apply)

    def _apply(self, result, error):
        if error or result is None:
            return
        nodes, cards, rule_data = result
        for row, parent in self._rows:
            parent.remove(row)
        self._rows = []
        for node in nodes:
            parent = self.sinks if node.is_sink else self.sources
            row = self._device_row(node, rule_data, cards)
            parent.add(row)
            self._rows.append((row, parent))
        self._fill_hidden({n.name for n in nodes},
                          {name for name, _desc in cards.values()})

    # ------------------------------------------------------------- row -----
    def _device_row(self, node, rule_data, cards):
        is_hw = node.name.startswith(('alsa_', 'bluez_'))
        rule = rule_data['nodes'].get(node.name, {})

        if is_hw:
            row = Adw.ExpanderRow(title=esc(node.description),
                                  subtitle=esc(node.name))
            if rule:
                row.add_suffix(pill('customized', 'warning'))
        else:
            row = Adw.ActionRow(title=esc(node.description),
                                subtitle=esc(node.name),
                                title_lines=1, subtitle_lines=1)
        icon = ('application-x-addon-symbolic' if node.is_virtual
                else 'audio-speakers-symbolic' if node.is_sink
                else 'audio-input-microphone-symbolic')
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        if node.is_virtual:
            row.add_suffix(pill('virtual', 'dim'))

        star = Gtk.Button(
            icon_name='starred-symbolic' if node.is_default
            else 'non-starred-symbolic',
            tooltip_text='Default device' if node.is_default
            else 'Make default')
        star.add_css_class('flat')
        if node.is_default:
            star.add_css_class('star-active')
        star.set_valign(Gtk.Align.CENTER)

        def make_default(_b):
            async_call(lambda: pw.set_default(node.id),
                       lambda ok, e: (self.window.toast(
                           f'Default set to {node.description}' if ok and not e
                           else 'Could not set default'), self.refresh()))
        star.connect('clicked', make_default)

        # Goes through make_volume like every other volume control, so this
        # page follows the chosen volume style and shows the live level too.
        updating = {'v': False}
        vol = make_volume(
            self.volume_style,
            lambda value: async_call(lambda: pw.set_volume(node.id, value)),
            compact=True)
        vol.set_value(node.volume if node.volume is not None else 1.0)
        vol.set_meter(node.serial)

        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                tooltip_text='Mute')
        mute.add_css_class('flat')
        mute.set_valign(Gtk.Align.CENTER)
        mute.set_active(node.muted)

        def mute_toggled(b):
            if not updating['v']:
                active = b.get_active()
                async_call(lambda: pw.set_mute(node.id, active))
        mute.connect('toggled', mute_toggled)

        row.add_suffix(vol.widget)
        row.add_suffix(mute)
        row.add_suffix(star)

        if is_hw:
            try:
                card = cards.get(int(node.props.get('device.id')))
            except (TypeError, ValueError):
                card = None
            self._add_settings_rows(row, node, rule, rule_data, card)
        return row

    # ------------------------------------------------- per-device settings --
    def _add_settings_rows(self, row, node, rule, rule_data, card):
        props = rule.get('props', {})
        updating = {'v': True}

        rename = Adw.EntryRow(title='Rename (empty = original name)')
        rename.set_text(rule.get('rename', ''))
        rename.connect('apply', lambda r: self._save(
            node, rename=r.get_text().strip()))
        rename.set_show_apply_button(True)
        row.add_row(rename)

        hide = Adw.SwitchRow(
            title='Hide this output' if node.is_sink else 'Hide this input',
            subtitle='Disables this endpoint — it disappears from every app. '
                     'The sound card itself stays listed in your desktop’s '
                     'sound settings.')
        hide.set_active(bool(rule.get('hide')))
        hide.connect('notify::active', lambda r, _p: (
            None if updating['v'] else self._save(node,
                                                  hide=r.get_active())))
        row.add_row(hide)

        if card:
            card_name, card_desc = card
            card_hide = Adw.SwitchRow(
                title='Hide the whole sound card',
                subtitle=f'Disables {esc(card_desc)} and every input and '
                         'output it provides, everywhere.')
            card_hide.set_active(
                bool(rule_data['devices'].get(card_name, {}).get('hide')))
            card_hide.connect('notify::active', lambda r, _p: (
                None if updating['v'] else self._save_card(
                    card_name, card_desc, r.get_active())))
            row.add_row(card_hide)

        for key, title, subtitle, kind, extra in rules.DEVICE_PROP_SCHEMA:
            if key in ('priority.session', 'priority.driver'):
                continue          # exposed on the Policy page instead
            current = props.get(key)
            if kind == 'enum':
                labels = [('Auto' if c in (0, 'auto') else str(c))
                          for c in extra]
                sub_row = Adw.ComboRow(title=title, subtitle=subtitle,
                                       model=Gtk.StringList.new(labels))
                try:
                    sub_row.set_selected(
                        extra.index(current) if current is not None else 0)
                except ValueError:
                    pass
                sub_row.connect(
                    'notify::selected',
                    lambda r, _p, k=key, ch=extra: (
                        None if updating['v'] else self._save(
                            node, props={k: ch[r.get_selected()]})))
            elif kind == 'latency':
                sub_row = Adw.EntryRow(title=f'{title} — {subtitle}')
                sub_row.set_text(str(current or ''))
                sub_row.set_show_apply_button(True)
                sub_row.connect('apply', lambda r, k=key: self._save(
                    node, props={k: r.get_text().strip()}))
            else:  # int
                lo, hi = extra
                sub_row = Adw.SpinRow.new_with_range(lo, hi, 1)
                sub_row.set_title(title)
                sub_row.set_subtitle(subtitle)
                try:
                    sub_row.set_value(float(
                        current if current is not None
                        else node.props.get(key, 0) or 0))
                except (TypeError, ValueError):
                    pass
                sub_row.connect(
                    'notify::value',
                    lambda r, _p, k=key: (
                        None if updating['v'] else self._save(
                            node, props={k: int(r.get_value())})))
            row.add_row(sub_row)
        updating['v'] = False

    # -------------------------------------------------------- hidden list --
    def _fill_hidden(self, live_nodes, live_cards):
        for row in self._hidden_rows:
            self.hidden.remove(row)
        self._hidden_rows = []
        entries = rules.hidden_entries()
        self.hidden.set_visible(bool(entries))
        for entry in entries:
            live = entry['key'] in (live_cards if entry['kind'] == 'device'
                                    else live_nodes)
            row = Adw.ActionRow(title=esc(entry['label']),
                                subtitle=esc(entry['key']),
                                title_lines=1, subtitle_lines=1)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                'view-conceal-symbolic'))
            if entry['kind'] == 'device':
                row.add_suffix(pill('whole card', 'dim'))
            if live:
                # Still in the graph while marked hidden: the generated
                # WirePlumber drop-in was removed or edited by hand.
                row.add_suffix(pill('not in effect', 'warning'))
            btn = Gtk.Button(label='Unhide')
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect('clicked', lambda _b, e=entry: self._unhide(e))
            row.add_suffix(btn)
            self.hidden.add(row)
            self._hidden_rows.append(row)

    def _unhide(self, entry):
        def work():
            if entry['kind'] == 'device':
                rules.set_device_rule(entry['key'], hide=False)
            else:
                rules.set_node_rule(entry['key'], hide=False)
            return True
        async_call(work, lambda r, e: (
            self.window.toast(f'Failed: {e}') if e
            else (self.window.flag_restart('wireplumber'), self.refresh())))

    def _save(self, node, rename=None, hide=None, props=None):
        def work():
            rules.set_node_rule(node.name, rename=rename, hide=hide,
                                props=props, desc=node.description)
            return True

        def done(_r, error):
            if error:
                self.window.toast(f'Failed: {error}')
                return
            self.window.flag_restart('wireplumber')
            if hide is not None:
                self.refresh()      # only a hide changes the hidden list;
                                    # rebuilding on every tweak would collapse
                                    # the expander the user is working in
        async_call(work, done)

    def _save_card(self, card_name, card_desc, hide):
        def work():
            rules.set_device_rule(card_name, hide=hide, desc=card_desc)
            return True
        async_call(work, lambda r, e: (
            self.window.toast(f'Failed: {e}') if e
            else (self.window.flag_restart('wireplumber'), self.refresh())))
