# RPM spec for Fedora, with openSUSE branches.
#
# The application tree is installed to /usr/share/pipewire-control-center and
# reached through symlinks in /usr/bin — the same layout as the AUR and Debian
# packages, so a bug report names the same paths on every distro. The launcher
# resolves its own symlink with realpath() before extending sys.path, so both
# entry points import pwctl correctly.
#
# Build one package per Fedora release: the bytecode is compiled for the
# Python of the release it is built on, and Fedora releases do not all share
# one. From a checkout, with REF a tag or branch and V this Version:
#
#   git archive --prefix="PipeWireController-$V/" \
#       -o "pipewire-control-center-$V.tar.gz" "$REF"
#   rpmbuild -ba --define "_sourcedir $PWD" packaging/rpm/pipewire-control-center.spec
#
# For a release, Source0 is the GitHub tag tarball, which unpacks to the same
# PipeWireController-<version>/ directory.

%global appid io.github.knightinfected.PipeWireControlCenter
%global forgename PipeWireController

Name:           pipewire-control-center
# Must equal __version__ in pwctl/__init__.py, or be a snapshot of it
# (0.6.2^git20261105, which sorts above 0.6.2 and below 0.6.3). %%prep
# refuses anything else.
Version:        0.6.3
Release:        1%{?dist}
Summary:        GTK4/libadwaita control center for PipeWire

License:        GPL-3.0-or-later
URL:            https://github.com/knightinfected/%{forgename}
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  desktop-file-utils
%if 0%{?suse_version}
BuildRequires:  fdupes
BuildRequires:  python3
%else
BuildRequires:  python3-devel
%endif

# Pure Python. Nothing is linked, so every dependency is a runtime one.
Requires:       python3 >= 3.10
Requires:       pipewire >= 1.0
Requires:       wireplumber >= 0.5

%if 0%{?suse_version}
# openSUSE splits the introspection typelibs out of the libraries, the cairo
# bridge out of PyGObject, and names the pw-* tools package differently.
Requires:       libadwaita-1-0 >= 1.7
Requires:       typelib-1_0-Adw-1 >= 1.7
Requires:       typelib-1_0-Gtk-4_0
Requires:       python3-gobject
Requires:       python3-gobject-cairo
Requires:       python3-numpy
Requires:       python3-SoundFile
# pw-dump, pw-cli, pw-link, pw-top, pw-metadata, pw-record, pw-play.
Requires:       pipewire-tools
%else
Requires:       gtk4
Requires:       libadwaita >= 1.7
# The cairo-1.0 typelib, which GTK 4's own typelib needs. Fedora 44's
# python3-gobject stopped pulling it in, and the app then dies at import.
Requires:       gobject-introspection
Requires:       python3-gobject
# paths_page imports cairo at module scope, so the app needs it to start.
Requires:       python3-cairo
Requires:       python3-numpy
Requires:       python3-soundfile
# pw-dump, pw-cli, pw-link, pw-top, pw-metadata, pw-record, pw-play.
Requires:       pipewire-utils
%endif

# Pulse compatibility keeps PulseAudio clients behaving like native ones, and
# pactl carries Bluetooth codec switching and the per-channel balance on sound
# cards. xdg-utils backs the Tools page's open-folder buttons.
Recommends:     pipewire-pulseaudio
Recommends:     pulseaudio-utils
Recommends:     xdg-utils
%if 0%{?fedora}
# Fedora packages PipeWire's LV2 and SOFA filter-graph backends on their own.
# Without them LV2 effects and the SOFA surround templates are greyed out.
Recommends:     pipewire-module-filter-chain-lv2
Recommends:     pipewire-module-filter-chain-sofa
Suggests:       lsp-plugins-lv2
Suggests:       lsp-plugins-ladspa
Suggests:       Carla
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
# The version lives in pwctl/__init__.py alone. Refuse to build a package
# whose version differs from the one the app reports.
app_version=$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" pwctl/__init__.py)
case "%{version}" in
  "$app_version"|"$app_version^"*) ;;
  *) echo "The spec's Version is %{version} but pwctl/__init__.py is" \
          "'$app_version' - update Version and add a %%changelog entry" >&2
     exit 1 ;;
esac
# A tarball built from a working tree can carry byte-compiled caches.
find pwctl -name __pycache__ -type d -prune -exec rm -rf {} +

%build
# Nothing to build.

%install
install -d %{buildroot}%{_datadir}/%{name}
cp -r pwctl %{buildroot}%{_datadir}/%{name}/
install -Dm755 pipewire-control-center \
    %{buildroot}%{_datadir}/%{name}/pipewire-control-center
# The system Python, never whichever python3 a venv puts first on PATH.
# Fedora rewrites this by itself; openSUSE does not.
sed -i '1s|^#!/usr/bin/env python3$|#!/usr/bin/python3|' \
    %{buildroot}%{_datadir}/%{name}/pipewire-control-center

install -d %{buildroot}%{_bindir}
ln -sf %{_datadir}/%{name}/pipewire-control-center \
    %{buildroot}%{_bindir}/%{name}
# Short alias. Deliberately not `pwctl`: one transposition from `wpctl`,
# WirePlumber's real CLI, and `pw-*` is PipeWire's own tool prefix.
ln -sf %{_datadir}/%{name}/pipewire-control-center %{buildroot}%{_bindir}/pwcc

install -Dm644 %{appid}.desktop \
    %{buildroot}%{_datadir}/applications/%{appid}.desktop

# A private tree outside site-packages is not byte-compiled automatically.
# Compiling it here means the package owns its caches: a user's Python cannot
# write them under /usr/share, and one run as root would otherwise leave
# files behind that removing the package does not delete.
%if 0%{?suse_version}
# openSUSE's rpm moves every file's time back to the build's start
# (build_mtime_policy clamp_to_buildtime), which leaves timestamp-checked
# bytecode stale: Python would recompile on every start. A hash-checked
# cache stays valid. fdupes hard-links the byte-identical optimised copies.
python3 -m compileall -q -f -o 0 -o 1 --invalidation-mode checked-hash \
    -s %{buildroot} -p / %{buildroot}%{_datadir}/%{name}/pwctl
%fdupes %{buildroot}%{_datadir}/%{name}
%else
%py_byte_compile %{python3} %{buildroot}%{_datadir}/%{name}/pwctl
%endif

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
* Fri Oct 30 2026 knightinfected <hmzmahmood5@gmail.com> - 0.6.3-1
- New upstream release. See CHANGELOG.md.

* Thu Oct 29 2026 knightinfected <hmzmahmood5@gmail.com> - 0.6.2-1
- New upstream release. See CHANGELOG.md.
- First release published as RPM packages, attached to the GitHub release.

* Thu Oct 29 2026 knightinfected <hmzmahmood5@gmail.com> - 0.6.1^git20261029-1
- Initial RPM packaging, built and installed on Fedora 43, 44 and 45 and
  openSUSE Tumbleweed. A snapshot of main after 0.6.1, for testing.
