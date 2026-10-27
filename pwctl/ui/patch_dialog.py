"""Connect channels: many Patchbay cables at once (issue #27).

Pick two nodes, say which channels ("all", "3", "1,3-16"), check the list of
cables it makes, then connect them (or remove the ones already there).  The
direct way, dropping a cable on a node's title, does "all"; this is the way
to do a range, such as an interface's inputs 1-8 into a DAW's 9-16.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import graph
from .widgets import esc

SHOWN = 64           # rows drawn in the list; the rest are counted


def _is_all(text: str) -> bool:
    return (text or '').strip().casefold() in ('', 'all', '*')


class ConnectDialog(Adw.Dialog):
    def __init__(self, page, out_node=None, in_node=None):
        super().__init__(title='Connect channels', content_width=520,
                         content_height=640)
        self.page = page
        self.g = page.area.g
        names = page.names()
        visible = list(page.area.visible_nodes())
        self.outs = sorted((n for n in visible if n.outputs),
                           key=lambda n: names[n.id].casefold())
        self.ins = sorted((n for n in visible if n.inputs),
                          key=lambda n: names[n.id].casefold())
        self.pairs: list = []

        src = Adw.PreferencesGroup(title='From')
        self.from_row = self._picker('Node', self.outs, names, out_node)
        self.from_ch = Adw.EntryRow(title='Channels', text='all')
        src.add(self.from_row)
        src.add(self.from_ch)

        dst = Adw.PreferencesGroup(title='To')
        self.to_row = self._picker('Node', self.ins, names, in_node)
        self.to_ch = Adw.EntryRow(title='Channels', text='all')
        dst.add(self.to_row)
        dst.add(self.to_ch)

        help_ = Gtk.Label(
            label='Channels count from 1, in the order the Patchbay lists '
                  'them. Write all, one channel, a list of ranges such as '
                  '1,3-16, or names (FL, or a MIDI device’s name). With all '
                  'on both sides, channels are matched by name (FL to FL); '
                  'otherwise they are paired in order. One channel on one '
                  'side goes to every channel on the other.',
            xalign=0, wrap=True)
        help_.add_css_class('caption')
        help_.add_css_class('dim-label')

        self.preview = Adw.PreferencesGroup(title='Cables')
        self._rows: list = []

        self.remove_btn = Gtk.Button(label='Remove')
        self.remove_btn.add_css_class('destructive-action')
        self.remove_btn.connect('clicked', lambda *_: self._apply(True))
        self.connect_btn = Gtk.Button(label='Connect')
        self.connect_btn.add_css_class('suggested-action')
        self.connect_btn.connect('clicked', lambda *_: self._apply(False))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        buttons.append(self.remove_btn)
        buttons.append(self.connect_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        for w in (src, dst, help_, self.preview):
            box.append(w)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(Adw.Clamp(maximum_size=560, child=box))
        bottom = Gtk.Box(margin_top=8, margin_bottom=8, margin_start=18,
                         margin_end=18)
        bottom.append(Gtk.Box(hexpand=True))
        bottom.append(buttons)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        view.add_bottom_bar(bottom)
        self.set_child(view)

        for row in (self.from_row, self.to_row):
            row.connect('notify::selected', lambda *_: self.update())
        for row in (self.from_ch, self.to_ch):
            row.connect('changed', lambda *_: self.update())
        self.update()

    @staticmethod
    def _picker(title, nodes, names, want):
        model = Gtk.StringList.new([names[n.id] for n in nodes])
        row = Adw.ComboRow(title=title, model=model, enable_search=True)
        row.set_expression(Gtk.PropertyExpression.new(
            Gtk.StringObject, None, 'string'))
        if want is not None:
            idx = next((i for i, n in enumerate(nodes) if n.id == want.id),
                       None)
            if idx is not None:
                row.set_selected(idx)
        return row

    def _chosen(self, row, nodes):
        i = row.get_selected()
        return nodes[i] if 0 <= i < len(nodes) else None

    def plan(self):
        """(pairs, error, note) for what the dialog shows now."""
        o = self._chosen(self.from_row, self.outs)
        i = self._chosen(self.to_row, self.ins)
        if o is None or i is None:
            return [], 'There is nothing to connect.', ''
        media = graph.shared_media(o, i)
        if media is None:
            kinds_o = ', '.join(sorted({p.media for p in o.outputs}))
            kinds_i = ', '.join(sorted({p.media for p in i.inputs}))
            return [], (f'These two can’t be connected: {o.label} sends '
                        f'{kinds_o}, {i.label} takes {kinds_i}.'), ''
        outs = graph.side_ports(o, 'out', media)
        ins = graph.side_ports(i, 'in', media)
        ft, tt = self.from_ch.get_text(), self.to_ch.get_text()
        try:
            sel_o = [outs[k] for k in graph.parse_channels(ft, len(outs),
                                                           outs)]
        except ValueError as e:
            return [], f'From: {e}.', ''
        try:
            sel_i = [ins[k] for k in graph.parse_channels(tt, len(ins), ins)]
        except ValueError as e:
            return [], f'To: {e}.', ''
        if o.id == i.id and _is_all(ft) and _is_all(tt):
            # Every output of a node into its own inputs is a loop (on the
            # Midi-Bridge, every device into itself): never the default.
            return [], ('From and To are the same node: say which channels, '
                        'for example a device’s name on each side.'), ''
        if _is_all(ft) and _is_all(tt):
            pairs = graph.plan_pairs(outs, ins)
            by = ('matched by name' if pairs and any(
                p.channel and p.channel == q.channel for p, q in pairs)
                else 'in order')
            note = f'{len(outs)} outputs, {len(ins)} inputs, {by}.'
        else:
            pairs = graph.pair_up(sel_o, sel_i)
            note = ''
            if len(sel_o) > 1 and len(sel_i) > 1 and len(sel_o) != len(sel_i):
                extra = abs(len(sel_o) - len(sel_i))
                side = 'outputs' if len(sel_o) > len(sel_i) else 'inputs'
                note = (f'{len(sel_o)} outputs, {len(sel_i)} inputs: the '
                        f'last {extra} {side} are left out.')
        return pairs, '', note

    def update(self):
        for row in self._rows:
            self.preview.remove(row)
        self._rows = []
        pairs, error, note = self.plan()
        self.pairs = pairs
        have = [self.g.find_link(o.id, i.id) is not None for o, i in pairs]
        new, old = have.count(False), have.count(True)
        if error:
            self.preview.set_title('Cables')
            self.preview.set_description(esc(error))
        else:
            self.preview.set_title(f'{len(pairs)} cable'
                                   f'{"s" if len(pairs) != 1 else ""}')
            parts = [note] if note else []
            if old:
                parts.append(f'{old} already connected.')
            self.preview.set_description(esc(' '.join(parts)))
        for (o, i), there in list(zip(pairs, have))[:SHOWN]:
            row = Adw.ActionRow(title=esc(f'{o.name}  →  {i.name}'),
                                title_lines=1)
            if there:
                row.set_subtitle('already connected')
            self.preview.add(row)
            self._rows.append(row)
        if len(pairs) > SHOWN:
            row = Adw.ActionRow(title=f'…and {len(pairs) - SHOWN} more')
            row.add_css_class('dim-label')
            self.preview.add(row)
            self._rows.append(row)
        self.connect_btn.set_label(f'Connect {new}' if new else 'Connect')
        self.connect_btn.set_sensitive(new > 0)
        self.remove_btn.set_label(f'Remove {old}' if old else 'Remove')
        self.remove_btn.set_sensitive(old > 0)

    def _apply(self, remove):
        pairs = list(self.pairs)
        self.close()
        self.page.patch_pairs(pairs, remove=remove)
