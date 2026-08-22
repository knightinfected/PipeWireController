"""Shared UI helpers."""

from __future__ import annotations

import threading

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402


def async_call(fn, callback=None):
    """Run fn() in a thread; deliver its result to callback on the UI loop."""
    def worker():
        try:
            result = fn()
            error = None
        except Exception as e:      # surface, don't crash the app
            result, error = None, e
        if callback:
            GLib.idle_add(lambda: (callback(result, error), False)[1])
    threading.Thread(target=worker, daemon=True).start()


def esc(text) -> str:
    """Make arbitrary text safe for an Adw row title or subtitle.

    Those labels are Pango markup, so a bare '&' or '<' in a song title, a
    device description or a name the user typed makes the whole label fail to
    render.  Setting use-markup=False doesn't help: the parse happens as the
    property is set, before we could turn it off.
    """
    return GLib.markup_escape_text(str(text)) if text else ''


def pill(text: str, style: str) -> Gtk.Label:
    """Small colored status label. style: success | warning | error | dim"""
    lbl = Gtk.Label(label=text)
    lbl.add_css_class('status-pill')
    lbl.add_css_class(f'pill-{style}')
    lbl.set_valign(Gtk.Align.CENTER)
    return lbl


def micro(text: str) -> Gtk.Label:
    """Uppercase micro heading.

    Uppercased in Python, not in CSS — GTK's `text-transform` support is not
    relied on anywhere in this app.
    """
    lbl = Gtk.Label(label=text.upper(), xalign=0)
    lbl.add_css_class('micro-heading')
    return lbl


def state_style(state: str) -> str:
    return {'active': 'success', 'activating': 'warning',
            'failed': 'error'}.get(state, 'dim')


def icon_button(icon: str, tooltip: str, callback, css=None) -> Gtk.Button:
    btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
    btn.set_valign(Gtk.Align.CENTER)
    btn.add_css_class('flat')
    if css:
        btn.add_css_class(css)
    btn.connect('clicked', callback)
    return btn


def group(title: str, description: str = '') -> Adw.PreferencesGroup:
    g = Adw.PreferencesGroup(title=title)
    if description:
        g.set_description(description)
    return g


class ColumnBox(Gtk.Widget):
    """Newspaper columns: fill the width without stretching anything.

    Every page in this app was one fixed-width column centred in the window.
    That is fine at 1080px and wrong at 2252, which is what this machine
    actually runs (a 201 PPI panel at compositor scale 1.0, so the window is
    2252 *logical* px).  Measured before this existed: twelve of sixteen pages
    painted 728px into a 2012px content area and left 64% of it blank.

    So: as many columns as the width affords, each still the width a
    preference group or a card actually looks right at.  `MAX_COL` is the
    point of the whole thing — extra width buys *another column*, never a
    wider one, because a 2000px-wide row with its title at the far left and
    its switch at the far right is worse than the empty space was.

    Children keep document order.  The split is by cumulative height, so a
    numbered wizard still reads 1-2 down the left and 3-4 down the right, and
    a page whose groups differ wildly in height still comes out level at the
    bottom.  Balancing by height is the reason this is not a `Gtk.FlowBox`:
    a flow box packs by row, so every child shorter than its tallest
    neighbour gets a band of dead space under it — the vertical version of
    exactly the bug being fixed.  (`homogeneous=True` makes that worse still;
    it sizes every cell to the tallest child in the whole box.)

    `append(child, span=True)` makes a child a **full-width band** instead: it
    takes the width of every column plus the gaps between them, and the column
    packing restarts underneath it.  That is what lets a board put two hero
    cards side by side at the top, a status strip across the middle and the
    rest back into columns, without a second layout container that would line
    its columns up somewhere else.
    """

    __gtype_name__ = 'PwctlColumnBox'

    IDEAL = 600      # the width a column wants
    MIN_COL = 520    # the narrowest worth opening another column for
    MAX_COL = 720    # and the width past which one is never stretched
    # A column holding less than this fraction of the tallest one reads as an
    # orphan, so that column count is rejected.  0.3 was picked against the
    # real pages: it keeps Server's three (its short column is 0.34) and
    # refuses Devices' third (0.11) and Filter Chains' second (0.21).
    BALANCE = 0.3

    def __init__(self, spacing=24, max_columns=3, ideal=IDEAL,
                 max_col=MAX_COL, min_col=MIN_COL):
        super().__init__()
        self._spacing = spacing
        self._max_columns = max_columns
        self._ideal = ideal
        self._max_col = max_col
        self._min_col = min_col
        self._kids: list[Gtk.Widget] = []
        self._spans: set = set()

    def append(self, child: Gtk.Widget, span: bool = False):
        child.set_parent(self)
        self._kids.append(child)
        if span:
            self._spans.add(child)

    def do_dispose(self):
        # A Gtk.Widget subclass must unparent its children itself, or GTK
        # warns that it was finalized with children still attached.
        for child in self._kids:
            child.unparent()
        self._kids = []
        self._spans = set()
        Gtk.Widget.do_dispose(self)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    # -- the split ---------------------------------------------------------
    def _visible(self):
        return [c for c in self._kids if c.get_visible()]

    def _fits(self, width):
        """Most columns this width can carry, before content is considered."""
        kids = self._visible()
        if not kids:
            return 0
        # Judged against the narrowest column worth having, not the ideal one
        # — otherwise a 1400px window sits at one 760px column with 400px
        # beside it, which is the original bug in miniature.
        n = (width + self._spacing) // (self._min_col + self._spacing)
        return max(1, min(self._max_columns, len(kids), int(n)))

    def _geometry(self, width, ncols):
        """(column width, x of the first column) for a given column count."""
        sp = self._spacing
        colw = max(int(min(self._max_col, (width - (ncols - 1) * sp) // ncols)),
                   1)
        total = ncols * colw + (ncols - 1) * sp
        return colw, max(0, (width - total) // 2)

    def _assign(self, kids, heights, ncols):
        """Which column each child lands in, in order.

        Greedy against an equal-height target, with one guard: never take so
        many children that a later column would be left empty.
        """
        target = sum(heights) / ncols if ncols else 0
        cols, col, acc = [], 0, 0
        for i, h in enumerate(heights):
            left_kids = len(heights) - i
            left_cols = ncols - col
            starve = acc > 0 and left_kids <= left_cols - 1
            over = acc > 0 and acc + h / 2 > target
            if col < ncols - 1 and (starve or over):
                col += 1
                acc = 0
            cols.append(col)
            acc += h + self._spacing
        return cols

    def _try(self, kids, width, ncols):
        """One candidate layout: (placements, height, column heights).

        Spanning children cut the list into runs.  Each run is packed into
        columns starting level under whatever came before it, and the next
        span starts below the tallest column of that run, so the bands really
        do span and nothing overlaps them.  Only columned children count
        toward the returned column heights — a band is the same width in every
        candidate, so letting it into the balance test would just flatten the
        differences the test exists to find.
        """
        sp = self._spacing
        colw, x0 = self._geometry(width, ncols)
        full = ncols * colw + (ncols - 1) * sp
        out: list = []
        totals = [0] * ncols
        y_base = 0
        run: list = []

        def flush():
            nonlocal y_base
            if not run:
                return
            heights = [c.measure(Gtk.Orientation.VERTICAL, colw)[1]
                       for c in run]
            cols = self._assign(run, heights, ncols)
            ys = [y_base] * ncols
            for child, h, col in zip(run, heights, cols):
                out.append((child, x0 + col * (colw + sp), ys[col], colw, h))
                ys[col] += h + sp
                totals[col] += h + sp
            y_base = max(ys)
            run.clear()

        for child in kids:
            if child in self._spans:
                flush()
                h = child.measure(Gtk.Orientation.VERTICAL, full)[1]
                out.append((child, x0, y_base, full, h))
                y_base += h + sp
            else:
                run.append(child)
        flush()
        return (out, max(0, y_base - sp),
                [max(0, t - sp) for t in totals])

    def _run(self, width):
        """Lay out at `width`; returns (placements, total height).

        Column count is chosen by content as well as by width.  Devices is
        why: four groups, but one of them is the whole device list and two are
        headings with nothing under them yet, so three columns put the list in
        the middle and stranded an empty "Inputs (sources)" heading in a
        column of its own.  A column that cannot be filled is worse than the
        gap it was opened to remove, so candidates are tried widest-first and
        the first decently balanced one wins.
        """
        kids = self._visible()
        if not kids:
            return [], 0
        best = None
        for ncols in range(self._fits(width), 0, -1):
            placements, height, col_heights = self._try(kids, width, ncols)
            best = best or (placements, height)
            tallest = max(col_heights)
            if ncols == 1 or min(col_heights) >= self.BALANCE * tallest:
                return placements, height
        return best

    # -- GtkWidget ---------------------------------------------------------
    def do_measure(self, orientation, for_size):
        kids = self._visible()
        if not kids:
            return 0, 0, -1, -1
        if orientation == Gtk.Orientation.HORIZONTAL:
            # Minimum is one column, so the page floor is unchanged and a
            # narrow window still works.  Natural is every column we would
            # ever open, which is what makes a clamp above us hand over the
            # real width instead of a natural 728.
            mn = max(c.measure(orientation, -1)[0] for c in kids)
            ncols = min(self._max_columns, len(kids))
            nat = ncols * self._ideal + (ncols - 1) * self._spacing
            return mn, max(mn, nat), -1, -1
        width = for_size if for_size > 0 else self.do_measure(
            Gtk.Orientation.HORIZONTAL, -1)[1]
        _placements, height = self._run(width)
        return height, height, -1, -1

    def do_size_allocate(self, width, height, baseline):
        placements, _h = self._run(width)
        for child, x, y, w, h in placements:
            alloc = Gdk.Rectangle()
            alloc.x, alloc.y, alloc.width, alloc.height = x, y, w, h
            child.size_allocate(alloc, -1)


def page_scroller(*groups, width=760, columns=3) -> Gtk.ScrolledWindow:
    """A scrollable, multi-column page of PreferencesGroups.

    `width` is now the widest a single column may get, not the width of the
    whole page — past that, extra window width opens another column instead
    of stretching the rows.  Pass `columns=1` for a page that must stay a
    single reading column.
    """
    box = ColumnBox(spacing=24, max_columns=columns,
                    max_col=max(width, ColumnBox.IDEAL))
    box.set_margin_top(24)
    box.set_margin_bottom(36)
    box.set_margin_start(16)
    box.set_margin_end(16)
    for g in groups:
        box.append(g)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                            vexpand=True)
    sw.set_child(box)
    return sw


# --------------------------------------------------- live lists that poll --

# After the user touches a control, ignore polled values for this long so a
# refresh can't yank it back mid-drag.
LOCAL_GRACE = 2.0


class GraceMixin:
    """Per-row 'the user just touched this' window.

    Any row on a page that polls needs this: without it the refresh writes the
    machine's value back over the one being dragged.  Call `touch()` from every
    handler that acts on user input, and check `in_grace` before applying a
    polled value.
    """

    _local_ts = 0.0

    def touch(self):
        self._local_ts = GLib.get_monotonic_time() / 1e6

    @property
    def in_grace(self) -> bool:
        return GLib.get_monotonic_time() / 1e6 - self._local_ts < LOCAL_GRACE


class RowSync:
    """Keeps a Gtk.ListBox in step with a list of objects, rebuilding only
    when the *membership* changes.

    The rule that matters: rebuilding replaces widgets, which cancels drags,
    closes popovers, collapses expanders and restarts level meters.  So a poll
    that finds the same keys in the same order must update the existing rows in
    place and touch nothing else.  Pages that tear down and rebuild on every
    refresh are the ones where an expander you opened snaps shut under you.

    `items` is a list of (key, obj).  `make_row(obj)` builds a new row; each row
    is expected to have an `update(obj)`-shaped method, which the caller drives
    from the returned pairs.
    """

    def __init__(self, listbox: Gtk.ListBox):
        self.listbox = listbox
        self.rows: dict = {}

    def clear(self):
        """Drop every row so the next sync rebuilds from scratch."""
        for row in self.rows.values():
            self.listbox.remove(row)
        self.rows = {}

    def sync(self, items, make_row) -> list:
        """Returns [(row, obj)] for the caller to update."""
        keys = [k for k, _ in items]
        if keys != list(self.rows):
            self.clear()
            for key, obj in items:
                row = make_row(obj)
                self.listbox.append(row)
                self.rows[key] = row
        return [(self.rows[k], obj) for k, obj in items]


def confirm(parent, heading, body, action_label, on_confirm,
            destructive=True):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.add_response('cancel', 'Cancel')
    dlg.add_response('ok', action_label)
    if destructive:
        dlg.set_response_appearance('ok', Adw.ResponseAppearance.DESTRUCTIVE)
    else:
        dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response('cancel')

    def on_resp(_d, resp):
        if resp == 'ok':
            on_confirm()
    dlg.connect('response', on_resp)
    dlg.present(parent)


def prompt_number(parent, heading, body, initial, on_accept,
                  action_label='Set'):
    """AlertDialog with a numeric entry; calls on_accept(text) on OK."""
    dlg = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(input_purpose=Gtk.InputPurpose.DIGITS,
                      text=str(initial), activates_default=True)
    dlg.set_extra_child(entry)
    dlg.add_response('cancel', 'Cancel')
    dlg.add_response('ok', action_label)
    dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response('ok')

    def on_resp(_d, resp):
        if resp == 'ok':
            on_accept(entry.get_text())
    dlg.connect('response', on_resp)
    dlg.present(parent)
    entry.grab_focus()


def combine_devices(parent, sinks, on_created, name_hint='Combined output'):
    """Tick several output devices and get one sink that feeds them all.

    A filter chain, rack or equalizer has a single playback target, so "play
    on the speakers and the HDMI at once" is really "target one sink that
    fans out".  That sink is an ordinary combine-sink virtual device — this
    just builds it without making the user go and learn that first.
    `on_created(node_name, description)` fires once it is running.
    """
    dlg = Adw.AlertDialog(
        heading='Send to several devices',
        body='The chosen devices are combined into one output, which this '
             'becomes the target of. It appears on the Virtual Devices page '
             'like any other, so you can rename or remove it later.')
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class('boxed-list')
    checks = []
    for n in sinks:
        row = Adw.ActionRow(title=esc(n.description), subtitle=esc(n.name),
                            title_lines=1, subtitle_lines=1)
        chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        row.add_prefix(chk)
        row.set_activatable_widget(chk)
        listbox.append(row)
        checks.append((chk, n))
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                            min_content_height=220, propagate_natural_height=True)
    sw.set_child(listbox)
    dlg.set_extra_child(sw)
    dlg.add_response('cancel', 'Cancel')
    dlg.add_response('ok', 'Combine')
    dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response('ok')

    def on_resp(_d, resp):
        if resp != 'ok':
            return
        picked = [n for chk, n in checks if chk.get_active()]
        if len(picked) < 2:
            on_created(None, 'Pick at least two devices to combine')
            return
        from ..backend import virtual
        dev = virtual.new_device(
            f'{name_hint} ({len(picked)} devices)', 'combine-sink',
            members=[n.name for n in picked])
        dev.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            on_created(dev.node_name if ok else None,
                       dev.name if ok else (err or 'could not be created'))
        async_call(lambda: virtual.apply(dev), done)
    dlg.connect('response', on_resp)
    dlg.present(parent)


def text_viewer_dialog(parent, title, text, editable=False, on_save=None):
    dlg = Adw.Dialog(title=title, content_width=720, content_height=560)
    tv = Gtk.TextView(editable=editable, monospace=True,
                      left_margin=12, right_margin=12,
                      top_margin=12, bottom_margin=12)
    tv.get_buffer().set_text(text)
    sw = Gtk.ScrolledWindow(vexpand=True)
    sw.set_child(tv)

    header = Adw.HeaderBar()
    view = Adw.ToolbarView()
    view.add_top_bar(header)
    view.set_content(sw)
    if editable and on_save:
        save = Gtk.Button(label='Save')
        save.add_css_class('suggested-action')

        def do_save(_b):
            buf = tv.get_buffer()
            content = buf.get_text(buf.get_start_iter(), buf.get_end_iter(),
                                   False)
            if on_save(content):
                dlg.close()
        save.connect('clicked', do_save)
        header.pack_end(save)
    dlg.set_child(view)
    dlg.present(parent)


def pick_file(parent, title, callback, filters=None, initial_folder=None):
    dialog = Gtk.FileDialog(title=title)
    if initial_folder:
        import os
        from gi.repository import Gio
        if os.path.isdir(initial_folder):
            dialog.set_initial_folder(Gio.File.new_for_path(initial_folder))
    if filters:
        from gi.repository import Gio
        store = Gio.ListStore()
        for name, patterns in filters:
            f = Gtk.FileFilter()
            f.set_name(name)
            for p in patterns:
                f.add_pattern(p)
            store.append(f)
        dialog.set_filters(store)

    def done(dlg, result):
        try:
            gfile = dlg.open_finish(result)
        except GLib.Error:
            return
        if gfile:
            callback(gfile.get_path())
    dialog.open(parent, None, done)


def pick_files(parent, title, callback, filters=None, initial_folder=None):
    dialog = Gtk.FileDialog(title=title)
    if initial_folder:
        import os
        from gi.repository import Gio
        if os.path.isdir(initial_folder):
            dialog.set_initial_folder(Gio.File.new_for_path(initial_folder))
    if filters:
        from gi.repository import Gio
        store = Gio.ListStore()
        for name, patterns in filters:
            f = Gtk.FileFilter()
            f.set_name(name)
            for p in patterns:
                f.add_pattern(p)
            store.append(f)
        dialog.set_filters(store)

    def done(dlg, result):
        try:
            files = dlg.open_multiple_finish(result)
        except GLib.Error:
            return
        callback([files.get_item(i).get_path()
                  for i in range(files.get_n_items())])
    dialog.open_multiple(parent, None, done)


def pick_folder(parent, title, callback):
    dialog = Gtk.FileDialog(title=title)

    def done(dlg, result):
        try:
            gfile = dlg.select_folder_finish(result)
        except GLib.Error:
            return
        if gfile:
            callback(gfile.get_path())
    dialog.select_folder(parent, None, done)
