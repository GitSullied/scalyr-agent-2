#!/usr/bin/env python
#
# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# Script used to build the RPM, Debian, and tarball packages for releasing Scalyr Agent 2.
#
# To execute this script, you must have installed fpm: https://github.com/jordansissel/fpm
#
# Usage: python build_package.py [options] rpm|tarball|deb
#
# author: Steven Czerwinski <czerwin@scalyr.com>

__author__ = 'czerwin@scalyr.com'

import errno
import glob
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from cStringIO import StringIO
from optparse import OptionParser
from time import gmtime, strftime

from scalyr_agent.__scalyr__ import determine_file_path, SCALYR_VERSION, scalyr_init

scalyr_init()

# The root of the Scalyr repository should just be the parent of this file.
__source_root__ = os.path.dirname(determine_file_path())


def build_package(package_type, variant):
    """Builds the scalyr-agent-2 package specified by the arguments.

    The package is left in the current working directory.  The file name of the
    package is returned by this function.

    @param package_type: One of 'rpm', 'deb', or 'tarball'. Determines which package type is built.
    @param variant: Adds the specified string into the package's iteration name. This may be None if no additional
        tweak to the name is required. This is used to produce different packages even for the same package type (such
        as 'rpm').

    @return: The file name of the produced package.
    """
    original_cwd = os.getcwd()

    version = SCALYR_VERSION

    # Create a temporary directory to build the package in.
    tmp_dir = tempfile.mkdtemp(prefix='build-scalyr-agent-packages')
    try:
        # Change to that directory and delegate to another method for the specific type.
        os.chdir(tmp_dir)
        if package_type == 'tarball':
            artifact_file_name = build_tarball_package(variant, version)
        else:
            assert package_type in ('deb', 'rpm')
            artifact_file_name = build_rpm_or_deb_package(package_type == 'rpm', variant, version)

        os.chdir(original_cwd)

        # Move the artifact (built package) to the original current working dir.
        shutil.move(os.path.join(tmp_dir, artifact_file_name), artifact_file_name)
        return artifact_file_name
    finally:
        # Be sure to delete the temporary directory.
        os.chdir(original_cwd)
        shutil.rmtree(tmp_dir)


def build_rpm_or_deb_package(is_rpm, variant, version):
    """Builds either an RPM or Debian package in the current working directory.

    @param is_rpm: True if an RPM should be built. Otherwise a Debian package will be built.
    @param variant: If not None, will add the specified string into the iteration identifier for the package. This
        allows for different packages to be built for the same type and same version.
    @param version: The agent version.

    @return: The file name of the built package.
    """
    original_dir = os.getcwd()

    # Create the directory structure for where the RPM/Debian package will place files on the sytem.
    make_directory('root/etc/init.d')
    make_directory('root/var/log/scalyr-agent-2')
    make_directory('root/var/lib/scalyr-agent-2')
    make_directory('root/etc/scalyr-agent-2')
    make_directory('root/etc/scalyr-agent-2/agent.d')
    make_directory('root/usr/share')
    make_directory('root/usr/sbin')

    # Place all of the import source in /usr/share/scalyr-agent-2.
    os.chdir('root/usr/share')
    build_base_files()

    os.chdir('scalyr-agent-2')
    # The build_base_files leaves the config directory in config/agent.json, but we have to move it to its etc
    # location.
    shutil.move(convert_path('config/agent.json'), make_path(original_dir, 'root/etc/scalyr-agent-2/agent.json'))
    shutil.rmtree('config')

    os.chdir(original_dir)

    # Create the links to the appropriate commands in /usr/sbin and /etc/init.d/
    make_soft_link('/usr/share/scalyr-agent-2/bin/scalyr-agent-2', 'root/etc/init.d/scalyr-agent-2')
    make_soft_link('/usr/share/scalyr-agent-2/bin/scalyr-agent-2', 'root/usr/sbin/scalyr-agent-2')
    make_soft_link('/usr/share/scalyr-agent-2/bin/scalyr-agent-2-config', 'root/usr/sbin/scalyr-agent-2-config')

    # Create the scriplets the RPM/Debian package invokes when uninstalling or upgrading.
    create_scriptlets()
    # Produce the change logs that we will embed in the package, based on the CHANGELOG.md in this directory.
    create_change_logs()

    if is_rpm:
        package_type = 'rpm'
    else:
        package_type = 'deb'

    # Only change the iteration label if we need to embed a variant.
    if variant is not None:
        iteration_arg = '--iteration 1.%s' % variant
    else:
        iteration_arg = ''

    description = ('Scalyr Agent 2 is the daemon process Scalyr customers run on their servers to collect metrics and '
                   'log files and transmit them to Scalyr.')

    run_command('fpm -s dir -a all -t %s -n "scalyr-agent-2" -v %s '
                '  --license "Apache 2.0" '
                '  --vendor Scalyr %s '
                '  --maintainer czerwin@scalyr.com '
                '  --provides scalyr-agent-2 '
                '  --description "%s" '
                '  --depends "python >= 2.4" '
                '  --depends "bash >= 3.2" '
                '  --url https://www.scalyr.com '
                '  --deb-user root '
                '  --deb-group root '
                '  --deb-changelog changelog-deb '
                '  --rpm-user root '
                '  --rpm-group root '
                '  --rpm-changelog changelog-rpm'
                '  --after-install postinstall.sh '
                '  --before-remove preuninstall.sh '
                '  --config-files /etc/scalyr-agent-2/agent.json '
                '  --directories /usr/share/scalyr-agent-2 '
                '  --directories /var/lib/scalyr-agent-2 '
                '  --directories /var/log/scalyr-agent-2 '
                '  -C root usr etc var' % (package_type, version, iteration_arg, description),
                exit_on_fail=True, command_name='fpm')

    # We determine the artifact name in a little bit of loose fashion.. we just glob over the current
    # directory looking for something either ending in .rpm or .deb.  There should only be one package,
    # so that is fine.
    if is_rpm:
        files = glob.glob('*.rpm')
    else:
        files = glob.glob('*.deb')

    if len(files) != 1:
        raise Exception('Could not find resulting rpm or debian package in the build directory.')

    return files[0]


def build_tarball_package(variant, version):
    """Builds the scalyr-agent-2 tarball in the current working directory.

    @param variant: If not None, will add the specified string into the final tarball name. This allows for different
        tarballs to be built for the same type and same version.
    @param version: The agent version.

    @return: The file name of the built tarball.
    """
    # Use build_base_files to build all of the important stuff in ./scalyr-agent-2
    build_base_files()

    # Build the rest of the directories required for the tarball install.  Mainly, the log and data directories
    # in the tarball itself where the running process will store its state.
    make_directory('scalyr-agent-2/data')
    make_directory('scalyr-agent-2/log')
    make_directory('scalyr-agent-2/config/agent.d')

    # Create a file named packageless.  This signals to the agent that
    # this a tarball install instead of an RPM/Debian install, which changes
    # the default paths for th econfig, logs, data, etc directories.  See
    # configuration.py.
    write_to_file('1', 'scalyr-agent-2/packageless')

    if variant is None:
        base_archive_name = 'scalyr-agent-%s' % version
    else:
        base_archive_name = 'scalyr-agent-%s.%s' % (version, variant)

    shutil.move('scalyr-agent-2', base_archive_name)

    # Tar it up.
    tar = tarfile.open('%s.tar.gz' % base_archive_name, 'w:gz')
    tar.add(base_archive_name)
    tar.close()

    return '%s.tar.gz' % base_archive_name


def build_base_files():
    """Build the basic structure for a package in a new directory scalyr-agent-2 in the current working directory.

    This creates scalyr-agent-2 in the current working directory and then populates it with the basic structure
    required by most of the packages.

    It copies the source files, the certs, the configuration directories, etc.  This will make sure to exclude
    files like .pyc, .pyo, etc.

    In the end, the structure will look like:
      scalyr-agent-2:
        py/scalyr_agent/           -- All the scalyr_agent source files
        certs/ca_certs.pem         -- The trusted SSL CA root list.
        config/agent.json          -- The configuration file.
        bin/scalyr-agent-2         -- Symlink to the agent_main.py file to run the agent.
        bin/scalyr-agent-2-config  -- Symlink to config_main.py to run the configuration tool
        build_info                 -- A file containing the commit id of the latest commit included in this package,
                                      the time it was built, and other information.
    """
    original_dir = os.getcwd()
    # This will return the parent directory of this file.  We will use that to determine the path
    # to files like scalyr_agent/ to copy the source files
    agent_source_root = __source_root__

    make_directory('scalyr-agent-2/py')
    os.chdir('scalyr-agent-2')

    make_directory('certs')
    make_directory('bin')

    # Copy the version file.  We copy it both to the root and the package root.  The package copy is done down below.
    shutil.copy(make_path(agent_source_root, 'VERSION'), 'VERSION')

    # Copy the source files.
    os.chdir('py')

    shutil.copytree(make_path(agent_source_root, 'scalyr_agent'), 'scalyr_agent')
    shutil.copytree(make_path(agent_source_root, 'monitors'), 'monitors')
    os.chdir('monitors')
    recursively_delete_files_by_name('README.md')
    os.chdir('..')
    shutil.copy(make_path(agent_source_root, 'VERSION'), os.path.join('scalyr_agent', 'VERSION'))

    # Exclude certain files.
    # TODO:  Should probably use MANIFEST.in to do this, but don't know the Python-fu to do this yet.
    #
    # Don't include the tests directories.  Also, don't include the .idea directory created by IDE.
    recursively_delete_dirs_by_name('\.idea', 'tests')
    recursively_delete_files_by_name('.*\.pyc', '.*\.pyo', '.*\.pyd', 'all_tests\.py', '.*~')

    os.chdir('..')

    # Copy the config
    shutil.copytree(make_path(agent_source_root, 'config'), 'config')

    # Create the trusted CA root list.
    os.chdir('certs')
    cat_files(glob_files(make_path(agent_source_root, 'certs/*.pem')), 'ca_certs.crt')

    os.chdir('..')

    # Create symlinks for the two commands
    os.chdir('bin')

    make_soft_link('../py/scalyr_agent/agent_main.py', 'scalyr-agent-2')
    make_soft_link('../py/scalyr_agent/config_main.py', 'scalyr-agent-2-config')

    os.chdir('..')

    write_to_file(get_build_info(), 'build_info')

    os.chdir(original_dir)


def make_directory(path):
    """Creates the specified directory including any parents that do not yet exist.

    @param path: The path of the directory to create. This string can use a forward slash to separate path
           components regardless of the separator character for this platform.  This method will perform the necessary
           conversion.
    """
    converted_path = convert_path(path)
    try:
        os.makedirs(converted_path)
    except OSError, error:
        if error.errno == errno.EEXIST and os.path.isdir(converted_path):
            pass
        else:
            raise


def make_path(parent_directory, path):
    """Returns the full path created by joining path to parent_directory.

    This method is a convenience function because it allows path to use forward slashes
    to separate path components rather than the platform's separator character.

    @param parent_directory: The parent directory. This argument must use the system's separator character. This may be
        None if path is relative to the current working directory.
    @param path: The path to add to parent_directory. This should use forward slashes as the separator character,
        regardless of the platform's character.

    @return:  The path created by joining the two with using the system's separator character.
    """
    if parent_directory is None and os.path.sep == '/':
        return path

    if parent_directory is None:
        result = ''
    elif path.startswith('/'):
        result = ''
    else:
        result = parent_directory

    for path_part in path.split('/'):
        if len(path_part) > 0:
            result = os.path.join(result, path_part)

    return result


def convert_path(path):
    """Converts the forward slashes in path to the platform's separator and returns the value.

    @param path: The path to convert. This should use forward slashes as the separator character, regardless of the
        platform's character.

    @return: The path created by converting the forward slashes to the platform's separator.
    """
    return make_path(None, path)


def make_soft_link(source, link_path):
    """Creates a soft link at link_path to source.

    @param source: The path that the link will point to. This should use a forward slash as the separator, regardless
        of the platform's separator.
    @param link_path: The path where the link will be created. This should use a forward slash as the separator,
        regardless of the platform's separator.
    """
    os.symlink(convert_path(source), convert_path(link_path))


def glob_files(path):
    """Returns the paths that match the specified path glob (based on current working directory).

    @param path: The path with glob wildcard characters to match. This should use a forward slash as the separator,
        regardless of the platform's separator.

    @return: The list of matched paths.
    """
    return glob.glob(convert_path(path))


def recursively_delete_dirs_by_name(*dir_names):
    """Deletes any directories that are in the current working directory or any of its children whose file names
    match the specified regular expressions.

    This will recursively examine all children of the current working directory.

    If a directory is found that needs to be deleted, all of it and its children are deleted.

    @param dir_names: A variable number of strings containing regular expressions that should match the file names of
        the directories that should be deleted.
    """
    # Compile the strings into actual regular expression match objects.
    matchers = []
    for dir_name in dir_names:
        matchers.append(re.compile(dir_name))

    # Walk down the file tree, top down, allowing us to prune directories as we go.
    for root, dirs, files in os.walk('.'):
        # The list of directories at the current level to delete.
        to_remove = []

        # Examine all directories at this level, see if any get a match
        for dir_path in dirs:
            remove_it = False
            for matcher in matchers:
                if matcher.match(dir_path):
                    remove_it = True
            if remove_it:
                to_remove.append(dir_path)

        # Go back and delete it.  Also, remove it from dirs so that we don't try to walk down it.
        for remove_dir_path in to_remove:
            shutil.rmtree(os.path.join(root, remove_dir_path))
            dirs.remove(remove_dir_path)


def recursively_delete_files_by_name(*file_names):
    """Deletes any files that are in the current working directory or any of its children whose file names
    match the specified regular expressions.

    This will recursively examine all children of the current working directory.

    @param file_names: A variable number of strings containing regular expressions that should match the file names of
        the files that should be deleted.
    """
    # Compile the strings into actual regular expression match objects.
    matchers = []
    for file_name in file_names:
        matchers.append(re.compile(file_name))

    # Walk down the current directory.
    for root, dirs, files in os.walk('.'):
        # See if any of the files at this level match any of the matchers.
        for file_path in files:
            remove_it = False
            for matcher in matchers:
                if matcher.match(file_path):
                    remove_it = True
            # Delete it if it did match.
            if remove_it:
                os.unlink(os.path.join(root, file_path))


def cat_files(file_paths, destination):
    """Concatenates the contents of the specified files and writes it to a new file at destination.

    @param file_paths: A list of paths for the files that should be read. The concatenating will be done in the same
        order as the list.
    @param destination: The path of the file to write the contents to.
    """
    dest_fp = open(destination, 'w')
    for file_path in file_paths:
        in_fp = open(file_path, 'r')
        for line in in_fp:
            dest_fp.write(line)
        in_fp.close()
    dest_fp.close()


def write_to_file(string_value, file_path):
    """Writes the specified string to a new file.

    This removes trailing newlines, etc, to avoid adding an extra blank line.

    @param string_value: The value to write to the file.
    @param file_path: The path of the file to write to.
    """
    dest_fp = open(file_path, 'w')
    dest_fp.write(string_value.rstrip())
    dest_fp.write(os.linesep)
    dest_fp.close()


def parse_date(date_str):
    """Parses a date time string of the format MMM DD, YYYY HH:MM +ZZZZ and returns seconds past epoch.

    Example of the format is: Oct 10, 2014 17:00 -0700

    @param date_str: A string containing the date and time in the format described above.

    @return: The number of seconds past epoch.

    @raise ValueError: if there is a parsing problem.
    """
    # For some reason, it was hard to parse this particular format with the existing Python libraries,
    # especially when the timezone was not the same as the local time zone.  So, we have to do this the
    # sort of hard way.
    #
    # It is a little annoying that strptime only takes Sep for September and not Sep which is more common
    # in US-eng, so we cheat here and just swap it out.
    adjusted = date_str.replace('Sept', 'Sep')

    # Find the timezone string at the end of the string.
    if re.search('[\-+]\d\d\d\d$', adjusted) is None:
        raise ValueError('Value \'%s\' does not meet required time format of \'MMM DD, YYYY HH:MM +ZZZZ\' (or '
                         'as an example, \' \'Oct 10, 2014 17:00 -0700\'' % date_str)

    # Use the existing Python string parsing calls to just parse the time and date.  We will handle the timezone
    # separately.
    try:
        base_time = time.mktime(time.strptime(adjusted[0:-6], '%b %d, %Y %H:%M'))
    except ValueError:
        raise ValueError('Value \'%s\' does not meet required time format of \'MMM DD, YYYY HH:MM +ZZZZ\' (or '
                         'as an example, \' \'Oct 10, 2014 17:00 -0700\'' % date_str)

    # Since mktime assumes the time is in localtime, we might have a different time zone
    # in tz_str, we must manually added in the difference.
    # First, convert -0700 to seconds.. the second two digits are the number of hours
    # and the last two are the minute of minutes.
    tz_str = adjusted[-5:]
    tz_offset_secs = int(tz_str[1:3]) * 3600 + int(tz_str[3:5]) * 60

    if tz_str.startswith('-'):
        tz_offset_secs *= -1

    # Determine the offset for the local timezone.
    if time.daylight:
        local_offset_secs = -1 * time.altzone
    else:
        local_offset_secs = -1 * time.timezone

    base_time += local_offset_secs - tz_offset_secs
    return base_time


# TODO:  This code is shared with config_main.py.  We should move this into a common
# utility location both commands can import it from.
def run_command(command_str, exit_on_fail=True, command_name=None):
    """Executes the specified command string returning the exit status.

    @param command_str: The command to execute.
    @param exit_on_fail: If True, will exit this process with a non-zero status if the command fails.
    @param command_name: The name to use to identify the command in error output.

    @return: The exist status of the command.
    """
    # We have to use a temporary file to hold the output to stdout and stderr.
    output_file = tempfile.mktemp()
    output_fp = open(output_file, 'w')

    try:
        return_code = subprocess.call(command_str, stdin=None, stderr=output_fp, stdout=output_fp, shell=True)
        output_fp.flush()

        # Read the output back into a string.  We cannot use a cStringIO.StringIO buffer directly above with
        # subprocess.call because that method expects fileno support which StringIO doesn't support.
        output_buffer = StringIO()
        input_fp = open(output_file, 'r')
        for line in input_fp:
            output_buffer.write(line)
        input_fp.close()

        output_str = output_buffer.getvalue()
        output_buffer.close()

        if return_code != 0:
            if command_name is not None:
                print >>sys.stderr, 'Executing %s failed and returned a non-zero result of %d' % (command_name,
                                                                                                  return_code)
            else:
                print >>sys.stderr, ('Executing the following command failed and returned a non-zero result of %d' %
                                     return_code)
                print >>sys.stderr, '  Command: "%s"' % command_str

            print >>sys.stderr, 'The output was:'
            print >>sys.stderr, output_str

            if exit_on_fail:
                print >>sys.stderr, 'Exiting due to failure.'
                sys.exit(1)

        return return_code, output_str

    finally:
        # Be sure to close the temporary file and delete it.
        output_fp.close()
        os.unlink(output_file)


def create_scriptlets():
    """Creates two scriptlets required by the RPM and Debian package in the current working directory.

    These are the preuninstall.sh and postuninstall.sh scripts.
    """
    fp = open('preuninstall.sh', 'w')
    fp.write("""#!/bin/bash

# We only need to take action if this is an uinstall of the package
# (rather than just removing this version because we are upgrading to
# a new one).  An uninstall is indicated by $1 == 0 for
# RPM and $1 == "remove" for Debian.
if [ "$1" == "0" -o "$1" == "remove" ]; then
  # Stop the service since we are about to completely remove it.
  service scalyr-agent-2 stop > /dev/null 2>&1

  # Remove the symlinks from the /etc/rcX.d directories.
  if [ -f /sbin/chkconfig -o -f /usr/sbin/chkconfig ]; then
    # For RPM-based systems.
    chkconfig --del scalyr-agent-2;
  elif [ -f /usr/sbin/update-rc.d -o -f /sbin/update-rc.d ]; then
    # For Debian-based systems.
    update-rc.d -f scalyr-agent-2 remove;
  else
    # All others.
    for x in 0 1 6; do
      rm /etc/rc$x.d/K02scalyr-agent-2;
    done

    for x in 2 3 4 5; do
      rm /etc/rc$x.d/S98scalyr-agent-2;
    done
  fi

  # Remove the .pyc files
  find /usr/share/scalyr-agent-2 -name *.pyc | rm -f
fi

exit 0;
""")
    fp.close()

    # Create the postinstall.sh script.
    fp = open('postinstall.sh', 'w')
    fp.write("""#!/bin/bash

config_owner=`stat -c %U /etc/scalyr-agent-2/agent.json`
script_owner=`stat -c %U /usr/share/scalyr-agent-2/bin/scalyr-agent-2`

# Determine if the agent had been previously configured to run as a
# different user than root.  We can determine this if agentConfig.json
# has a different user.  If so, then make sure the newly installed files
# (like agent.sh) are changed to the correct owners.
if [ "$config_owner" != "$script_owner" ]; then
  /usr/share/scalyr-agent-2/bin/scalyr-agent-2-config --set_user $config_owner > /dev/null 2>&1;
fi

# Add in the symlinks in the appropriate /etc/rcX.d/ directories
# to stop and start the service at boot time.
if [ -f /sbin/chkconfig -o -f /usr/sbin/chkconfig ]; then
  # For Redhat-based systems, use chkconfig to create links.
  chkconfig --add scalyr-agent-2;
elif [ -f /usr/sbin/update-rc.d -o -f /sbin/update-rc.d ]; then
  # For Debian-based systems, update-rc.d does the job.
  update-rc.d scalyr-agent-2 defaults 98 02;
else
  # Otherwise just fall back to creating them manually.
  for x in 0 1 6; do
    ln -s /etc/init.d/scalyr-agent-2 /etc/rc$x.d/K02scalyr-agent-2;
  done

  for x in 2 3 4 5; do
    ln -s /etc/init.d/scalyr-agent-2 /etc/rc$x.d/S98scalyr-agent-2;
  done
fi

# Do a restart of the service if we are either installing/upgrading the
# package, instead of removing it.  For an RPM, a remove is indicated by
# a zero being passed into $1 (instead of 1 or higher).  For Debian, a
# remove is indicated something other than "configure" being passed into $1.
if [[ "$1" =~ ^[0-9]+$ && $1 -gt 0 ]] || [ "$1" == "configure" ]; then
  service scalyr-agent-2 condrestart --quiet;
  exit $?;
else
  exit 0;
fi
""")
    fp.close()


def create_change_logs():
    """Creates the necessary change logs for both RPM and Debian based on CHANGELOG.md.

    Creates two files in the current working directory named 'changelog-rpm' and 'changelog-deb'.  They
    will have the same content as CHANGELOG.md but formatted by the respective standards for the different
    packaging systems.
    """
    # We define a helper function named print_release_notes that is used down below.
    def print_release_notes(output_fp, notes, level_prefixes, level=0):
        """Emits the notes for a single release to output_fp.

        @param output_fp: The file to write the notes to
        @param notes: An array of strings containing the notes for the release. Some elements may be lists of strings
            themselves to represent sublists. Only three levels of nested lists are allowed. This is the same format
            returned by parse_change_log() method.
        @param level_prefixes: The prefix to use for each of the three levels of notes.
        @param level: The current level in the notes.
        """
        prefix = level_prefixes[level]
        for note in notes:
            if isinstance(note, list):
                # If a sublist, then recursively call this function, increasing the level.
                print_release_notes(output_fp, note, level_prefixes, level+1)
                if level == 0:
                    print >>output_fp
            else:
                # Otherwise emit the note with the prefix for this level.
                print >>output_fp, '%s%s' % (prefix, note)

    # Handle the RPM log first.  We parse CHANGELOG.md and then emit the notes in the expected format.
    fp = open('changelog-rpm', 'w')
    try:
        for release in parse_change_log():
            date_str = time.strftime('%a %b %d %Y', time.localtime(release['time']))

            # RPM expects the leading line for a relesae to start with an asterisk, then have
            # the name of the person doing the release, their e-mail and then the version.
            print >>fp, '* %s %s <%s> %s' % (date_str, release['packager'], release['packager_email'],
                                             release['version'])
            print >>fp
            print >>fp, 'Release: %s (%s)' % (release['version'], release['name'])
            print >>fp
            # Include the release notes, with the first level with no indent, an asterisk for the second level
            # and a dash for the third.
            print_release_notes(fp, release['notes'], ['', ' * ', '   - '])
            print >>fp
    finally:
        fp.close()

    # Next, create the Debian change log.
    fp = open('changelog-deb', 'w')
    try:
        for release in parse_change_log():
            # Debian expects a leading line that starts with the package, including the version, the distribution
            # urgency.  Then, anything goes until the last line for the release, which begins with two dashes.
            date_str = time.strftime('%a, %d %b %Y %H:%M:%S %z', time.localtime(release['time']))
            print >>fp, 'scalyr-agent-2 (%s) stable; urgency=low' % release['version']
            # Include release notes with an indented first level (using asterisk, then a dash for the next level,
            # finally a plus sign.
            print_release_notes(fp, release['notes'], [' * ', '   - ', '     + '])
            print >>fp, '-- %s <%s>  %s' % (release['packager'], release['packager_email'], date_str)
    finally:
        fp.close()


def parse_change_log():
    """Parses the contents of CHANGELOG.md and returns the content in a structured way.

    @return: A list of dicts, one for each release in CHANGELOG.md.  Each release dict will have with several fields:
            name:  The name of the release
            version:  The version of the release
            packager:  The name of the packager, such as 'Steven Czerwinski'
            packager_email:  The email for the packager
            time:  The seconds past epoch when the package was created
            notes:  A list of strings or lists representing the notes for the release.  The list may
                have elements that are strings (for a single line of notes) or lists (for a nested list under
                the last string element).  Only three levels of nesting are allowed.
    """
    # Some regular expressions matching what we expect to see in CHANGELOG.md.
    # Each release section should start with a '##' line for major header.
    release_matcher = re.compile('## ([\d\._]+) "(.*)"')
    # The expected pattern we will include in a HTML comment to give information on the packager.
    packaged_matcher = re.compile('Packaged by (.*) <(.*)> on (\w+ \d+, \d+ \d+:\d\d [+-]\d\d\d\d)')

    # Listed below are the deliminators we use to extract the structure from the changelog release
    # sections.  We fix our markdown syntax to make it easier for us.
    #
    # Our change log will look something like this:
    #
    # ## 2.0.1 "Aggravated Aardvark"
    #
    # New core features:
    # * Blah blah
    # * Blah Blah
    #   - sub point
    #
    # Bug fixes:
    # * Blah Blah

    # The deliminators, each level is marked by what pattern we should see in the next line to either
    # go up a level, go down a level, or confirm it is at the same level.
    section_delims = [
        # First level does not have any prefix.. just plain text.
        # So, the level up is the release header, which begins with '##'
        # The level down is ' *'.
        {'up': re.compile('## '), 'down': re.compile('\* '), 'same': re.compile('[^\s\*\-#]'), 'prefix': ''},
        # Second level always begins with an asterisk.
        {'up': re.compile('[^\s\*\-#]'), 'down': re.compile('    - '), 'same': re.compile('\* '), 'prefix': '* '},
        # Third level always begins with '  -'
        {'up': re.compile('\* '), 'down': None, 'same': re.compile('    - '), 'prefix': '    - '},
    ]

    # Helper function.
    def read_section(lines, level=0):
        """Transforms the lines representing the notes for a single release into the desired nested representation.

        @param lines: The lines for the notes for a release including markup. NOTE, this list must be in reverse order,
            where the next line to be scanned is the last line in the list.
        @param level: The nesting level that these lines are at.

        @return: A list containing the notes, with nested lists as appropriate.
        """
        result = []

        if len(lines) == 0:
            return result

        while len(lines) > 0:
            # Go over each line, seeing based on its content, if we should go up a nesting level, down a level,
            # or just stay at the same level.
            my_line = lines.pop()

            # If the next line is at our same level, then just add it to our current list and continue.
            if section_delims[level]['same'].match(my_line) is not None:
                result.append(my_line[len(section_delims[level]['prefix']):])
                continue

            # For all other cases, someone else is going to have to look at this line, so add it back to the list.
            lines.append(my_line)

            # If the next line looks like it belongs any previous nesting levels, then we must have exited out of
            # our current nesting level, so just return what we have gathered for this sublist.
            for i in range(level + 1):
                if section_delims[i]['up'].match(my_line) is not None:
                    return result
            if (section_delims[level]['down'] is not None and
                  section_delims[level]['down'].match(my_line) is not None):
                # Otherwise, it looks like the next line belongs to a sublist.  Recursively call ourselves, going
                # down a level in nesting.
                result.append(read_section(lines, level + 1))
            else:
                raise BadChangeLogFormat('Release not line did not match expect format at level %d: %s' % (
                                         level, my_line))
        return result

    # Begin the real work here.  Read the change log.
    change_log_fp = open(os.path.join(__source_root__, 'CHANGELOG.md'), 'r')

    try:
        # Skip over the first two lines since it should be header.
        change_log_fp.readline()
        change_log_fp.readline()

        # Read over all the lines, eliminating the comment lines and other useless things.  Also strip out all newlines.
        content = []
        in_comment = False
        for line in change_log_fp:
            line = line.rstrip()
            if len(line) == 0:
                continue

            # Check for a comment.. either beginning or closing.
            if line == '<!---':
                in_comment = True
            elif line == '--->':
                in_comment = False
            elif packaged_matcher.match(line) is not None:
                # The only thing we will pay attention to while in a comment is our packaged line.  If we see it,
                # grab it.
                content.append(line)
            elif not in_comment:
                # Keep any non-comments.
                content.append(line)

        change_log_fp.close()
        change_log_fp = None
    finally:
        if change_log_fp is not None:
            change_log_fp.close()

    # We reverse the content list so the first lines to be read are at the end.  This way we can use pop down below.
    content.reverse()

    # The list of release objects
    releases = []

    # The rest of the log should just contain release notes for each release.  Iterate over the content,
    # reading out the release notes for each release.
    while len(content) > 0:
        # Each release must begin with at least two lines -- one for the release name and then one for the
        # 'Packaged by Steven Czerwinski on... ' line that we pulled out of the HTML comment.
        if len(content) < 2:
            raise BadChangeLogFormat('New release section does not contain at least two lines.')

        # Extract the information from each of those two lines.
        current_line = content.pop()
        release_version_name = release_matcher.match(current_line)
        if release_version_name is None:
            raise BadChangeLogFormat('Header line for release did not match expected format: %s' % current_line)

        current_line = content.pop()
        packager_info = packaged_matcher.match(current_line)
        if packager_info is None:
            raise BadChangeLogFormat('Packager line for release did not match expected format: %s' % current_line)

        # Read the section notes until we hit a '##' line.
        release_notes = read_section(content)

        try:
            time_value = parse_date(packager_info.group(3))
        except ValueError, err:
            raise BadChangeLogFormat(err.message)

        releases.append({
            'name': release_version_name.group(2),
            'version': release_version_name.group(1),
            'packager': packager_info.group(1),
            'packager_email': packager_info.group(2),
            'time': time_value,
            'notes': release_notes
        })

    return releases


# A string containing the build info for this build, to be placed in the 'build_info' file.
__build_info__ = None


def get_build_info():
    """Returns a string containing the build info."""
    global __build_info__
    if __build_info__ is not None:
        return __build_info__

    build_info_buffer = StringIO()
    original_dir = os.getcwd()

    try:
        # We need to execute the git command in the source root.
        os.chdir(__source_root__)
        # Add in the e-mail address of the user building it.
        (_, packager_email) = run_command('git config user.email', exit_on_fail=True, command_name='git')
        print >>build_info_buffer, 'Packaged by: %s' % packager_email.strip()

        # Determine the last commit from the log.
        (_, commit_id) = run_command('git log --summary -1 | head -n 1 | cut -d \' \' -f 2',
                                     exit_on_fail=True, command_name='git')
        print >>build_info_buffer, 'Latest commit: %s' % commit_id.strip()

        # Include the branch just for safety sake.
        (_, branch) = run_command('git branch | cut -d \' \' -f 2', exit_on_fail=True, command_name='git')
        print >>build_info_buffer, 'From branch: %s' % branch.strip()

        # Add a timestamp.
        print >>build_info_buffer, 'Build time: %s' % strftime("%Y-%m-%d %H:%M:%S UTC", gmtime())

        __build_info__ = build_info_buffer.getvalue()
        return __build_info__
    finally:
        os.chdir(original_dir)

        if build_info_buffer is not None:
            build_info_buffer.close()


def set_build_info(build_info_file_path):
    """Sets the file to use as the build_info file to include in the package.

    If this is called, then future calls to get_build_info will return the contents of this file
    and will not use other commands such as 'git' to try to create it on its own.

    This is useful when you are running trying create a package on a system that does not have full access
    to git.

    @param build_info_file_path: The path to the build_info file to use.
    """
    global __build_info__
    fp = open(build_info_file_path, 'r')
    __build_info__ = fp.read()
    fp.close()

    return __build_info__


class BadChangeLogFormat(Exception):
    pass

if __name__ == '__main__':
    parser = OptionParser(usage='Usage: python build_package.py [options] rpm|tarball|deb')
    parser.add_option('-v', '--variant', dest='variant', default=None,
                      help='An optional string that is included in the package name to identify a variant '
                      'of the main release created by a different packager.  '
                      'Most users do not need to use this option.')
    parser.add_option('', '--only-create-build-info', action='store_true', dest='build_info_only', default=False,
                      help='If true, will only create the build_info file and exit.  This can be used in conjunction '
                      'with the --set-build-info option to create the build_info file on one host and then build the '
                      'rest of the package on another.  This is useful when the final host does not have full access '
                      'to git')

    parser.add_option('', '--set-build-info', dest='build_info', default=None,
                      help='The path to the build_info file to include in the final package.  If this is used, '
                      'this process will not invoke commands such as git in order to compute the build information '
                      'itself.  The file should be one built by a previous run of this script.')

    (options, args) = parser.parse_args()
    # If we are just suppose to create the build_info, then do it and exit.  We do not bother to check to see
    # if they specified a package.
    if options.build_info_only:
        write_to_file(get_build_info(), 'build_info')
        print 'Built build_info'
        sys.exit(0)

    if len(args) < 1:
        print >> sys.stderr, 'You must specify the package you wish to build, such as "rpm", "deb", or "tarball".'
        parser.print_help(sys.stderr)
        sys.exit(1)
    elif len(args) > 1:
        print >> sys.stderr, 'You may only specify one package to build.'
        parser.print_help(sys.stderr)
        sys.exit(1)
    elif args[0] not in ('rpm', 'deb', 'tarball'):
        print >> sys.stderr, 'Unknown package type given: "%s"' % args[0]
        parser.print_help(sys.stderr)
        sys.exit(1)

    if options.build_info is not None:
        set_build_info(options.build_info)

    artifact = build_package(args[0], options.variant)
    print 'Built %s' % artifact
    sys.exit(0)
