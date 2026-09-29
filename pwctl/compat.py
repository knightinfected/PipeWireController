"""Startup requirement checks.

This module runs before the UI is built and is the one part of the app that has
to keep working on a machine too old to run the rest of it.  So: no imports
from ``pwctl.ui``, and nothing from libadwaita newer than 1.0.  The version
getters have been there since 1.0, which is what makes them safe to ask.

Without this, a machine below the floor dies with an ``AttributeError`` deep
inside a widget constructor — invisible when the app was started from the
desktop entry, and unreadable when it wasn't.
"""

from __future__ import annotations

import os
import sys

# Derived, not guessed: /usr/share/gir-1.0/Adw-1.gir carries a version=
# attribute on every class, so cross-referencing the Adw.* names used under
# pwctl/ against it gives the real floor.  Four symbols need 1.7 and nothing
# needs more — Adw.ToggleGroup and Adw.Toggle in dashboard.py, Adw.WrapBox and
# Adw.JustifyMode in paths_page.py.  Re-derive it the same way before changing
# this number; checking members as well as classes throws ~60 false positives.
MIN_ADW = (1, 7)

REPO = 'https://github.com/knightinfected/PipeWireController'


def _fmt(version) -> str:
    return '.'.join(str(n) for n in version)


def adw_version() -> tuple[int, ...] | None:
    """This machine's libadwaita as a tuple, or None if it isn't loadable."""
    # Debug only, and it can only ever report something *older* than reality —
    # it exists so the failure path can be seen on a machine that is fine.
    fake = os.environ.get('PWCTL_FAKE_ADW')
    if fake:
        try:
            return tuple(int(n) for n in fake.split('.'))
        except ValueError:
            pass
    try:
        import gi
        gi.require_version('Adw', '1')
        from gi.repository import Adw
    except (ImportError, ValueError):
        return None
    return (Adw.get_major_version(),
            Adw.get_minor_version(),
            Adw.get_micro_version())


def problem(found: tuple[int, ...] | None = None, _probe: bool = True):
    """(title, body) describing why this machine can't run the app, else None.

    Pass `found` to ask about a version other than this machine's.
    """
    if found is None and _probe:
        found = adw_version()

    if found is None:
        return ('libadwaita could not be loaded',
                'PipeWire Controller is built on GTK 4 and libadwaita, and '
                'libadwaita %s or newer has to be installed for it to start.\n\n'
                'It is packaged by your distribution — look for libadwaita '
                'along with its GObject introspection data, which is a separate '
                'package on some distributions (gir1.2-adw-1 on Debian and '
                'Ubuntu, typelib-1_0-Adw-1 on openSUSE).'
                % _fmt(MIN_ADW))

    if found[:2] >= MIN_ADW:
        return None

    return ('This version needs a newer libadwaita',
            'PipeWire Controller needs libadwaita %s or newer.\n'
            'This system has libadwaita %s.\n\n'
            'libadwaita comes from your distribution, not from this app. '
            'Debian 13, Ubuntu 26.04 LTS, Fedora 42, openSUSE Tumbleweed and '
            'Arch all ship 1.7 or newer; Ubuntu 24.04 LTS and Debian 12 do '
            'not.\n\n'
            'Releases before v0.6.0 do start on an older libadwaita, but the '
            'Signal Paths page will not open.'
            % (_fmt(MIN_ADW), _fmt(found)))


def cairo_problem():
    """(title, body) when this machine can't draw the app's graphics, else None.

    Asks for the exact thing the drawing uses.  Every `set_draw_func` callback
    is handed a `cairo.Context`, which PyGObject can only pass to Python
    through its cairo bridge, `gi._gi_cairo` — and `require_foreign('cairo')`
    loads exactly that.  `import cairo` succeeding proves nothing: pycairo and
    the bridge are separate packages on Debian, Ubuntu and openSUSE, and with
    pycairo alone the window opens and every meter, sparkline and the Patchbay
    stays blank, logging a TypeError per frame (issue #21).  Without pycairo as
    well, the app dies on its first `import cairo`.  This catches both.
    """
    try:
        import gi
        gi.require_foreign('cairo')
    except Exception as exc:
        return ('Cairo support could not be loaded',
                'PipeWire Controller draws its level meters, graphs and '
                'Patchbay with cairo, through PyGObject\'s cairo support, and '
                'that could not be loaded here.\n\n'
                'It is packaged by your distribution, and on some it is a '
                'package of its own: python3-gi-cairo on Debian and Ubuntu, '
                'python3-gobject-cairo on openSUSE. On Arch install '
                'python-cairo, and on Fedora python3-gobject.\n\n'
                'Python reported: %s' % exc)
    return None


def _report_terminal(title: str, body: str) -> None:
    sys.stderr.write('\n%s\n\n%s\n\n%s\n\n' % (title, body, REPO))


def _report_window(title: str, body: str) -> bool:
    """Show the message in a window.  Core GTK 4.0 widgets only — a machine
    that fails the libadwaita check may well be too old for Adw.AlertDialog
    (1.5) as well, and a desktop-entry launch has no stderr anyone will read.

    Returns False when there is no display to draw on, e.g. over ssh.
    """
    try:
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
    except Exception:
        return False

    # There is no usable "is there a display" probe here, which is worth
    # knowing before anyone tries to add one: measured on GTK 4 / PyGObject
    # 3.5x, `Gtk.init_check()` and `Gtk.is_initialized()` both answer True on a
    # machine with no display at all — is_initialized() even answers True
    # before init is called — and PyGObject then refuses to build the widget
    # anyway.  So building it *is* the test, and the terminal message has
    # already gone out by the time we get here.
    shown = []

    def on_activate(app):
        try:
            win = Gtk.Window(application=app, title='PipeWire Controller',
                             resizable=False, default_width=460)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            for side in ('top', 'bottom', 'start', 'end'):
                getattr(box, 'set_margin_' + side)(24)

            # Markup rather than a style class: without the libadwaita
            # stylesheet loaded, `title-2` and friends resolve to nothing.
            head = Gtk.Label(xalign=0, wrap=True, use_markup=True,
                             label='<span size="large" weight="bold">%s</span>'
                                   % title)
            text = Gtk.Label(xalign=0, wrap=True, label=body)
            text.set_max_width_chars(52)

            link = Gtk.LinkButton(uri=REPO, label=REPO, halign=Gtk.Align.START)

            close = Gtk.Button(label='Close', halign=Gtk.Align.END)
            close.connect('clicked', lambda *_: win.close())

            for w in (head, text, link, close):
                box.append(w)
            win.set_child(box)
            win.present()
            shown.append(True)
        except Exception:
            app.quit()

    try:
        app = Gtk.Application(application_id='io.github.knightinfected.'
                                             'PipeWireControlCenter.VersionError')
        app.connect('activate', on_activate)
        app.run([])
    except Exception:
        return False
    return bool(shown)


def require() -> None:
    """Report and exit when this machine can't run the app.  Otherwise return.

    Safe to call more than once — the launcher calls it before importing the
    app at all, in case a module ever grows a too-new symbol at import scope.
    """
    found = problem() or cairo_problem()
    if found is None:
        return
    title, body = found
    _report_terminal(title, body)
    _report_window(title, body)
    raise SystemExit(1)
