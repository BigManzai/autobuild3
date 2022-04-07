# $LicenseInfo:firstyear=2010&license=mit$
# Copyright (c) 2010, Linden Research, Inc.
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
# $/LicenseInfo$

import os
import sys
from ast import literal_eval
from collections import OrderedDict
import errno
import itertools
import json
import logging
from pprint import pformat
import re
import shutil
import stat
import string
import subprocess
import tempfile

from . import common
from . import autobuild_base

logger = logging.getLogger('autobuild.source_environment')

# for the time being, we expect that we're checked out side-by-side with
# parabuild buildscripts, so back up a level to find $helper.
get_params = None
helper = os.path.join(os.path.dirname(__file__),
                      os.pardir,
                      os.pardir,
                      'buildscripts/hg/bin')
if os.path.exists(helper):
    # Append helper to sys.path.
    _helper_idx = len(sys.path)
    sys.path.append(helper)
    assert sys.path[_helper_idx] == helper

    try:
        import get_params
        logger.info("found get_params: '%s'" % get_params.__file__)
    except ImportError:
        # restore original sys.path value
        assert sys.path[_helper_idx] == helper
        del sys.path[_helper_idx]

class SourceEnvError(common.AutobuildError):
    pass

_VSxxxCOMNTOOLS_re = re.compile(r"VS(.*)COMNTOOLS$")
_VSxxxCOMNTOOLS_st = "VS%sCOMNTOOLS"

# From VS 2017 on, we have to look for vswhere.exe at this canonical path to
# discover where the Visual Studio install is.
# https://stackoverflow.com/a/44323312
# https://blogs.msdn.microsoft.com/heaths/2017/02/25/vswhere-available/
# It's plausible to make these getenv() and join() calls even on a non-Windows
# system, as long as we don't assume the resulting path actually exists.

# Ab VS 2017 m�ssen wir auf diesem kanonischen Pfad nach vswhere.exe suchen
# Finden Sie heraus, wo sich die Visual Studio-Installation befindet.
# https://stackoverflow.com/a/44323312
# https://blogs.msdn.microsoft.com/heaths/2017/02/25/vswhere-available/
# Es ist plausibel, diese getenv()- und join()-Aufrufe auch auf einem Nicht-Windows durchzuf�hren
# System, solange wir nicht davon ausgehen, dass der resultierende Pfad tats�chlich existiert.
_VSWHERE_PATH = os.path.join(os.getenv("ProgramFiles(x86)", ""), "Microsoft Visual Studio", "Installer", "vswhere.exe")

def _available_vsvers():
    # First check all the VSxxxCOMNTOOLS environment variables.
    # �berpr�fen Sie zuerst alle VSxxxCOMNTOOLS-Umgebungsvariablen.
    candidates = set(match.group(1)
                     for match in (_VSxxxCOMNTOOLS_re.match(k) for k in os.environ)
                     if match)
    # Now, if there's a vswhere.exe on this system, ask it to enumerate VS
    # versions too. Use a set to unify any duplication.

    # Nun, wenn es auf diesem System eine vswhere.exe gibt, bitten Sie sie, VS aufzuz�hlen
    # Versionen auch. Verwenden Sie einen Satz, um jede Duplizierung zu vereinheitlichen.
    try:
        versions = subprocess.check_output(
            # Unless you add -legacy, vswhere.exe doesn't report anything
            # older than VS 2015. However:
            # Error 0x57: The "legacy" parameter cannot be specified with either the "products" or "requires" parameter

            # Sofern Sie nicht -legacy hinzuf�gen, meldet vswhere.exe nichts
            # �lter als VS 2015. Allerdings:
            # Fehler 0x57: Der "legacy" Parameter kann weder mit dem "products" noch mit dem "requires" Parameter angegeben werden
            [_VSWHERE_PATH, '-all', # '-legacy',
             '-products', '*',
             '-requires', 'Microsoft.Component.MSBuild',
             '-property', 'installationVersion'],
            universal_newlines=True)
    except OSError as err:
        if err.errno != errno.ENOENT:
            raise
        # Nonexistence of the vswhere.exe utility is normal for older VS
        # installs.

        # Das Nichtvorhandensein des Dienstprogramms vswhere.exe ist f�r �ltere VS normal
        # Installationen.
    except subprocess.CalledProcessError as err:
        # We were able to find it, but it was unsuccessful. vswhere reports
        # important error information on stdout, captured as err.output.

        # Wir konnten es finden, aber es war erfolglos. vswhere meldet wichtige Fehlerinformationen auf stdout, erfasst als err.output.
        raise SourceEnvError('{}:\n{}'.format(err, err.output))
    else:
        # 'versions' is (e.g.):
        # 15.8.28010.2016
        # 12.0
        # Have to convert from (e.g.) 15.8.28010.2016 to 158 to align with
        # AUTOBUILD_VSVER convention. In other words, match only ONE digit
        # after the dot.

        # 'Versionen' ist (zB):
        # 15.8.28010.2016
        # 12.0
        # Muss von (z. B.) 15.8.28010.2016 in 158 konvertiert werden, um sich auszurichten
        # AUTOBUILD_VSVER-Konvention. Mit anderen Worten, stimmen Sie nur mit EINER Ziffer �berein
        # nach dem Punkt.
        pattern = re.compile(r'([0-9]+)\.([0-9])')
        candidates.update(''.join(match.group(1,2))
                          for match in (pattern.match(line)
                                        for line in versions.splitlines())
                          if match)
    # Caller expects a list; sorted() is documented to return a list.
    # Anrufer erwartet eine Liste; sorted() ist dokumentiert, um eine Liste zur�ckzugeben.
    return sorted(candidates)

def load_vsvars(vsver):
    """
    Return a dict of environment variables set by the applicable Visual Studio
    vcvars*.bat file. Note: any variable identical to the corresponding
    current os.environ entry is assumed to be inherited rather than set. The
    returned dict contains only variables added or changed by vcvars*.bat.

    The relevant Visual Studio version is specified by the vsver parameter,
    according to Microsoft convention:

    '100' selects Visual Studio 2010
    '120' selects Visual Studio 2013 (version 12.0)
    etc.

    os.environ['AUTOBUILD_ADDRSIZE'] (set by common.establish_platform()) also
    participates in the selection of the .bat file. When it's '32', the .bat
    file will set variables appropriate for a 32-bit build, and similarly when
    it's '64'.

     Gibt ein Diktat von Umgebungsvariablen zur�ck, die vom entsprechenden Visual Studio festgelegt wurden
     vcvars*.bat-Datei. Hinweis: jede Variable identisch mit der entsprechenden
     Es wird davon ausgegangen, dass der aktuelle os.environ-Eintrag geerbt und nicht festgelegt wird. Die
     Das zur�ckgegebene Diktat enth�lt nur Variablen, die von vcvars*.bat hinzugef�gt oder ge�ndert wurden.

     Die relevante Visual Studio-Version wird durch den vsver-Parameter angegeben,
     nach Microsoft-Konvention:

     �100� w�hlt Visual Studio 2010 aus
     '120' w�hlt Visual Studio 2013 (Version 12.0) aus
     usw.

     os.environ['AUTOBUILD_ADDRSIZE'] (festgelegt von common.etabliert_platform()) ebenfalls
     beteiligt sich an der Auswahl der .bat-Datei. Wenn es '32' ist, wird die .bat
     Datei wird Variablen setzen, die f�r einen 32-Bit-Build geeignet sind, und �hnlich wann
     es ist '64'.
    """
    # open question: At what version did Microsoft stop setting VSxxxCOMNTOOLS
    # in favor of the vswhere.exe tool? We're sure that by VS 2017 ('150')
    # there was no VS150COMNTOOLS environment variable, whereas at that point
    # vswhere.exe was installed at the canonical location by Visual Studio.
    # Did VS 2015 still adhere to the VS140COMNTOOLS convention? Adjust the
    # comparison here as necessary.

    # offene Frage: Ab welcher Version hat Microsoft aufgeh�rt VSxxxCOMNTOOLS zu setzen
     # zugunsten des Tools vswhere.exe? Wir sind sicher, dass bis VS 2017 ('150')
     # es gab keine Umgebungsvariable VS150COMNTOOLS, wohingegen zu diesem Zeitpunkt
     # vswhere.exe wurde von Visual Studio am klassisches Muster des Speicherort installiert.
     # Hat sich VS 2015 noch an die VS140COMNTOOLS Konvention gehalten? Verstelle die
     # Vergleich hier nach Bedarf.
    if int(vsver) >= 150:
        # We can't use the VSxxxCOMNTOOLS dodge as we always used to. Use
        # vswhere.exe instead.

        # Wir k�nnen den VSxxxCOMNTOOLS-Dodge nicht mehr verwenden, wie wir es immer getan haben. Verwenden
        # stattdessen vswhere.exe.
        via = os.path.basename(_VSWHERE_PATH)
        # Split (e.g.) '155' into '15' and '5'
        major, minor = vsver[:-1], vsver[-1:]
        # If user specifies vsver='155', but VS 2017 isn't installed, but VS
        # 2019 is, s/he isn't going to be satisfied with VS 2019. Limit the
        # allowable range of responses only to the next version up, e.g.
        # -version [15.5,16.0)

        # Wenn der Benutzer vsver='155' angibt, aber VS 2017 nicht installiert ist, aber VS 2019, 
        # wird er/sie mit VS 2019 nicht zufrieden sein. 
        # Beschr�nken Sie den zul�ssigen Bereich von Antworten nur auf die n�chsth�here Version, z.B -Version [15.5,16.0)
        nextver = int(major) + 1
        version = '[{}.{},{}.0)'.format(major, minor, nextver)
        try:
            # don't pass text=True or universal_newlines=True: we want bytes
            # to pass to json.loads()

            # �bergeben Sie nicht text=True oder universal_newlines=True: Wir m�chten, dass Bytes an json.loads() �bergeben werden.
            raw = subprocess.check_output(
                [_VSWHERE_PATH, '-version', version, '-products', '*',
                 '-requires', 'Microsoft.Component.MSBuild', '-utf8',
                 '-format', 'json'], universal_newlines=True).rstrip()
            installs = json.loads(raw)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
            raise SourceEnvError('AUTOBUILD_VSVER={} unsupported, '
                                 'is Visual Studio {} installed? (%s not found)'
                                 .format(vsver, vsver, _VSWHERE_PATH))
        except subprocess.CalledProcessError as err:
            # Don't forget that vswhere reports error information on stdout.
            # Vergessen Sie nicht, dass vswhere Fehlerinformationen auf stdout meldet.
            raise SourceEnvError('AUTOBUILD_VSVER={} unsupported: {}:\n{}'
                                 .format(vsver, err, err.output))
        except ValueError as err:
            raise SourceEnvError("Can't parse vswhere output:\n" + raw)

        if not installs:
            # vswhere terminated with 0, yet its output is empty.
            # vswhere mit 0 abgeschlossen, aber seine Ausgabe ist leer.
            raise SourceEnvError('AUTOBUILD_VSVER={} unsupported, '
                                 'is Visual Studio {} installed? (vswhere couldn\'t find)'
                                 .format(vsver, vsver))

        # If we get this far, 'installs' is the output of the above vswhere
        # command. BUT vswhere treats -version as a lower bound: it reports
        # every installed VS version >= -version. That's the reason we request
        # json output, so that for each listed VS install we get the specific
        # version as well as its install directory. Sort on the version string
        # and pick the lowest version >= AUTOBUILD_VSVER. This is necessary
        # because for (e.g.) -version 15.0, the version reported for VS 2017
        # might be '15.8.2'. But what if you have both 15.5 and 15.8?
        # We want a numeric sort, not a string sort, so that "10" sorts
        # later than "8". Since some version components can be reported as
        # (e.g.) "2+28010", don't just pass to int() -- find every cluster of
        # decimal digits and build a list of int()s of those. Thus,
        # "15.8.2+28010.2016" becomes [15, 8, 2, 2810, 2016].

        # Wenn wir so weit kommen, ist 'installs' die Ausgabe des obigen vswhere
         # Befehl. ABER vswhere behandelt -version als Untergrenze: es meldet
         # jede installierte VS-Version >= -version. Das ist der Grund, warum wir darum bitten
         # json-Ausgabe, sodass wir f�r jede aufgelistete VS-Installation die spezifische
         # Version sowie dessen Installationsverzeichnis. Sortieren Sie nach der Versionszeichenfolge
         # und w�hlen Sie die niedrigste Version >= AUTOBUILD_VSVER. Das ist notwendig
         # weil f�r (z. B.) -version 15.0 die f�r VS 2017 gemeldete Version
         # k�nnte '15.8.2' sein. Aber was ist, wenn Sie sowohl 15,5 als auch 15,8 haben?
         # Wir wollen eine numerische Sortierung, keine String-Sortierung, damit "10" sortiert
         # sp�ter als "8". Da einige Versionskomponenten als gemeldet werden k�nnen
         # (z. B.) "2+28010", nicht einfach an int() �bergeben -- jeden Cluster von finden
         # Dezimalziffern und erstelle eine Liste von int()s davon. Daher,
         # "15.8.2+28010.2016" wird zu [15, 8, 2, 2810, 2016].
        installs.sort(key=lambda inst:
                      [int(found.group(0))
                       for found in re.finditer('[0-9]+', inst['catalog']['productDisplayVersion'])])
        where = installs[0]['installationPath']
        # Append the rest of the directory path.
        # H�ngen Sie den Rest des Verzeichnispfads an.
        VCINSTALLDIR = os.path.join(where, 'VC', 'Auxiliary', 'Build')

    else:
        # Older Visual Studio versions use the VSxxxCOMNTOOLS environment
        # variable.
        # �ltere Visual Studio-Versionen verwenden die Umgebungsvariable VSxxxCOMNTOOLS.
        key = _VSxxxCOMNTOOLS_st % vsver
        via = key
        logger.debug("vsver %s, key %s" % (vsver, key))
        try:
            # We've seen traceback output from this if vsver doesn't match an
            # environment variable. Produce a reasonable error message instead.

            # Wir haben eine Traceback-Ausgabe davon gesehen, wenn vsver nicht mit einer Umgebungsvariablen �bereinstimmt. 
            # Produzieren Sie stattdessen eine vern�nftige Fehlermeldung.
            VSxxxCOMNTOOLS = os.environ[key]
        except KeyError:
            candidates = _available_vsvers()
            explain = " (candidates: %s)" % ", ".join(candidates) if candidates \
                      else ""
            raise SourceEnvError('AUTOBUILD_VSVER=%s unsupported, '
                                 'is Visual Studio %s installed?%s' %
                                 (vsver, vsver, explain))

        # VSxxxCOMNTOOLS will be something like:
        # C:\Program Files (x86)\Microsoft Visual Studio 12.0\Common7\Tools\
        # We want to find vcvarsall.bat, which will be somewhere like
        # C:\Program Files (x86)\Microsoft Visual Studio 12.0\VC\vcvarsall.bat
        # Assuming that we can just find %VSxxxCOMNTOOLS%..\..\VC seems a little
        # fragile across installs or (importantly) across future VS versions.
        # Instead, use %VSxxxCOMNTOOLS%VCVarsQueryRegistry.bat to populate
        # VCINSTALLDIR.

        # VSxxxCOMNTOOLS sieht so aus:
         # C:\Program Files (x86)\Microsoft Visual Studio 12.0\Common7\Tools\
         # Wir wollen vcvarsall.bat finden, das irgendwo wie sein wird
         # C:\Program Files (x86)\Microsoft Visual Studio 12.0\VC\vcvarsall.bat
         # Angenommen, wir k�nnen nur %VSxxxCOMNTOOLS%..\..\VC finden, scheint ein wenig zu sein
         # zerbrechlich bei Installationen oder (wichtig) bei zuk�nftigen VS-Versionen.
         # Verwenden Sie stattdessen %VSxxxCOMNTOOLS%VCVarsQueryRegistry.bat zum Ausf�llen
         # VCINSTALLDIR.
        VCVarsQueryRegistry_base = "VCVarsQueryRegistry.bat"
        VCVarsQueryRegistry = os.path.join(VSxxxCOMNTOOLS, VCVarsQueryRegistry_base)
        # Failure to find any of these .bat files could produce really obscure
        # execution errors. Make the error messages as explicit as we can.

        # Wenn keine dieser .bat-Dateien gefunden wird, kann dies zu wirklich obskuren Ausf�hrungsfehlern f�hren. 
        # Machen Sie die Fehlermeldungen so ausf�hrlich und differenziert wie m�glich.
        if not os.path.exists(VCVarsQueryRegistry):
            raise SourceEnvError("%s not found at %s: %s" %
                                 (VCVarsQueryRegistry_base, key, VSxxxCOMNTOOLS))

        # Found VCVarsQueryRegistry.bat, run it.
        vcvars = get_vars_from_bat(VCVarsQueryRegistry)

        # Then we can find %VCINSTALLDIR%vcvarsall.bat.
        try:
            VCINSTALLDIR = vcvars["VCINSTALLDIR"]
        except KeyError:
            raise SourceEnvError("%s did not populate VCINSTALLDIR" % VCVarsQueryRegistry)

    vcvarsall_base = "vcvarsall.bat"
    vcvarsall = os.path.join(VCINSTALLDIR, vcvarsall_base)
    if not os.path.exists(vcvarsall):
        raise SourceEnvError("%s not found at: %s (via %s)" %
                             (vcvarsall_base, VCINSTALLDIR, via))

    # vcvarsall.bat accepts a single argument: the target architecture, e.g.
    # "x86" or "x64".
    # Let KeyError, if any, propagate: lack of AUTOBUILD_ADDRSIZE would be an
    # autobuild coding error. So would any value for that variable other than
    # what's stated below.

    # vcvarsall.bat akzeptiert ein einziges Argument: die Zielarchitektur, z.B. �x86� oder �x64�. 
    # Lassen Sie KeyError, falls vorhanden, propagieren: Das Fehlen von AUTOBUILD_ADDRSIZE w�re ein Autobuild-Codierungsfehler. 
    # Dies gilt auch f�r jeden anderen Wert f�r diese Variable als den unten angegebenen.
    arch = {
        '32': 'x86',
        '64': 'x64',
        }[os.environ["AUTOBUILD_ADDRSIZE"]]
    vcvars = get_vars_from_bat(vcvarsall, arch)

    # Now weed out of vcvars anything identical to OUR environment. Retain
    # only environment variables actually modified by vcvarsall.bat.
    # Capture the list of items up front instead of trying to traverse vcvars
    # while modifying it.

    # Entfernen Sie jetzt aus vcvars alles, was mit UNSERER Umgebung identisch ist. Behalten Sie nur Umgebungsvariablen bei, 
    # die tats�chlich von vcvarsall.bat ge�ndert wurden. 
    # Erfassen Sie die Liste der Elemente im Voraus, anstatt zu versuchen, vcvars zu durchlaufen, w�hrend Sie sie �ndern.
    for var, value in list(vcvars.items()):
        # Bear in mind that some variables were introduced by vcvarsall.bat and
        # are therefore NOT in our os.environ.

        # Denken Sie daran, dass einige Variablen von vcvarsall.bat eingef�hrt wurden und daher NICHT in unserer os.environ sind.
        if os.environ.get(var) == value:
            # Any environment variable from our batch script that's identical
            # to our own os.environ was simply inherited. Discard it.

            # Jede Umgebungsvariable aus unserem Batch-Skript, 
            # die mit unserem eigenen os.environ identisch ist, wurde einfach geerbt. Verwirf es.
            del vcvars[var]
    logger.debug("set by %s %s:\n%s" % (vcvarsall, arch, pformat(vcvars)))

    return vcvars

def get_vars_from_bat(batpath, *args):
    # Invent a temp filename into which to capture our script output. Some
    # versions of vsvars32.bat emit stdout, some don't; we've been bitten both
    # ways. Bypass that by not commingling our desired output into stdout.

    # Erfinden Sie einen tempor�ren Dateinamen, in dem unsere Skriptausgabe erfasst wird. 
    # Einige Versionen von vsvars32.bat geben stdout aus, andere nicht; Wir wurden in beide Richtungen gebissen. 
    # Umgehen Sie das, indem Sie unsere gew�nschte Ausgabe nicht in stdout mischen.
    temp_output = tempfile.NamedTemporaryFile(suffix=".pydata", delete=False)
    temp_output.close()
    try:
        # Write a little temp batch file to set variables from batpath and
        # regurgitate them in a form we can parse.
        # First call batpath to update the cmd shell's environment. Then
        # use Python itself -- not just any Python interpreter, but THIS one
        # -- to format the ENTIRE environment into temp_output.name.
        # In Python 3, os.environ is no longer a simple dict. Explicitly
        # convert to dict so pprint() will emit a form literal_eval() can read.

        # Schreiben Sie eine kleine tempor�re Batch Datei, um Variablen aus batpath zu setzen 
        # und geben Sie sie in einer Form wieder, die wir analysieren k�nnen. 
        # Rufen Sie zuerst batpath auf, um die Umgebung der cmd Shell zu aktualisieren. 
        # Verwenden Sie dann Python selbst � nicht irgendeinen Python-Interpreter, 
        # sondern DIESEN � um die GESAMTE Umgebung in temp_output.name zu formatieren. 
        # In Python 3 ist os.environ kein einfaches Diktat mehr. 
        # Konvertieren Sie explizit in dict, damit pprint() ein Formular ausgibt, das literal_eval() lesen kann.
        temp_script_content = """\
call "%s"%s
"%s" -c "import os, pprint; pprint.pprint(dict(os.environ))" > "%s"
""" % (batpath, ''.join(' '+arg for arg in args), sys.executable, temp_output.name)
        # Specify mode="w" for text mode ("\r\n" newlines); default is binary.
        with tempfile.NamedTemporaryFile(suffix=".cmd", delete=False, mode="w") as temp_script:
            temp_script.write(temp_script_content)
            temp_script_name = temp_script.name
        logger.debug("wrote to %s:\n%s" % (temp_script_name, temp_script_content))

        try:
            # Run our little batch script. Intercept any stdout it produces,
            # which would confuse our invoker, who wants to parse OUR stdout.

            # F�hren Sie unser kleines Batch Skript aus. Jede stdout abfangen, die es erzeugt, 
            # was unseren Aufrufer verwirren w�rde, der UNSERE stdout parsen m�chte.
            cmdline = ['cmd', '/Q', '/C', temp_script_name]
            logger.debug(cmdline)
            script = subprocess.Popen(cmdline,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            logger.debug(script.communicate()[0].rstrip())
            rc = script.wait()
            if rc != 0:
                raise SourceEnvError("%s failed with rc %s" % (' '.join(cmdline), rc))

        finally:
            # Whether or not the temporary script file worked, clean it up.
            # Unabh�ngig davon, ob die tempor�re Skriptdatei funktioniert hat, bereinigen Sie sie.
            os.remove(temp_script_name)

        # Read our temporary output file, knowing that it cannot contain any
        # output produced by batpath itself.

        # Lesen Sie unsere tempor�re Ausgabedatei, wissend, 
        # dass sie keine von batpath selbst erzeugte Ausgabe enthalten kann.
        with open(temp_output.name) as tf:
            raw_environ = tf.read()

    finally:
        # Clean up our temporary output file.
        os.remove(temp_output.name)

    try:
        # trust pprint.pprint() to produce output readable by ast.literal_eval()
        vsvars = literal_eval(raw_environ)
    except Exception:
        # but in case of a glitch, report raw string data for debugging
        logger.debug("pprint output of %s:\n%s" % (batpath, raw_environ))
        raise

    logger.debug("environment from %s:\n%s" % (batpath, pformat(vsvars)))
    return vsvars


def cygpath(*args):
    """run cygpath with specified command-line args, returning its output"""
    cmdline = ["cygpath"] + list(args)
    stdout = subprocess.Popen(cmdline, stdout=subprocess.PIPE, universal_newlines=True) \
                       .communicate()[0].rstrip()
    logger.debug("%s => '%s'" % (cmdline, stdout))
    return stdout


environment_template = """
%(vars)s

    set_build_variables() {
        # set_build_variables is a dead branch of the evolutionary tree. The
        # functionality formerly engaged by the command:
        # set_build_variables convenience Release
        # has now been subsumed into autobuild source_environment itself. But
        # since a number of build-cmd.sh scripts have been tweaked to call
        # set_build_variables, make it produce an explanatory error. While it
        # would be simpler to remove the shell function and produce an error
        # that way, that could leave a developer scrambling to figure out:
        # okay, this line broke, why? Did set_build_variables go away? Did its
        # name change? What replaces it?
        echo "set_build_variables is no longer needed. Pass to autobuild source_environment
the pathname of your local clone of the build-variables/variables file, or set
AUTOBUILD_VARIABLES_FILE to that pathname before autobuild source_environment,
and remove the set_build_variables command. All the same variables will be set." 1>&2
        exit 1
    }

    # Usage:
    # switches="$(remove_switch -DPIC $LL_BUILD)"
    # It's important NOT to quote whichever compiler-arguments string you pass to
    # remove_switch (LL_BUILD in the example above), just as it's important not to
    # quote it when passing it to the compiler itself: bash must parse into
    # separate tokens.
    remove_switch() {
        local todel="$1"
        shift
        local out=()
        for sw
        do if [ "$sw" != "$todel" ]
           then # append $sw to out
                out[${#out[*]}]="$sw"
           fi
        done
        echo "${out[@]}"
    }

    # Usage:
    # switches="$(replace_switch -DPIC -DPOC $LL_BUILD)"
    # It's important NOT to quote whichever compiler-arguments string you pass to
    # replace_switch (LL_BUILD in the example above), just as it's important not to
    # quote it when passing it to the compiler itself: bash must parse into
    # separate tokens.
    replace_switch() {
        local todel="$1"
        local toins="$2"
        shift
        shift
        echo "$toins $(remove_switch "$todel" "$@")"
    }

    fix_dylib_id() {
        local dylib=$1
        local dylink="$dylib"
        if [ -f "$dylib" ]; then
            if [ -L "$dylib" ]; then
                dylib="$(readlink "$dylib")"
            fi
            install_name_tool -id "@executable_path/../Resources/$dylib" "$dylib"
            if [ "$dylib" != "$dylink" ]; then
                ln -svf "$dylib" "$dylink"
            fi
        fi
    }
"""

windows_template = """

    build_sln() {
        local solution=$1
        local config=$2
        local proj="${3:-}"

        if (($USE_INCREDIBUILD)) ; then
            BuildConsole "$solution" ${proj:+/PRJ="$proj"} /CFG="$config"
        else
            devenv.com "$(cygpath -w "$solution")" /build "$config" ${proj:+/project "$proj"}
        fi
    }

    # function for loading visual studio related env vars
    load_vsvars() {
%(vsvars)s
    }
    
    if ! (($USE_INCREDIBUILD)) ; then
        load_vsvars
    fi
"""

def do_source_environment(args):
    # SL-452: autobuild source_environment now takes a positional argument
    # 'varsfile' indicating the script in which we'll find essential
    # environment variable settings. This argument isn't required to ease
    # transitioning from autobuild 1.0 and earlier -- instead, if omitted,
    # check AUTOBUILD_VARIABLES_FILE. (Easier for a developer to set that one
    # environment variable than to fix every autobuild source_environment
    # command in every build script s/he must run.)
    if args.varsfile is None:
        try:
            args.varsfile = os.environ["AUTOBUILD_VARIABLES_FILE"]
        except KeyError:
            logger.warning("""\
No source_environment argument and no AUTOBUILD_VARIABLES_FILE variable set:
no build variables (e.g.
https://bitbucket.org/lindenlab/build-variables/src/tip/variables)
will be emitted. This could cause your build to fail for lack of LL_BUILD or
similar.""")

    exports, vars, vsvars = \
        internal_source_environment(args.configurations, args.varsfile)

    var_mapping = {}

    if not common.is_system_windows():
        template = environment_template
    else:
        template = '\n'.join((environment_template, windows_template))

        # We don't know which environment variables might be modified by
        # vsvars32.bat, but one of them is likely to be PATH. Treat PATH
        # specially: when a bash script invokes our load_vsvars() shell
        # function, we want to prepend to its existing PATH rather than
        # replacing it with whatever's visible to Python right now.
        try:
            PATH = vsvars.pop("PATH")
        except KeyError:
            pass
        else:
            # Translate paths from windows to cygwin format.
            # Match patterns of the form %SomeVar%. Match the SHORTEST such
            # string so that %var1% ... %var2% are two distinct matches.
            percents = re.compile(r"%(.*?)%")
            PATH = ":".join(
                # Some pathnames in the PATH var may be individually quoted --
                # strip quotes from those.
                # Moreover, some may have %SomeVar% substitutions; replace
                # with ${SomeVar} substitutions for bash. (Use curly braces
                # because we don't want to have to care what follows.)
                # may as well de-dup while we're at it
                dedup(cygpath("-u", percents.sub(r"${\1}", p.strip('"')))
                      for p in PATH.split(';'))
            )
            vsvars["PATH"] = PATH + ":$PATH"

        # Now make a list of the items from vsvars.
        # A pathname ending with a backslash (as many do on Windows), when
        # embedded in quotes in a bash script, might inadvertently escape the
        # close quote. Remove all trailing backslashes.
        vsvarslist = [(k, v.rstrip('\\')) for (k, v) in vsvars.items()]

        # may as well sort by keys
        vsvarslist.sort()

        # Since at coding time we don't know the set of all modified
        # environment variables, don't try to name them individually in the
        # template. Instead, bundle all relevant export statements into a
        # single substitution.
        var_mapping["vsvars"] = '\n'.join(
            ('        export %s="%s"' % varval for varval in vsvarslist)
        )

    # Before expanding template with var_mapping, finalize the 'exports' and
    # 'vars' dicts into var_mapping["vars"] as promised above.
    var_mapping["vars"] = '\n'.join(itertools.chain(
        (("    export %s='%s'" % (k, v)) for k, v in exports.items()),
        (("    %s='%s'" % (k, v)) for k, v in vars.items()),
        ))

    # Write to stdout buffer to avoid writing CRLF line endings
    sys.stdout.buffer.write((template % var_mapping).encode("utf-8"))

    if get_params:
        # *TODO - run get_params.generate_bash_script()
        pass


def internal_source_environment(configurations, varsfile):
    """
    configurations is a list of requested configurations (e.g. 'Release'). If
    the list isn't empty, the first entry will be used; any additional entries
    will be ignored with a warning.

    varsfile, if not None, is the name of a local variables file as in
    https://bitbucket.org/lindenlab/build-variables/src/tip/variables.

    os.environ['AUTOBUILD_VSVER'] indirectly indicates a Visual Studio
    vcvarsall.bat script from which to load variables. Its values are e.g.
    '100' for Visual Studio 2010 (VS 10), '120' for Visual Studio 2013 (VS 12)
    and so on. A correct value nnn for the running system will identify a
    corresponding VSnnnCOMNTOOLS environment variable.

    Returns a triple of dicts (exports, vars, vsvars):

    exports is intended to propagate down to child processes, hence should be
    exported by the consuming bash shell.

    vars is intended for use by the consuming bash shell, hence need not be
    exported.

    vsvars contains variables set by the relevant Visual Studio vcvarsall.bat
    script. It is an empty dict on any platform but Windows.
    """
    if not common.is_system_windows():
        vsver = None                    # N/A
    else:
        try:
            vsver = os.environ['AUTOBUILD_VSVER']
        except KeyError:
            # try to figure out most recent Visual Studio version
            try:
                vsver = _available_vsvers()[-1]
            except IndexError:
                logger.warning("No Visual Studio install detected -- "
                               "certain configuration variables will not be available")
                vsver = None

    # OPEN-259: it turns out to be important that if AUTOBUILD is already set
    # in the environment, we should LEAVE IT ALONE. So if it exists, use the
    # existing value. Otherwise just use our own executable path.
    autobuild_path = common.get_autobuild_executable_path()
    AUTOBUILD = os.environ.get("AUTOBUILD", autobuild_path)
    # The cross-platform environment_template contains a generic 'vars' slot
    # where we can insert lines defining environment variables. Putting a
    # variable definition into this 'exports' dict causes it to be listed
    # there with an 'export' statement; putting a variable definition into the
    # 'vars' dict lists it there as local to that bash process. Logic just
    # before expanding environment_template populates 'exports' and 'vars'
    # into var_mapping["vars"]. We defer it that long so that conditional
    # logic below can, if desired, add to either 'exports' or 'vars' first.
    exports = dict(
        AUTOBUILD=AUTOBUILD,
        AUTOBUILD_VERSION_STRING=common.AUTOBUILD_VERSION_STRING,
        AUTOBUILD_PLATFORM=common.get_current_platform(),
        )
    vars = dict(
##      MAKEFLAGS="",
##      DISTCC_HOSTS="",
        )
    vsvars = {}

    # varsfile could have been set either of two ways above, check again
    if varsfile is not None:
        # Read variable definitions from varsfile. Syntax restrictions are
        # documented in the build-variables/variables file itself, but
        # essentially it's the common subset of bash and string.Template
        # expansion functionality.
        # This is what we expect every substantive line in the input file to
        # look like: a valid variable name (starting with letter or
        # underscore, containing only letters, underscores or digits) = a
        # double-quoted value. We do not presently tolerate extra whitespace.
        assign_line = re.compile(r'^([A-Za-z_][A-Za-z0-9_]+)="(.*)"$')
        vfvars = {}
        try:
            with open(varsfile) as vf:
                for linen0, line in enumerate(vf):
                    # skip empty lines and comment lines
                    if line == '\n' or line.startswith('#'):
                        continue
                    match = assign_line.match(line.rstrip())
                    if not match:
                        # Fatal error is the only sure way to get a developer
                        # to fix a bad assignment in the variables file. If we
                        # just skip it with a warning, it could be weeks
                        # before we figure out why some large subset of
                        # third-party packages was built without essential
                        # compiler switches.
                        raise SourceEnvError(
                            "%s(%s): malformed variable assignment:\n%s" %
                            (varsfile, linen0+1, line.rstrip()))
                    var, value = match.group(1,2)
                    try:
                        # Rely on the similarity between string.Template
                        # subtitution syntax and bash substitution syntax.
                        vfvars[var] = string.Template(value).substitute(vfvars)
                    except ValueError as err:
                        raise SourceEnvError(
                            "%s(%s): bad substitution syntax: %s\n%s" %
                            (varsfile, linen0+1, err, line.rstrip()))
                    except KeyError as err:
                        raise SourceEnvError(
                            "%s(%s): undefined variable %s:\n%s" %
                            (varsfile, linen0+1, err, line.rstrip()))
        except (IOError, OSError) as err:
            # Even though it's only a warning to fail to specify varsfile,
            # it's a fatal error to specify one that doesn't exist or can't be
            # read.
            raise SourceEnvError(
                "%s: can't read '%s': %s" %
                (err.__class__.__name__, varsfile, err))

        # Here vfvars contains all the variables set in varsfile. Before
        # passing them along to the 'vars' dict, make a convenience pass over
        # them to extract simpler variable names specific to the platform and
        # build type.

        # If we recognize the current platform, provide shorthand vars for it.
        try:
            # Base this on sys.platform rather than
            # common.get_current_platform() because we don't want to have to
            # enumerate common.PLATFORM_WINDOWS, common.PLATFORM_WINDOWS64,
            # etc. just to blur the distinction between them again.
            platform = dict(
                win32 ="WINDOWS",
                cygwin="WINDOWS",
                darwin="DARWIN",
                linux="LINUX",
                linux2="LINUX",
                )[sys.platform]
        except KeyError:
            logger.warning("Unsupported platform %s: no short names provided" %
                           sys.platform)
        else:
            platform_re = re.compile(r'(.*_BUILD)_%s(.*)$' % platform)
            # use items() rather than iteritems(): we're modifying as we iterate
            for var, value in list(vfvars.items()):
                match = platform_re.match(var)
                if match:
                    # add a shorthand variable that excludes _PLATFORM
                    vfvars[''.join(match.group(1,2))] = value

        # If caller specified configuration, provide shorthand vars for it.
        # If nothing was specified, configurations will be empty; if something
        # was, take only the first specified configuration.
        if configurations:
            configuration = configurations[0].upper()
            if configurations[1:]:
                logger.warning("Ignoring extra configurations %s" %
                               ", ".join(configurations[1:]))
            configuration_re = re.compile(r'(.*_BUILD)_%s(.*)$' % configuration)
            # use items() because we're modifying as we iterate
            for var, value in list(vfvars.items()):
                match = configuration_re.match(var)
                if match:
                    # add a shorthand variable that excludes _CONFIGURATION
                    vfvars[''.join(match.group(1,2))] = value

        # We've been keeping varsfile variables separate so we can make the
        # above convenience passes through them without accidentally matching
        # pre-existing entries in 'vars'. Now dump everything into 'vars'.
        vars.update(vfvars)

    # Let KeyError, if any, propagate: lack of AUTOBUILD_ADDRSIZE would be
    # an autobuild coding error. So would any value for that variable
    # other than what's stated below.
    exports["AUTOBUILD_CONFIGURE_ARCH"] = {
        '32': 'i386',
        '64': 'x86_64',
        }[os.environ["AUTOBUILD_ADDRSIZE"]]

    if common.is_system_windows():
        try:
            use_ib = int(os.environ['USE_INCREDIBUILD'])
        except ValueError:
            logger.warning("USE_INCREDIBUILD environment variable contained garbage %r "
                           "(expected 0 or 1)" % os.environ['USE_INCREDIBUILD'])
            use_ib = 0
        except KeyError:
            # We no longer require Incredibuild for Windows builds. Therefore,
            # if you want to engage Incredibuild, you must explicitly set
            # USE_INCREDIBUILD=1. We no longer implicitly set that if
            # BuildConsole.exe is on the PATH.
            use_ib = 0

        vars["USE_INCREDIBUILD"] = str(use_ib)

        # Let KeyError, if any, propagate: lack of AUTOBUILD_ADDRSIZE would be
        # an autobuild coding error. So would any value for that variable
        # other than what's stated below.
        exports["AUTOBUILD_WIN_VSPLATFORM"] = {
            '32': 'Win32',
            '64': 'x64',
            }[os.environ["AUTOBUILD_ADDRSIZE"]]

        if vsver:
            # When one of our build-cmd.sh scripts invokes CMake on Windows, it's
            # probably prudent to use a -G switch for the specific Visual Studio
            # version we want to target. It's not that uncommon for a Windows
            # build host to have multiple VS versions installed, and it can
            # sometimes take a while for us to switch to the newest release. Yet
            # we do NOT want to hard-code the version-specific CMake generator
            # name into each 3p source repo: we know from experience that
            # sprinkling version specificity throughout a large collection of 3p
            # repos is part of what makes it so hard to upgrade the compiler. The
            # problem is that the mapping from vsver to (e.g.) "Visual Studio 12"
            # isn't necessarily straightforward -- we may have to maintain a
            # lookup dict. That dict should not be replicated into each 3p repo,
            # it should be central. It should be here.
            try:
                AUTOBUILD_WIN_CMAKE_GEN = {
                    '120': "Visual Studio 12",
                    '140': "Visual Studio 14",
                    '150': "Visual Studio 15",
                    '160': "Visual Studio 16",
                    }[vsver]
            except KeyError:
                # We don't have a specific mapping for this value of vsver. Take
                # a wild guess. If we guess wrong, CMake will complain, and the
                # user will have to update autobuild -- which is no worse than
                # what s/he'd have to do anyway if we immediately produced an
                # error here. Plus this way, we defer the error until we hit a
                # build that actually consumes AUTOBUILD_WIN_CMAKE_GEN.
                AUTOBUILD_WIN_CMAKE_GEN = "Visual Studio %s" % (vsver[:-1])
            # Of course CMake also needs to know bit width :-P
            # Or at least it used to, until VS 2019.
            if os.environ["AUTOBUILD_ADDRSIZE"] == "64" and vsver < '160':
                AUTOBUILD_WIN_CMAKE_GEN += " Win64"
            exports["AUTOBUILD_WIN_CMAKE_GEN"] = AUTOBUILD_WIN_CMAKE_GEN

            # load vsvars32.bat variables
            vsvars = load_vsvars(vsver)

            # Resetting our PROMPT is a bit heavy-handed. Plus the substitution
            # syntax probably differs.
            vsvars.pop("PROMPT", None)

    return exports, vars, vsvars


def get_enriched_environment(configuration):
    """
    Return a dict containing an 'enriched' environment in which to run
    external commands under autobuild.

    configuration is the requested configuration (e.g. 'Release'), or None.
    This is used to provide abbreviations for certain variables set in
    AUTOBUILD_VARIABLES_FILE.

    os.environ['AUTOBUILD_VARIABLES_FILE'], if set, is the name of a local
    variables file as in
    https://bitbucket.org/lindenlab/build-variables/src/tip/variables.

    On Windows, os.environ['AUTOBUILD_VSVER'] indirectly indicates a Visual
    Studio vcvarsall.bat script from which to load variables. Its values are
    e.g. '100' for Visual Studio 2010 (VS 10), '120' for Visual Studio 2013
    (VS 12) and so on. A correct value nnn for the running system will
    identify a corresponding VSnnnCOMNTOOLS environment variable.

    On Windows, if AUTOBUILD_VSVER isn't set, a value will be inferred from
    the available VSnnnCOMNTOOLS environment variables.
    """
    result = common.get_autobuild_environment()
    exports, vars, vsvars = internal_source_environment(
        [configuration] if configuration else [],
        os.environ.get("AUTOBUILD_VARIABLES_FILE"))
    result.update(exports)
    result.update(vars)
    result.update(vsvars)
    return result


def dedup(iterable):
    return iter(OrderedDict((item, 1) for item in iterable))


class AutobuildTool(autobuild_base.AutobuildBase):
    def get_details(self):
        return dict(name=self.name_from_file(__file__),
                    description='Prints out the shell environment Autobuild-based buildscripts to use (by calling \'eval\').')

    # called by autobuild to add help and options to the autobuild parser, and
    # by standalone code to set up argparse
    def register(self, parser):
        parser.description='prints out the shell environment for Autobuild-based ' \
                           'buildscripts to use ' \
                           '(by calling \'eval\' i.e. eval "$(autobuild source_environment)").'
        parser.add_argument('-V', '--version', action='version',
                            version='source_environment tool module %s' %
                            common.AUTOBUILD_VERSION_STRING)
        # we use action="append" not because we want to support multiple -c
        # arguments, but to unify the processing between supplied -c and
        # configurations_from_environment(), which produces a list.
        parser.add_argument('--configuration', '-c', nargs='?',
                            action="append", dest='configurations',
                            help="emit shorthand variables for a specific build configuration\n"
                            "(may be specified in $AUTOBUILD_CONFIGURATION; "
                            "multiple values make no sense here)",
                            metavar='CONFIGURATION',
                            default=self.configurations_from_environment())
        parser.add_argument("varsfile", nargs="?", default=None,
                            help="Local sh script in which to find essential environment "
                            "variable settings (default from $AUTOBUILD_VARIABLES_FILE), "
                            "e.g. a checkout of "
                            "https://bitbucket.org/lindenlab/viewer-build-variables/"
                            "src/tip/variables")

    def run(self, args):
        do_source_environment(args)