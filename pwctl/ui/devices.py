"""Devices page: every output and input, as a card you can operate.

Each endpoint is a `device_card.DeviceCard`: level and mute, the default
star, port and card profile, what is playing into it, where a software
device sends its sound, its state and running format, a test sound, and
for hardware the per-device settings (now a dialog, `device_settings`).
A sound card that gives nothing in a direction (Off, or output-only seen
from Inputs) gets a `CardOffCard`, so the way back is always on the page.

Layout: one `ColumnBox`, with Outputs, Inputs and Hidden as full-width bands
and the cards in newspaper columns under each, hardware first.  The column
floor is the cards' own minimum (`ColumnBox._content_floor`), so no width
can squeeze a card.

Live state: the graph comes from `livestate` while the page is mapped (one
fresh `pw-dump` per real change), the 3 s poll only where it cannot listen.
Every delivery is applied in place (`device_levels.sync_device_levels` for
level, mute and star, `DeviceCard.paint` for the rest); cards are rebuilt
only when the set of endpoints changes, so meters are never respawned and an
open popover survives audio starting elsewhere.

Per-device settings become WirePlumber node rules through backend.rules and
need a WirePlumber restart (the banner appears automatically).
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import (chains, endpoints, livestate, prefs, pw, reroute,  # noqa: E402
                       rules, surround)
from .device_card import CardOffCard, DeviceCard  # noqa: E402
from .device_levels import sync_device_levels  # noqa: E402
from .device_settings import DeviceSettingsDialog  # noqa: E402
from .widgets import (LOCAL_GRACE, ColumnBox, async_call, esc,  # noqa: E402
                      micro, pill)


def _now():
    return GLib.get_monotonic_time() / 1e6


def collect(dump):
    """Everything one delivery needs, out of one dump."""
    return {'nodes': pw.list_audio_nodes(dump),
            'streams': pw.list_streams(dump),
            'cards': surround.list_cards(dump, outputs_only=False)}


def _order(nodes, sink):
    """Hardware first, then software, each by name; stable across deliveries."""
    mine = [n for n in nodes if n.is_sink == sink]
    return sorted(mine, key=lambda n: (n.is_virtual, n.description.lower(),
                                       n.name, n.id))


class _Band:
    """A section heading across every column: OUTPUTS · 6."""

    def __init__(self, title):
        self.widget = Gtk.Box(spacing=10)
        self.widget.add_css_class('dev-band')
        self.title = micro(title)
        self.title.add_css_class('dev-band-title')
        self.count = Gtk.Label(valign=Gtk.Align.CENTER)    # not the row's
        self.count.add_css_class('dev-band-count')        # height (Hidden)
        self.widget.append(self.title)
        self.widget.append(self.count)

    def set_count(self, shown, total):
        self.count.set_label(str(total) if shown == total
                             else f'{shown} of {total}')


class DevicesPage:
    _POLL_SECONDS = 3       # only when livestate cannot listen

    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style')
        self.chain_templates = {}
        self.label_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        self._cards = {}                # key -> DeviceCard | CardOffCard
        self._order = []                # keys in display order
        self._live_controls = {}        # (id, serial) -> card.live
        self._hidden_rows = []
        self._data = None               # what the last delivery said
        self._poll_timer = None
        self._poll_busy = False
        self._live = None               # livestate subscription while mapped
        self._rebuild_next = False      # the next delivery rebuilds the cards
        self._grace_id = None
        self._default_ts = 0.0          # when the user last clicked a star
        self.dragging = None            # the stream an app chip is carrying
        self._held_rebuild = False      # a rebuild that waited for the drop

        self.box = ColumnBox(spacing=16, max_columns=3)
        self.box.set_margin_top(18)
        self.box.set_margin_bottom(32)
        self.box.set_margin_start(16)
        self.box.set_margin_end(16)

        self._build_toolbar()
        self.out_band = _Band('Outputs')
        self.in_band = _Band('Inputs')
        self._build_hidden()
        self.box.append(self.toolbar, span=True)

        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vexpand=True)
        sw.set_child(self.box)
        self.widget = sw
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

    # ------------------------------------------------------------ chrome --
    def _build_toolbar(self):
        bar = Gtk.Box(spacing=8)
        bar.add_css_class('dev-toolbar')
        self.search = Gtk.SearchEntry(placeholder_text='Filter devices',
                                      hexpand=True)
        self.search.set_max_width_chars(28)
        self.search.connect('search-changed', lambda *_a: self._filter())
        bar.append(self.search)
        self.summary = Gtk.Label(xalign=1, hexpand=True)
        self.summary.add_css_class('dim-label')
        self.summary.add_css_class('caption')
        bar.append(self.summary)
        btn = Gtk.Button(icon_name='view-refresh-symbolic',
                         tooltip_text='Read the devices again')
        btn.add_css_class('flat')
        btn.connect('clicked', lambda *_a: self.refresh())
        bar.append(btn)
        self.toolbar = bar

    def _build_hidden(self):
        self.hidden = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.hidden_band = _Band('Hidden')
        head = self.hidden_band.widget
        self.hidden_toggle = Gtk.ToggleButton(label='Show')
        self.hidden_toggle.add_css_class('flat')
        self.hidden_toggle.add_css_class('dev-small')
        self.hidden_toggle.connect('toggled', self._on_hidden_toggled)
        spacer = Gtk.Box(hexpand=True)
        head.append(spacer)
        head.append(self.hidden_toggle)
        self.hidden.append(head)
        self.hidden_note = Gtk.Label(
            xalign=0, wrap=True,
            label='A hidden device is gone from the audio graph, so this is '
                  'the only place it is listed. Unhiding takes effect after '
                  'WirePlumber restarts.')
        self.hidden_note.add_css_class('caption')
        self.hidden_note.add_css_class('dim-label')
        self.hidden_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.hidden_list.add_css_class('boxed-list')
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.append(self.hidden_note)
        inner.append(self.hidden_list)
        self.hidden_reveal = Gtk.Revealer(child=inner, reveal_child=False)
        self.hidden.append(self.hidden_reveal)
        self.hidden.set_visible(False)

    def _on_hidden_toggled(self, btn):
        shown = btn.get_active()
        btn.set_label('Hide' if shown else 'Show')
        self.hidden_reveal.set_reveal_child(shown)

    # ------------------------------------------------------- live state --
    def _on_map(self, *_args):
        # The graph comes from livestate when something changes; the 3 s
        # poll is only for a machine where it cannot listen.
        if self._live is None:
            self._live = livestate.subscribe(self._on_graph)
        if self._live is not None:
            self._rebuild_next = True       # its first delivery builds them
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

        Applied in place, or rebuilt from the same dump, on the main loop and
        in delivery order, so an older read never lands on a newer one.
        """
        data = collect(dump)
        self._data = data
        if self._rebuild_next or not self._sync(data):
            if self.dragging is not None:
                # A rebuild would destroy the chip being dragged; it waits
                # for the drop (UI.md §9: no rebuild during a drag).
                self._held_rebuild = True
                return
            self._rebuild_next = False
            self._rebuild(data, rules.load())
        self._watch_grace()

    def _membership(self, data):
        keys = {(n.id, n.serial) for n in data['nodes']}
        for sink in (True, False):
            for card in endpoints.cards_without_endpoint(
                    data['cards'], data['nodes'], sink):
                keys.add(('card', card.id, sink))
        return keys

    def _ctx(self, data):
        return {'nodes': data['nodes'], 'cards': data['cards'],
                'acts': endpoints.activity(data['nodes'], data['streams'])}

    def _sync(self, data):
        """Paint a delivery onto the cards there are; False to rebuild."""
        if self._membership(data) != set(self._cards):
            return False
        now = _now()
        if not sync_device_levels(
                self._live_controls, data['nodes'], now, LOCAL_GRACE,
                default_held=now - self._default_ts < LOCAL_GRACE):
            return False
        ctx = self._ctx(data)
        for n in data['nodes']:
            self._cards[(n.id, n.serial)].paint(n, ctx)
        for sink in (True, False):
            for card in endpoints.cards_without_endpoint(
                    data['cards'], data['nodes'], sink):
                self._cards[('card', card.id, sink)].paint(card)
        self._filter()
        return True

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
        holds = [c.live['local_ts'] for c in self._cards.values()]
        until = max(holds + [self._default_ts]) + LOCAL_GRACE
        left = until - _now()
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
        if self._data is not None and not self._sync(self._data):
            self.refresh()
        self._watch_grace()

    def _poll_levels(self):
        if self._poll_busy:
            return True
        self._poll_busy = True
        async_call(lambda: collect(pw.pw_dump()), self._apply_polled)
        return True

    def _apply_polled(self, data, error):
        self._poll_busy = False
        if error or data is None:
            return
        self._data = data
        if not self._sync(data):
            if self.dragging is not None:
                self._held_rebuild = True
                return
            # An endpoint appeared, vanished or was recreated.  Rebuild once
            # so its callbacks target the current PipeWire id; ordinary polls
            # keep the cards, their popovers and their meters.
            self.refresh()

    def refresh(self):
        if self._live is not None:
            # Through livestate, so the rebuild stays in order with its
            # deliveries.  Rules are read when the dump lands.
            self._rebuild_next = True
            livestate.refresh()
            return

        def work():
            return collect(pw.pw_dump()), rules.load()
        async_call(work, self._apply)

    def _apply(self, result, error):
        if error or result is None:
            return
        data, rule_data = result
        self._data = data
        self._rebuild(data, rule_data)

    # ------------------------------------------------------------ build --
    def _rebuild(self, data, rule_data):
        self.box.remove_all()
        self._cards = {}
        self._order = []
        self._live_controls = {}
        # A fresh group, so labels of the cards just dropped stop counting.
        self.label_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.chain_templates = {c.id: c.template
                                for c in chains.list_chains()}
        ctx = self._ctx(data)
        nodes = data['nodes']

        self.box.append(self.toolbar, span=True)
        for sink, band in ((True, self.out_band), (False, self.in_band)):
            self.box.append(band.widget, span=True)
            for node in _order(nodes, sink):
                card = DeviceCard(self, node,
                                  rule_data['nodes'].get(node.name, {}), ctx)
                self._add(card)
                self._live_controls[card.key] = card.live
            for c in endpoints.cards_without_endpoint(data['cards'], nodes,
                                                      sink):
                self._add(CardOffCard(self, c, sink))
        self.box.append(self.hidden, span=True)
        self._fill_hidden({n.name for n in nodes},
                          {c.name for c in data['cards']})
        self._filter()

    def _add(self, card):
        self._cards[card.key] = card
        self._order.append(card.key)
        self.box.append(card.widget)

    def _filter(self):
        text = self.search.get_text().strip().lower()
        counts = {True: [0, 0], False: [0, 0]}
        playing = 0
        for key in self._order:
            card = self._cards[key]
            shown = not text or text in card.filter_text
            card.widget.set_visible(shown)
            counts[card.sink][1] += 1
            counts[card.sink][0] += shown
            node = getattr(card, 'node', None)
            if node is not None and node.state == 'running':
                playing += 1
        self.out_band.set_count(*counts[True])
        self.in_band.set_count(*counts[False])
        outs, ins = counts[True][1], counts[False][1]
        parts = [f'{outs} output{"s" if outs != 1 else ""}',
                 f'{ins} input{"s" if ins != 1 else ""}']
        if playing:
            parts.append(f'{playing} running')
        self.summary.set_label(' · '.join(parts))

    # --------------------------------------------------------- actions --
    def make_default(self, card):
        # Paint it now and hold it for LOCAL_GRACE: the graph only shows the
        # new default a little after wpctl returns, so a delivery landing in
        # between would put the star back.  Painted in place, never rebuilt.
        node = card.node
        if node.is_default and self._default_ts == 0.0:
            return
        self._default_ts = _now()
        for other in self._cards.values():
            if isinstance(other, DeviceCard) and other.sink == card.sink:
                other.paint_star(other is card)
        what = 'output' if card.sink else 'input'

        def done(ok, error):
            if ok and not error:
                self.window.toast(f'Default {what}: {node.description}')
                return
            self.window.toast(f'Could not set the default {what}')
            self._default_ts = 0.0
            self._resync()          # put the stars back where they really are
        async_call(lambda: pw.set_default(node.id), done)
        self._watch_grace()

    # -- dragging an app chip onto another card -----------------------------
    def _stream(self, value):
        """The stream a drag payload names, from the last delivery."""
        if not isinstance(value, str) or not value.startswith('stream:'):
            return None
        try:
            sid = int(value.split(':', 1)[1])
        except ValueError:
            return None
        streams = (self._data or {}).get('streams', [])
        return next((st for st in streams if st.id == sid), None)

    def accepts(self, card, value):
        stream = value if not isinstance(value, str) else self._stream(value)
        if stream is None or not isinstance(card, DeviceCard):
            return False
        if card.node.id == stream.target_id:
            return False                        # already there
        # playback goes to an output; a recorder takes an input or the
        # monitor of an output, as its own chip menu offers
        return card.sink if stream.is_playback else True

    def drag_started(self, stream):
        # Say where it can land before it is dropped (UI.md §1, direct
        # manipulation): every card that would take it, and dim the rest.
        self.dragging = stream
        for card in self._cards.values():
            if isinstance(card, DeviceCard):
                card.widget.add_css_class('drop-ready' if self.accepts(
                    card, stream) else 'drop-no')

    def drag_ended(self):
        was = self.dragging
        self.dragging = None
        for card in self._cards.values():
            for css in ('drop-ready', 'drop-no', 'drop-into'):
                card.widget.remove_css_class(css)
        if was is not None and self._held_rebuild:
            self._held_rebuild = False
            GLib.idle_add(lambda: (self.refresh(), False)[1])

    def drop_on(self, card, value):
        stream = self._stream(value)
        self.drag_ended()
        if stream is None or not self.accepts(card, stream):
            return False
        self.move_stream(stream, card.node)
        return True

    def move_stream(self, stream, target):
        def done(ok, error):
            if ok and not error:
                self.window.toast(f'{stream.name} → {target.description}')
            else:
                self.window.toast(f'Could not move {stream.name}')
            self.refresh()
        async_call(lambda: pw.move_stream(stream.id, target.serial), done)

    def repoint(self, card, stream_id, target):
        """Send a software device's output somewhere else, and keep it."""
        node_name = card.node.name

        def work():
            if not pw.move_stream(stream_id, target.serial):
                return None
            # Only once the live move worked: a stored target the graph does
            # not have would leave the two disagreeing.
            return reroute.remember_output(node_name, target.name)

        def done(note, error):
            if note is not None and not error:
                self.window.toast(f'{card.node.description} → '
                                  f'{target.description}{note}')
            else:
                self.window.toast('Could not change where it sends its sound')
            self.refresh()
        async_call(work, done)

    def open_settings(self, card):
        node = card.node
        cards = (self._data or {}).get('cards', [])
        sc = endpoints.card_of(node, cards)
        data = rules.load()
        dlg = DeviceSettingsDialog(
            self, node, data['nodes'].get(node.name, {}),
            (sc.name, sc.description) if sc is not None else None,
            data['devices'].get(sc.name, {}) if sc is not None else {})
        dlg.present()

    def rule_changed(self, node_name):
        """A dialog saved a rule: show "customized" without a rebuild."""
        rule = rules.node_rule(node_name)
        for card in self._cards.values():
            if isinstance(card, DeviceCard) and card.node.name == node_name:
                card.set_rule(rule)

    # -------------------------------------------------------- hidden list --
    def _fill_hidden(self, live_nodes, live_cards):
        for row in self._hidden_rows:
            self.hidden_list.remove(row)
        self._hidden_rows = []
        entries = rules.hidden_entries()
        self.hidden.set_visible(bool(entries))
        self.hidden_band.set_count(len(entries), len(entries))
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
            btn = Gtk.Button(label='Unhide', valign=Gtk.Align.CENTER)
            btn.connect('clicked', lambda _b, e=entry: self._unhide(e))
            row.add_suffix(btn)
            self.hidden_list.append(row)
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
