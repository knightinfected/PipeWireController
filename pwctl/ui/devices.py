"""Devices page: a list of every device, and the selected one in full.

The list (`device_list.DeviceList`) groups devices by kind (sound cards,
Bluetooth, virtual and processing), with a heading per sound card that has
several rows; the detail (`device_detail`) shows the selected one: a hero
card for the frequent things (default, test, volume, balance, the running
format) and sections that each apply to some devices (signal path,
connection, Bluetooth, routing, name and visibility, format and timing,
technical details).  DR-012, "List and detail" (2026-10-26).

Layout: an `Adw.NavigationSplitView` in the page's own `Adw.BreakpointBin`.
Wide, the list sits beside the detail; below `COLLAPSE_SP` of page width
it collapses to one pane, and picking a device slides its detail in, with
a back button above it.  The bin wraps the split, never sits inside a
scroller (UI.md §3), and its width request is the page's minimum.

Live state: the graph comes from `livestate` while the page is mapped (one
fresh `pw-dump` per real change), the 3 s poll only where it cannot listen.
Every delivery is applied in place: `device_levels.sync_device_levels` for
level, mute and star, `paint()` on every row and on the detail for the
rest.  The list is rebuilt only when the set of endpoints changes; the
detail only when the selection changes or the selected node is recreated,
so it never rebuilds under the user.

Per-device settings become WirePlumber node rules through backend.rules and
need a WirePlumber restart (the banner appears automatically).
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import (bluetooth, chains, endpoints, livestate,  # noqa: E402
                       prefs, pw, reroute, rules, surround)
from .device_detail import CardOffDetail, EndpointDetail, HiddenDetail  # noqa: E402
from .device_levels import sync_device_levels  # noqa: E402
from .device_list import DeviceList, HiddenEntry, NodeEntry, OffEntry  # noqa: E402
from .widgets import LOCAL_GRACE, async_call  # noqa: E402

# Below this page width the list and the detail stop sitting side by side.
# The list wants ~260 and a detail reads well from ~400; in sp, so it
# follows the text size.
COLLAPSE_SP = 680

# The page's minimum width, which a BreakpointBin takes from its request
# alone (UI.md §3).  Under the window's 360, like Signal Paths' 352.
MIN_PAGE_WIDTH = 352


def _now():
    return GLib.get_monotonic_time() / 1e6


def collect(dump):
    """Everything one delivery needs, out of one dump."""
    return {'nodes': pw.list_audio_nodes(dump),
            'streams': pw.list_streams(dump),
            'cards': surround.list_cards(dump, outputs_only=False)}


class DevicesPage:
    _POLL_SECONDS = 3       # only when livestate cannot listen

    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style')
        self.chain_templates = {}

        self._entries = {}              # key -> list entry
        self._order = []                # keys in display order
        self._live_controls = {}        # (id, serial) -> entry.live
        self._data = None               # what the last delivery said
        self._rules = {'nodes': {}, 'devices': {}}
        self._hidden = []
        self._poll_timer = None
        self._poll_busy = False
        self._live = None               # livestate subscription while mapped
        self._rebuild_next = False      # the next delivery rebuilds the list
        self._grace_id = None
        self._default_ts = 0.0          # when the user last clicked a star
        self.dragging = None            # the stream an app chip is carrying
        self._held_rebuild = False      # a rebuild that waited for the drop
        self._want = None               # (ident, key) of the selection
        self.detail = None
        self._codecs = {}               # (card name, profile) -> codecs

        self.list = DeviceList(self)

        # The detail side: a back bar for the narrow layout, then the
        # selected device's pane, crossfading when the selection changes.
        self.back = Gtk.Box(spacing=6, margin_top=6, margin_bottom=2,
                            margin_start=6, margin_end=6)
        self.back.add_css_class('devl-back')
        back = Gtk.Button(tooltip_text='Back to the list')
        back.set_child(Adw.ButtonContent(icon_name='go-previous-symbolic',
                                         label='Devices'))
        back.add_css_class('flat')
        back.connect('clicked', lambda _b: self.split.set_show_content(False))
        self.back.append(back)
        self.back_button = back
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType
                               .CROSSFADE, transition_duration=150,
                               vexpand=True, hhomogeneous=False)
        self.stack.connect('notify::transition-running', self._drop_old)
        self._empty = Adw.StatusPage(icon_name='pwctl-devices-symbolic',
                                     title='No devices yet',
                                     description='Reading the audio graph…')
        self.stack.add_child(self._empty)
        self._old = []
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.back)
        content.append(self.stack)

        self.split = Adw.NavigationSplitView(
            min_sidebar_width=250, max_sidebar_width=340,
            sidebar_width_fraction=0.34)
        self.split.set_sidebar(Adw.NavigationPage(
            title='Devices', tag='list', child=self.list.widget))
        self.content_page = Adw.NavigationPage(title='Device', tag='detail',
                                               child=content)
        self.split.set_content(self.content_page)
        self.split.connect('notify::collapsed', self._on_collapsed)
        self._on_collapsed()

        bin_ = Adw.BreakpointBin(width_request=MIN_PAGE_WIDTH,
                                 height_request=200, child=self.split)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(
            f'max-width: {COLLAPSE_SP}sp'))
        bp.add_setter(self.split, 'collapsed', True)
        bin_.add_breakpoint(bp)
        self.widget = bin_
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

        # The sidebar's Advanced switch; a window without one (a harness's
        # stand-in) simply never changes it.
        switch = getattr(window, 'adv_switch', None)
        if isinstance(switch, Gtk.Widget):
            switch.connect('notify::active', lambda *_a: self._on_advanced())

    # ------------------------------------------------------------ layout --
    @property
    def collapsed(self):
        return self.split.get_collapsed()

    def _on_collapsed(self, *_a):
        self.back.set_visible(self.split.get_collapsed())

    def _on_advanced(self):
        if self.detail is not None:
            self.detail.set_advanced(bool(getattr(self.window, 'advanced',
                                                  False)))

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
        """Paint a delivery onto the rows there are; False to rebuild."""
        mine = {k for k in self._entries if k != 'hidden'}
        if self._membership(data) != mine:
            return False
        now = _now()
        if not sync_device_levels(
                self._live_controls, data['nodes'], now, LOCAL_GRACE,
                default_held=now - self._default_ts < LOCAL_GRACE):
            return False
        ctx = self._ctx(data)
        cards = {c.id: c for c in data['cards']}
        stale_heads = False
        for e in self._entries.values():
            card = getattr(e.group, 'card', None)
            if card is not None and card.id in cards:
                fresh = cards[card.id]
                if (fresh.active_profile, fresh.description) != \
                        (card.active_profile, card.description):
                    stale_heads = True
                e.group.card = fresh
        for n in data['nodes']:
            self._entries[(n.id, n.serial)].paint(n, ctx)
        for sink in (True, False):
            for card in endpoints.cards_without_endpoint(
                    data['cards'], data['nodes'], sink):
                self._entries[('card', card.id, sink)].paint(card)
        if stale_heads:
            self.list.listbox.invalidate_headers()
        self._paint_detail(data, ctx)
        self.list.refilter()
        return True

    def _paint_detail(self, data, ctx):
        d = self.detail
        if isinstance(d, EndpointDetail):
            node = next((n for n in data['nodes']
                         if (n.id, n.serial) == d.key), None)
            if node is not None:
                d.paint(node, ctx)
        elif isinstance(d, CardOffDetail):
            card = next((c for c in data['cards'] if c.id == d.key[1]), None)
            if card is not None:
                d.paint(card)

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
        holds = [e.live['local_ts'] for e in self._entries.values()]
        if self.detail is not None and getattr(self.detail, 'live', None):
            holds.append(self.detail.live.get('local_ts', 0.0))
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
            # keep the rows, the detail and their meters.
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
        self._rules = rule_data
        self.chain_templates = {c.id: c.template
                                for c in chains.list_chains()}
        ctx = self._ctx(data)
        nodes = data['nodes']
        self._hidden = rules.hidden_entries()
        groups = endpoints.list_groups(nodes, data['cards'])
        entries = self.list.rebuild(groups, ctx, len(self._hidden))
        self._entries = {e.key: e for e in entries}
        self._order = [e.key for e in entries]
        self._live_controls = {e.key: e.live for e in entries
                               if isinstance(e, NodeEntry)}
        for e in entries:
            if isinstance(e, NodeEntry):
                e.paint_star(e.node.is_default)
        self._live_nodes = {n.name for n in nodes}
        self._live_cards = {c.name for c in data['cards']}
        self._restore_selection(ctx)

    def _restore_selection(self, ctx):
        """Select what was selected, by what lasts: kind, direction, name."""
        entries = [self._entries[k] for k in self._order]
        if not entries:
            self._empty.set_description('There is no output or input in '
                                        'the audio graph.')
            self._show(None)
            return
        want = self._want
        pick = None
        if want is not None:
            ident, key = want
            same = [e for e in entries if e.ident == ident]
            pick = next((e for e in same if e.key == key),
                        same[0] if same else None)
            if pick is None and ident[0] in ('node', 'off'):
                pick = self._near(entries, ident)
        if pick is None:
            pick = next((e for e in entries if isinstance(e, NodeEntry)
                         and e.sink and e.node.is_default), None) \
                or next((e for e in entries if isinstance(e, NodeEntry)),
                        entries[0])
        self.select(pick, show=False, ctx=ctx)

    def _near(self, entries, ident):
        """After a profile switch: the same card, the same direction first."""
        kind, a, b = ident
        card_name = None
        if kind == 'off':
            card_name, sink = a, b
        else:
            sink = a
            old = self.detail
            card = getattr(old, 'card', None)
            card_name = card.name if card is not None else None
        if card_name is None:
            return None

        def on_card(e):
            if isinstance(e, OffEntry):
                return e.card.name == card_name
            if isinstance(e, NodeEntry):
                c = endpoints.card_of(e.node, (self._data or {}).get(
                    'cards', []))
                return c is not None and c.name == card_name
            return False
        mine = [e for e in entries if on_card(e)]
        return next((e for e in mine if e.sink == sink),
                    mine[0] if mine else None)

    # -------------------------------------------------------- selection --
    def select(self, entry, show=True, ctx=None):
        """Open `entry` in the detail; in the narrow layout, slide to it."""
        self.list.highlight(entry)
        self._want = (entry.ident, entry.key)
        if self.detail is None or self.detail.key != entry.key \
                or isinstance(entry, HiddenEntry):
            self._show(self._build_detail(entry, ctx))
        elif isinstance(entry, NodeEntry):
            self.detail.bind_live(entry.live)
        if show:
            self.split.set_show_content(True)

    def select_node(self, node):
        """Open a device named by a chip or a sibling row."""
        entry = self._entries.get((node.id, node.serial))
        if entry is None:
            return
        if entry not in self.list.visible_entries():
            # Filtered out of the list: show everything again, or the
            # detail would belong to a row nobody can see.
            self.list.search.set_text('')
            self.list.dirs.set_active_name('all')
        self.select(entry)

    def reload_detail(self):
        """Build the selected device's detail again (its rule was reset)."""
        entry = self.list.current
        if entry is not None:
            self._show(self._build_detail(entry))

    def _build_detail(self, entry, ctx=None):
        data = self._data or {'nodes': [], 'streams': [], 'cards': []}
        ctx = ctx or self._ctx(data)
        if isinstance(entry, NodeEntry):
            node = entry.node
            rule = self._rules['nodes'].get(node.name, {})
            card = endpoints.card_of(node, data['cards'])
            card_rule = (self._rules['devices'].get(card.name, {})
                         if card is not None else {})
            return EndpointDetail(self, node, rule, ctx, entry.live,
                                  card_rule=card_rule)
        if isinstance(entry, OffEntry):
            return CardOffDetail(self, entry.card, entry.sink)
        return HiddenDetail(self, self._hidden,
                            getattr(self, '_live_nodes', set()),
                            getattr(self, '_live_cards', set()))

    def _show(self, detail):
        old = self.detail
        if old is not None:
            old.unbind()
        self.detail = detail
        widget = detail.widget if detail is not None else self._empty
        # What a screen reader (and the narrow layout's page) calls it.
        node = getattr(detail, 'node', None)
        self.content_page.set_title(node.description if node is not None
                                    else 'Hidden devices'
                                    if isinstance(detail, HiddenDetail)
                                    else 'Device')
        if widget.get_parent() is None:
            self.stack.add_child(widget)
        self.stack.set_visible_child(widget)
        if old is not None and old.widget is not widget:
            self._old.append(old.widget)
        self._drop_old()

    def _drop_old(self, *_a):
        """Remove the details crossfaded away, once the fade is over."""
        if self.stack.get_transition_running():
            return
        for w in self._old:
            if w.get_parent() is self.stack and \
                    w is not self.stack.get_visible_child():
                self.stack.remove(w)
        self._old = []

    # --------------------------------------------------------- actions --
    def paint_star(self, entry, is_default):
        """The sync's star painter for one node: its row, and the detail."""
        entry.paint_star(is_default)
        d = self.detail
        if isinstance(d, EndpointDetail) and d.key == entry.key:
            d.paint_star(is_default)

    def make_default(self, node):
        # Paint it now and hold it for LOCAL_GRACE: the graph only shows the
        # new default a little after wpctl returns, so a delivery landing in
        # between would put the star back.  Painted in place, never rebuilt.
        if node.is_default and self._default_ts == 0.0:
            return
        self._default_ts = _now()
        for e in self._entries.values():
            if isinstance(e, NodeEntry) and e.sink == node.is_sink:
                self.paint_star(e, e.node.id == node.id
                                and e.node.serial == node.serial)
        what = 'output' if node.is_sink else 'input'

        def done(ok, error):
            if ok and not error:
                self.window.toast(f'Default {what}: {node.description}')
                return
            self.window.toast(f'Could not set the default {what}')
            self._default_ts = 0.0
            self._resync()          # put the stars back where they really are
        async_call(lambda: pw.set_default(node.id), done)
        self._watch_grace()

    def codecs(self, card_name, profile, callback):
        """The codecs a headset offers on a profile, asked once per profile.

        `pactl … list-codecs` is a subprocess, so never per delivery: the
        answer is kept until the profile changes."""
        key = (card_name, profile)
        if key in self._codecs:
            callback(self._codecs[key])
            return

        def done(result, error):
            codecs = [] if error or result is None else result
            self._codecs[key] = codecs
            callback(codecs)
        async_call(lambda: bluetooth.list_codecs(card_name), done)

    def add_virtual(self):
        """Add → New virtual device: the Virtual Devices page's own dialog."""
        page = getattr(self.window, 'pages', {}).get('virtual')
        opener = getattr(page, '_open_dialog', None)
        if callable(opener):
            opener(None)
        else:
            self.window.goto('virtual')

    # -- dragging an app chip onto a row ------------------------------------
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

    def accepts(self, entry, value):
        stream = value if not isinstance(value, str) else self._stream(value)
        if stream is None or not isinstance(entry, NodeEntry):
            return False
        if entry.node.id == stream.target_id:
            return False                        # already there
        # playback goes to an output; a recorder takes an input or the
        # monitor of an output, as its own chip menu offers
        return entry.sink if stream.is_playback else True

    def drag_started(self, stream):
        # Say where it can land before it is dropped (UI.md §1, direct
        # manipulation): every row that would take it, and dim the rest.
        self.dragging = stream
        self.list.drag_marks(lambda e: self.accepts(e, stream))

    def drag_ended(self):
        was = self.dragging
        self.dragging = None
        self.list.clear_drag_marks()
        if was is not None and self._held_rebuild:
            self._held_rebuild = False
            GLib.idle_add(lambda: (self.refresh(), False)[1])

    def drop_on(self, entry, value):
        stream = self._stream(value)
        self.drag_ended()
        if stream is None or not self.accepts(entry, stream):
            return False
        self.move_stream(stream, entry.node)
        return True

    def move_stream(self, stream, target):
        def done(ok, error):
            if ok and not error:
                self.window.toast(f'{stream.name} → {target.description}')
            else:
                self.window.toast(f'Could not move {stream.name}')
            self.refresh()
        async_call(lambda: pw.move_stream(stream.id, target.serial), done)

    def repoint(self, node, stream_id, target):
        """Send a software device's output somewhere else, and keep it."""
        node_name = node.name

        def work():
            if not pw.move_stream(stream_id, target.serial):
                return None
            # Only once the live move worked: a stored target the graph does
            # not have would leave the two disagreeing.
            return reroute.remember_output(node_name, target.name)

        def done(note, error):
            if note is not None and not error:
                self.window.toast(f'{node.description} → '
                                  f'{target.description}{note}')
            else:
                self.window.toast('Could not change where it sends its sound')
            self.refresh()
        async_call(work, done)

    def rule_changed(self, node_name):
        """A section saved a rule: show "customized" without a rebuild."""
        rule = rules.node_rule(node_name)
        self._rules.setdefault('nodes', {})[node_name] = rule
        d = self.detail
        if isinstance(d, EndpointDetail) and d.node.name == node_name:
            d.set_rule(rule)

    def unhide(self, entry):
        def work():
            if entry['kind'] == 'device':
                rules.set_device_rule(entry['key'], hide=False)
            else:
                rules.set_node_rule(entry['key'], hide=False)
            return True
        async_call(work, lambda r, e: (
            self.window.toast(f'Failed: {e}') if e
            else (self.window.flag_restart('wireplumber'), self.refresh())))
