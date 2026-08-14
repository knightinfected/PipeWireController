# RPM spec for Fedora / COPR, with openSUSE conditionals.
#
# The application tree is installed to %{_datadir}/%{name} and reached through
# symlinks in %{_bindir} — the same layout as the AUR and Debian packages, so a
# bug report names the same paths on every distro. The launcher resolves its
# own symlink with realpath() before extending sys.path, so both entry points
# import pwctl correctly.

%global appid io.github.knightinfected.PipeWireControlCenter
%global forgename PipeWireController

Name:           pipewire-control-center
Version:        0.5.0
Release:        1%{?dist}
Summary:        GTK4/libadwaita control center for PipeWire

License:        GPL-3.0-or-later
URL:            https://github.com/knightinfected/%{forgename}
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  desktop-file-utils

# Pure Python — nothing is compiled, so these are runtime-only.
Requires:       python3 >= 3.10
Requires:       gtk4
Requires:       libadwaita >= 1.4
Requires:       pipewire >= 1.0
Requires:       wireplumber >= 0.5

%if 0%{?suse_version}
# openSUSE splits the introspection typelibs out of the libraries themselves.
# NOT yet verified on a real openSUSE build — confirm before publishing to OBS.
Requires:       typelib-1_0-Gtk-4_0
Requires:       typelib-1_0-Adw-1
Requires:       python3-gobject
Requires:       python3-gobject-cairo
Requires:       python3-numpy
Requires:       python3-soundfile
Requires:       pipewire-tools
%else
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       python3-numpy
Requires:       python3-soundfile
# pw-dump, pw-cli, pw-link, pw-top, pw-metadata, pw-record, pw-play.
Requires:       pipewire-utils
%endif

# Pulse compatibility carries the Bluetooth codec switching (pactl
# send-message) and keeps PulseAudio clients behaving like native ones.
%if 0%{?fedora}
Recommends:     pipewire-pulseaudio
Recommends:     pulseaudio-utils
Suggests:       lsp-plugins
Suggests:       carla
%endif

%description
A graphical control center for PipeWire that exposes everything under
PipeWire's Configuration documentation as toggles and dropdowns, so none of it
has to be reached by hand-editing .conf files.

Signal Paths builds the route audio actually takes — sources with their own
effect chain sending into mixes with their own chain and outputs. Alongside it
are a parametric equalizer with live A/B compare, microphone cleanup, live
level meters on every volume control, a drag-to-connect patchbay, performance
monitoring, virtual devices, LADSPA/LV2 effect inserts, per-application
policies, routing snapshots and HRIR virtual surround.

All persistent changes are written as drop-in configuration files, never to the
base files, so every setting can be reverted cleanly.

%prep
%autosetup -n %{forgename}-%{version}
# A tarball built from a working tree can carry byte-compiled caches.
find pwctl -name __pycache__ -type d -prune -exec rm -rf {} +

%build
# Nothing to build.

%install
install -d %{buildroot}%{_datadir}/%{name}
cp -r pwctl %{buildroot}%{_datadir}/%{name}/
install -Dm755 pipewire-control-center \
    %{buildroot}%{_datadir}/%{name}/pipewire-control-center

install -d %{buildroot}%{_bindir}
ln -sf %{_datadir}/%{name}/pipewire-control-center \
    %{buildroot}%{_bindir}/%{name}
# Short alias. Deliberately not `pwctl`: one transposition from `wpctl`,
# WirePlumber's real CLI, and `pw-*` is PipeWire's own tool prefix.
ln -sf %{_datadir}/%{name}/pipewire-control-center %{buildroot}%{_bindir}/pwcc

install -Dm644 %{appid}.desktop \
    %{buildroot}%{_datadir}/applications/%{appid}.desktop

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop

%files
%license LICENSE
%doc README.md CHANGELOG.md
%{_datadir}/%{name}/
%{_bindir}/%{name}
%{_bindir}/pwcc
%{_datadir}/applications/%{appid}.desktop

%changelog
* Wed Aug 12 2026 knightinfected <hmzmahmood5@gmail.com> - 0.5.0-1
- Initial RPM packaging.
- Renamed from pipewire-controller: an unrelated project holds that name on
  PyPI and installs the same /usr/bin binary and .desktop filename.
