"""Explicit native build contracts for SQLite and its small dependency closure.

These contracts describe commands Forge actually runs, rather than the extra
outputs and build machinery used by a distribution's debian/rules.
"""
from __future__ import annotations

import tarfile

from .storage import ForgeError


ADAPTERS = {"sqlite3": "sqlite-native", "readline": "readline-native",
            "ncurses": "ncurses-native", "tcl8.6": "tcl-native", "zlib": "zlib-native"}


OUTPUTS = {
    "sqlite3": {"sqlite3", "libsqlite3-0", "libsqlite3-dev"},
    "readline": {"libreadline-dev", "libreadline8", "libreadline8t64", "libhistory8", "libhistory8t64"},
    "ncurses": {"libncurses-dev", "libncurses5-dev", "libncursesw5-dev", "libtinfo-dev", "ncurses-dev",
                "libncurses6", "libncursesw6", "libtinfo6", "ncurses-bin", "ncurses-base", "ncurses-term"},
    "tcl8.6": {"tcl8.6", "tcl8.6-dev", "libtcl8.6"},
    "zlib": {"zlib1g", "zlib1g-dev"},
}


# Run configure in a separate directory without using shell command strings.
CONFIGURE = ("from pathlib import Path; import os,sys; "
             "p=Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True); "
             "os.chdir(p); os.execv(sys.argv[2],sys.argv[2:])")


def _configure(directory, executable, *options):
    return ["${python}", "-c", CONFIGURE, directory, executable, "--prefix=${prefix}", *options]


def _tcl_check(binary, *, timezone=False):
    program = "puts [info patchlevel]\n"
    if timezone:
        program += "puts [clock format 0 -timezone UTC -format %Y]\n"
    # repr inserts exactly one level of Python escaping into the generated
    # command. The Tcl process must receive line breaks, not literal '\\n'.
    script = ("import subprocess,sys; r=subprocess.run([sys.argv[1]],input=" + repr(program) +
              ",capture_output=True,text=True); lines=r.stdout.splitlines(); "
              "assert r.returncode==0 and lines and lines[0].startswith('8.6.'), r.stdout+r.stderr")
    if timezone:
        script += "; assert len(lines)>1 and lines[1]=='1970', r.stdout+r.stderr"
    return ["${python}", "-c", script, binary]


def recipe(profile, row, source, backend, archive, *, version):
    project = row["Package"]
    adapter = ADAPTERS.get(project)
    if not adapter:
        return None
    requested = profile.get("package", profile["name"])
    if requested not in OUTPUTS[project]:
        raise ForgeError(f"The {project} native contract does not provide {requested}; this output needs a dedicated adapter.")
    expected = "cmake" if project == "zlib" else "autotools:unix" if project == "tcl8.6" else "autotools"
    if backend != expected:
        raise ForgeError(f"{project} source layout changed; the native adapter expects {expected}, found {backend}.")
    result = {"schema_version": 2, "name": profile["name"], "version": version,
              "description": f"Native {project} libraries and tools", "source": source,
              "requires_tools": ["make", "cc"], "provenance": {"build_adapter": adapter},
              "environment": {"prepend": {"PATH": ["${prefix}/bin"], "CMAKE_PREFIX_PATH": ["${prefix}"],
                  "PKG_CONFIG_PATH": ["${prefix}/lib/pkgconfig"], "CPATH": ["${prefix}/include"],
                  "LIBRARY_PATH": ["${prefix}/lib"], "LD_LIBRARY_PATH": ["${prefix}/lib"]}}}
    if project == "sqlite3":
        # Tcl is needed to generate the amalgamation, even with bindings disabled.
        tcl = "${dep:tcl8.6}/bin/tclsh8.6"
        result.update(build=[["${source}/configure", "--prefix=${prefix}", "--disable-tcl", "--enable-readline", "--disable-editline", "TCLSH_CMD=" + tcl],
                             ["make", "-j${jobs}", "TCLSH_CMD=" + tcl]],
                      install=[["make", "install", "TCLSH_CMD=" + tcl]],
                      dependency_check=[_tcl_check(tcl)],
                      artifacts=["bin/sqlite3", "include/sqlite3.h", "lib/libsqlite3.a", "lib/pkgconfig/sqlite3.pc"],
                      check=[["${python}", "-c", "import subprocess,sys; r=subprocess.run([sys.argv[1],':memory:','select 6*7;'],capture_output=True,text=True); assert r.returncode==0 and r.stdout.strip()=='42',r.stdout+r.stderr", "${prefix}/bin/sqlite3"]])
    elif project == "readline":
        result.update(build=[["${source}/configure", "--prefix=${prefix}", "--with-curses"], ["make", "-j${jobs}"]],
                      # GNU make marks this documentation target already handled;
                      # library, header and pkg-config install targets still run.
                      install=[["make", "-o", "install-doc", "install"]],
                      artifacts=["include/readline/readline.h", "lib/libreadline.a", "lib/libhistory.a", "lib/pkgconfig/readline.pc"])
    elif project == "ncurses":
        common = ["--with-shared", "--without-debug", "--without-ada", "--without-cxx", "--without-cxx-binding",
                  "--without-manpages", "--without-tests", "--without-gpm", "--with-termlib", "--enable-pc-files",
                  "--with-pkg-config-libdir=${prefix}/lib/pkgconfig", "--libdir=${prefix}/lib"]
        result.update(build=[], install=[], artifacts=["include/curses.h", "include/ncursesw/curses.h", "lib/libncurses.a", "lib/libncursesw.a", "bin/tic"])
        for mode, flags in (("narrow", ["--disable-widec", "--enable-overwrite"]), ("wide", ["--enable-widec", "--disable-overwrite"])):
            directory = "${build}/" + mode
            result["build"] += [_configure(directory, "${source}/configure", *common, *flags), ["make", "-C", directory, "-j${jobs}"]]
            result["install"].append(["make", "-C", directory, "install"])
    elif project == "tcl8.6":
        # The native install includes upstream's timezone database, without the
        # Debian-specific dpkg-dev/tzdata runtime configuration.
        with tarfile.open(archive, "r:*") as package:
            has_tzdata = any(m.isfile() and "/library/tzdata/" in m.name for m in package)
        if not has_tzdata:
            raise ForgeError("Tcl archive lacks bundled timezone data; a timezone adapter is required.")
        result.update(build=[_configure("${build}", "${source}/unix/configure", "--enable-threads", "--with-tzdata=yes"), ["make", "-C", "${build}", "-j${jobs}"]],
                      install=[["make", "-C", "${build}", "install"]],
                      artifacts=["bin/tclsh8.6", "include/tcl.h", "lib/tcl8.6/tzdata/UTC"],
                      check=[_tcl_check("${prefix}/bin/tclsh8.6", timezone=True)])
    else:  # zlib: CMake builds only the selected native architecture, no multilib.
        result["requires_tools"] = ["cmake", "cc"]
        result.update(build=[["cmake", "-S", "${source}", "-B", "${build}", "-DCMAKE_INSTALL_PREFIX=${prefix}", "-DCMAKE_INSTALL_LIBDIR=lib", "-DCMAKE_BUILD_TYPE=Release"], ["cmake", "--build", "${build}", "--parallel", "${jobs}"]],
                      install=[["cmake", "--install", "${build}"]], artifacts=["include/zlib.h", "lib/pkgconfig/zlib.pc"])
    if "check" not in result:
        result["check"] = [["${python}", "-c", "from pathlib import Path; import sys; p=Path(sys.argv[1]); assert all((p/n).is_file() for n in sys.argv[2:]),'Required native development files missing'", "${prefix}", *result["artifacts"]]]
    result["probe"] = list(result["check"])
    return result


def dependency(adapter, kind, alternatives):
    """Return the native relationship and a recorded decision, if it changes."""
    names = {a["name"] for a in alternatives}
    skip = set()
    reason = None
    if adapter in ADAPTERS.values() and kind == "build":
        skip |= {"autoconf", "automake", "libtool", "autoconf-dickey"}
        reason = "Uses shipped configure or CMake files; does not regenerate Debian build machinery"
    if adapter == "sqlite-native" and kind == "build":
        skip |= {"chrpath", "lynx"}
        if names & {"chrpath", "lynx"}:
            reason = "Native make/install does not run Debian chrpath or HTML-to-text documentation steps"
        if "tcl8.6-dev" in names:
            changed = [{**a, "name": "tcl8.6"} if a["name"] == "tcl8.6-dev" else a for a in alternatives]
            return changed, "Tcl interpreter generates SQLite sources; --disable-tcl excludes Tcl extension headers"
    if adapter == "readline-native" and kind == "build":
        skip.add("texinfo")
        if "texinfo" in names:
            reason = "make -o install-doc install excludes documentation; native libraries and headers remain enabled"
    if adapter == "ncurses-native" and kind == "build":
        skip |= {"libgpm-dev", "pkgconf", "pkg-config"}
        if names & {"libgpm-dev", "pkgconf", "pkg-config"}:
            reason = "--without-gpm and native pkg-config file generation need no GPM library or pkg-config parser"
    if adapter == "readline-native" and kind == "run" and "readline-common" in names:
        return None, "Native Readline uses built-in defaults and user/site inputrc; no Debian configuration package is installed"
    if adapter == "tcl-native" and kind == "run" and names & {"dpkg-dev", "tzdata"}:
        return None, "Native tclConfig does not use dpkg-dev; --with-tzdata=yes installs the bundled Tcl timezone database"
    if names & skip:
        return None, reason
    return alternatives, None
