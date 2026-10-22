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
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import balance, livestate, prefs, pw, rules
from .device_levels import sync_device_levels
from .volume import ChannelPanel, make_volume
from .widgets import (LOCAL_GRACE, async_call, device_icon, esc, group,
                      micro, page_scroller, pill)


class DevicesPage:
    _POLL_SECONDS = 3       # only when livestate cannot listen

    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style')
        self.sinks = group('Outputs (sinks)',
                           'Star a device to make it the default output. '
                           'Expand a device for its balance and channel '
                           'levels, and a hardware device for its settings.')
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
        self._live_controls = {}
        self._poll_timer = None
        self._poll_busy = False
        self._live = None               # livestate subscription while mapped
        self._rebuild_next = False      # the next delivery rebuilds the rows
        self._last_nodes = None         # what the last delivery said
        self._grace_id = None
        self._default_ts = 0.0          # when the user last clicked a star
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

    def _on_map(self, *_args):
        # The graph comes from livestate when something changes; the 3 s
        # poll is only for a machine where it cannot listen.
        if self._live is None:
            self._live = livestate.subscribe(self._on_graph)
        if self._live is not None:
            self._rebuild_next = True       # its first delivery builds the rows
            return
        self.refresh()
        if self._poll_timer is None:
            self._poll_timer = GLib.timeout_add_seconds(
                self._POLL_SECONDS, self._poll_levels)

    def _on_unmap(self, *_args):
        if self._live is not None:
            livestate.unsubscribe(self._live)
            self._live = None
        if self._grace_id is not None:
            GLib.source_remove(self._grace_id)
            self._grace_id = None
        if self._poll_timer is not None:
            GLib.source_remove(self._poll_timer)
            self._poll_timer = None

    def _on_graph(self, dump):
        """A fresh pw-dump from livestate: the graph changed, or we asked.

        Rows are updated in place from it, or rebuilt from the same dump, on
        the main loop and in delivery order, so an older read can never land
        on top of a newer one.
        """
        nodes = pw.list_audio_nodes(dump)
        self._last_nodes = nodes
        if self._rebuild_next or not self._sync(nodes):
            self._rebuild_next = False
            self._apply((nodes, pw.card_map(dump), rules.load()), None)
        self._watch_grace()

    def _sync(self, nodes):
        now = GLib.get_monotonic_time() / 1e6
        return sync_device_levels(
            self._live_controls, nodes, now, LOCAL_GRACE,
            default_held=now - self._default_ts < LOCAL_GRACE)

    def _watch_grace(self):
        """Apply the last delivery again once every hold has run out.

        A delivery that lands while a slider is being dragged, or just after
        a star click, leaves that control alone.  The poll's next tick used to
        correct it; with events there may be no next delivery.
        """
        if self._grace_id is not None:
            GLib.source_remove(self._grace_id)
            self._grace_id = None
        if self._live is None:
            return                          # the poll's next tick does it
        holds = [st['local_ts'] for st in self._live_controls.values()]
        until = max(holds + [self._default_ts]) + LOCAL_GRACE
        left = until - GLib.get_monotonic_time() / 1e6
        if left > 0:
            self._grace_id = GLib.timeout_add(int(left * 1000) + 50,
                                              self._grace_over)

    def _grace_over(self):
        self._grace_id = None
        self._resync()
        return GLib.SOURCE_REMOVE

    def _resync(self):
        """Put every control back where the graph has it."""
        if self._live is None:
            self._poll_levels()
            return
        if self._last_nodes is not None and not self._sync(self._last_nodes):
            self.refresh()
        self._watch_grace()

    def _poll_levels(self):
        if self._poll_busy:
            return True
        self._poll_busy = True
        async_call(lambda: pw.list_audio_nodes(pw.pw_dump()),
                   self._apply_polled_levels)
        return True

    def _apply_polled_levels(self, nodes, error):
        self._poll_busy = False
        if error or nodes is None:
            return

        if not self._sync(nodes):
            # A node appeared, vanished or was recreated. Rebuild once so its
            # callbacks target the current PipeWire id; ordinary volume polls
            # keep the existing rows and their expanded state intact.
            self.refresh()

    def refresh(self):
        if self._live is not None:
            # Through livestate, so the rebuild stays in order with its
            # deliveries.  Rules are read when the dump lands.
            self._rebuild_next = True
            livestate.refresh()
            return

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
        self._live_controls = {}
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
        # Anything with two or more channels expands too, to show them; on
        # hardware the channels sit above the per-device settings.
        multichannel = len(node.channels) > 1

        if is_hw or multichannel:
            row = Adw.ExpanderRow(title=esc(node.description),
                                  subtitle=esc(node.name))
            if not is_hw:
                # These were ActionRows with one line each before they got
                # channels; without it the two-column layout letter-wraps
                # "Surround HRTFdb" one letter per line (UI.md; seen on the
                # live graph 2026-10-22).  Hardware rows keep the two-line
                # wrap they always had.
                row.set_title_lines(1)
                row.set_subtitle_lines(1)
            if rule:
                row.add_suffix(pill('customized', 'warning'))
        else:
            row = Adw.ActionRow(title=esc(node.description),
                                subtitle=esc(node.name),
                                title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(device_icon(node)))
        if node.is_virtual:
            row.add_suffix(pill('virtual', 'dim'))
            kind = node.software_kind
            if kind:
                row.add_suffix(pill(kind, 'dim'))

        star = Gtk.Button()
        star.add_css_class('flat')
        star.set_valign(Gtk.Align.CENTER)

        def paint_star(is_default):
            star.set_icon_name('starred-symbolic' if is_default
                               else 'non-starred-symbolic')
            star.set_tooltip_text('Default device' if is_default
                                  else 'Make default')
            if is_default:
                star.add_css_class('star-active')
            else:
                star.remove_css_class('star-active')
        paint_star(node.is_default)

        def make_default(_b):
            # Paint it now and hold it for LOCAL_GRACE: the graph only shows
            # the new default a little after wpctl returns, so a poll landing
            # in between would put the star back.  Repainting in place, not
            # rebuilding, keeps an expanded row open.
            self._default_ts = GLib.get_monotonic_time() / 1e6
            for other in self._live_controls.values():
                if other['is_sink'] == node.is_sink:
                    other['star'](other['star'] is paint_star)

            def done(ok, error):
                if ok and not error:
                    self.window.toast(f'Default set to {node.description}')
                    return
                self.window.toast('Could not set default')
                self._default_ts = 0.0
                self._resync()          # put the star back where it really is
            async_call(lambda: pw.set_default(node.id), done)
        star.connect('clicked', make_default)

        # Goes through make_volume like every other volume control, so this
        # page follows the chosen volume style and shows the live level too.
        live = {
            'volume': None,
            'mute': None,
            'star': paint_star,
            'is_sink': node.is_sink,
            'updating': False,
            'local_ts': 0.0,
            # the row's one copy of the channels: the master and the channel
            # panel both act on it, so neither works from stale levels
            'levels': balance.Levels(node.channels, node.channel_map),
            'panel': None,
        }
        levels = live['levels']

        def touch():
            live['local_ts'] = GLib.get_monotonic_time() / 1e6

        def write(values):
            touch()
            nid, serial, mclass = node.id, node.serial, node.media_class
            async_call(lambda: pw.set_channel_volumes(
                nid, values, serial=serial, media_class=mclass))

        def set_volume(value):
            # scales every channel, so a balance survives the master slider
            write(levels.set_master(value))
            if live['panel'] is not None:
                live['panel'].refresh()

        vol = make_volume(
            self.volume_style,
            set_volume,
            compact=True)
        vol.set_value(node.volume if node.volume is not None else 1.0)
        vol.set_meter(node.serial)

        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                tooltip_text='Mute')
        mute.add_css_class('flat')
        mute.set_valign(Gtk.Align.CENTER)
        mute.set_active(node.muted)

        def mute_toggled(b):
            if not live['updating']:
                touch()
                active = b.get_active()
                async_call(lambda: pw.set_mute(node.id, active))
        mute.connect('toggled', mute_toggled)

        row.add_suffix(vol.widget)
        row.add_suffix(mute)
        row.add_suffix(star)
        live['volume'] = vol
        live['mute'] = mute
        self._live_controls[(node.id, node.serial)] = live

        if multichannel:
            def channels_written(values):
                write(values)
                vol.set_value(levels.master)

            # The box goes in now, so the channels stay above the settings
            # rows; the panel itself is built on first expand, as the Mixer
            # does.  Built eagerly it nearly doubled a rebuild (27 -> 49 ms,
            # 641 -> 1192 widgets for 8 stereo devices, measured 2026-10-22).
            # `levels` follows every delivery either way, so a late panel
            # opens on current values.
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          margin_top=10, margin_bottom=12,
                          margin_start=12, margin_end=12)
            box.append(micro('Channel levels'))
            row.add_row(box)

            def build_panel(*_a):
                if live['panel'] is not None or not row.get_expanded():
                    return
                panel = ChannelPanel(levels, self.volume_style,
                                     channels_written, compact=True)
                live['panel'] = panel
                box.append(panel.widget)
            row.connect('notify::expanded', build_panel)

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
