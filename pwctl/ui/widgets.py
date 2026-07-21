"""Shared UI helpers."""

from __future__ import annotations

import threading

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402


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


def pill(text: str, style: str) -> Gtk.Label:
    """Small colored status label. style: success | warning | error | dim"""
    lbl = Gtk.Label(label=text)
    lbl.add_css_class('status-pill')
    lbl.add_css_class(f'pill-{style}')
    lbl.set_valign(Gtk.Align.CENTER)
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


def page_scroller(*groups, width=760) -> Gtk.ScrolledWindow:
    """A scrollable, clamped column of PreferencesGroups."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                  margin_top=24, margin_bottom=36,
                  margin_start=16, margin_end=16)
    for g in groups:
        box.append(g)
    clamp = Adw.Clamp(maximum_size=width, tightening_threshold=min(600, width))
    clamp.set_child(box)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                            vexpand=True)
    sw.set_child(clamp)
    return sw


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
