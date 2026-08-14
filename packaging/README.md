# Packaging

Distribution packaging for PipeWire Controller. The Arch package lives on the
AUR as [`pipewire-control-center`][aur]; this directory holds the Debian and
RPM equivalents.

All three install the same way, deliberately:

```
/usr/share/pipewire-control-center/       the application tree (pwctl/ + launcher)
/usr/bin/pipewire-control-center          symlink to the launcher
/usr/bin/pwcc                             short alias
/usr/share/applications/io.github.knightinfected.PipeWireControlCenter.desktop
```

Keeping the layout identical everywhere means a bug report names the same paths
whichever distro it came from. The launcher calls `realpath()` on itself before
extending `sys.path`, so both `/usr/bin` entries import `pwctl` correctly —
`abspath()` does not resolve symlinks and would break both (this was the
v0.1.1 fix; keep `realpath`).

Nothing is compiled. The packages are `all` / `noarch`.

## Why not Flatpak

Investigated and rejected on 2026-08-11, having measured it rather than assumed.
Two findings decided it:

- **Flathub does not accept this class of application.** Their requirements
  state that "system utilities which are generally used on host will not be
  accepted" and that applications relying "on host components ... for core
  functionality will not be accepted". Every PipeWire application already on
  Flathub (EasyEffects, Helvum, coppwr, qpwgraph) ships only
  `--filesystem=xdg-run/pipewire-0` and is a PipeWire *client*; none of them
  configures the host audio stack.
- **The sandbox breaks features.** Measured inside `org.freedesktop.Sdk//25.08`
  against a live PipeWire 1.6.8 daemon: `pw-dump`, `pw-metadata` (including
  per-node writes, which is how streams are moved) and `pw-link` all work, but
  `pw-top` returns nothing at all — exit 0, zero rows, with the manager socket
  mounted and with `PIPEWIRE_REMOTE` pointed at it — which removes the entire
  Monitor page. The runtime also ships PipeWire 1.4.9, whose `pw-record` has no
  `--container` flag, so the level meters fail outright.

Beyond both: filter chains, virtual devices, enhancements and signal paths run
as **host systemd user units** that must outlive the window, survive reboot and
follow host PipeWire restarts. Running those helpers inside a sandbox makes
them die with the application.

## Debian / Ubuntu

Needs **Debian 13+ or Ubuntu 24.04+** — earlier releases ship libadwaita below
1.4. Debian 12 (bookworm) has 1.2.2 and will not work.

The source format is `3.0 (native)`, so the changelog version carries **no
Debian revision** (`0.5.0`, not `0.5.0-1`) — `dpkg-buildpackage` rejects a
revision on a native package. Keep it that way when bumping.

From a checkout, with `devscripts` and `debhelper` installed:

```bash
cp -r packaging/debian debian
dpkg-buildpackage -us -uc -b
sudo apt install ../pipewire-control-center_*_all.deb
```

The package `Conflicts`/`Replaces`/`Provides` `pipewire-controller`, so anyone
who built the pre-rename package upgrades cleanly.

## Fedora / COPR

```bash
spectool -g -R packaging/rpm/pipewire-control-center.spec
rpmbuild -ba packaging/rpm/pipewire-control-center.spec
```

For COPR, upload the spec and let it fetch `Source0` from the GitHub tag.

## openSUSE

The spec carries `%if 0%{?suse_version}` branches for the packages openSUSE
names differently — it splits the introspection typelibs out of the libraries
(`typelib-1_0-Gtk-4_0`, `typelib-1_0-Adw-1`) and the PipeWire CLI tools live in
`pipewire-tools` rather than `pipewire-utils`.

**Those names are unverified** — they have not been built on a real openSUSE
system. Confirm them before publishing to OBS.

## Verification status

| Package | Layout checked | Built | Installed |
|---|---|---|---|
| Arch (AUR) | yes | yes | yes |
| Debian/Ubuntu | yes | no | no |
| Fedora | yes | no | no |
| openSUSE | no | no | no |

The Debian and RPM install steps have been dry-run into a staging directory and
produce the correct tree, but neither has been through `dpkg-buildpackage` or
`rpmbuild` — this is an Arch machine with no `dpkg`, `rpmbuild` or usable
container runtime. Build both in a container before publishing.

[aur]: https://aur.archlinux.org/packages/pipewire-control-center
