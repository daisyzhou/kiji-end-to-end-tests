#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runs the KijiMusic tutorial."""

import argparse
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import bento_cluster
import command
import maven_fetcher


# logging.INFO is 20
# logging.DEBUG is 10
DEBUG_VERBOSE = 5


# Command-line flags, initialized after parsing:
FLAGS = None


# Horizontal ruler:
LINE = '-' * 80


def NowMS():
  """Returns: the current time, in ms since the Epoch."""
  return int(1000 * time.time())


class Error(Exception):
  """Errors used in this module."""
  pass


# ------------------------------------------------------------------------------


def BuildFlagParser():
  """Builds a command-line flag parser for this script.

  Returns:
    The command-line flag parser for this script.
  """
  parser = argparse.ArgumentParser(
    description='KijiMusic tutorial.'
  )

  parser.add_argument(
    '--work_dir',
    type=str,
    default=None,
    help='Working directory.',
  )

  parser.add_argument(
    '--maven_local_repo',
    type=str,
    default=None,
    help='Optional Maven local repository from where to fetch artifacts.',
  )

  parser.add_argument(
    '--maven_remote_repo',
    type=str,
    default=None,
    help='Optional Maven remote repository from where to fetch artifacts.',
  )

  parser.add_argument(
    '--kiji_bento_version',
    type=str,
    default=None,
    help=('Version of KijiBento to download and test against. '
          + 'For example "1.0.0-rc4" or "1.0.0-rc5-SNAPSHOT".'),
  )

  parser.add_argument(
    '--disable_cleanup_after_test',
    nargs='?',
    action='store',
    default='false',
    const='true',
    help=('When set, disables cleaning up after test. '
          + 'Bento cluster stay alive, working directory is not wiped.'),
  )

  parser.add_argument(
    '--log_dir',
    type=str,
    default=None,
    help='Directory for log files.',
  )

  parser.add_argument(
    '--log_level',
    type=str,
    default='INFO',
    help='Log level.',
  )
  return parser


def Truth(text):
  """Parses a human truth value.

  Accepts 'true', 'false', 'yes', 'no'.
  Parsing is case insensitive.

  Args:
    text: Input to parse.
  Returns:
    Parsed truth value as a bool.
  """
  lowered = text.lower()
  if lowered in frozenset(['yes', 'true']):
    return True
  elif lowered in frozenset(['no', 'false']):
    return False
  else:
    raise Error('Invalid truth value: %r' % text)


# ------------------------------------------------------------------------------


class KijiCommand(command.Command):
  """Shell command assuming a KijiBento environment."""

  def __init__(self, command, **kwargs):
    """Runs a Kiji command line.

    Requires the current working directory to be the KijiBento installation dir.

    Args:
      command: Shell command-line, as a single text string.
      **kwargs: Keyword arguments passed to the underlying Command.
    """
    args = [
        '/bin/bash',
        '-c',
        'source ./bin/kiji-env.sh > /dev/null 2>&1 && %s' % command,
    ]
    super(KijiCommand, self).__init__(*args, **kwargs)


# ------------------------------------------------------------------------------


def ExtractArchive(archive, work_dir, strip_components=0):
  """Extracts a tar archive.

  Args:
    archive: Path to the tar archive to extract.
    work_dir: Where to extract the archive.
    strip_components: How many leading path components to strip.
  """
  assert os.path.exists(archive), (
      'Archive %r does not exist', archive)
  assert os.path.exists(work_dir), (
      'Working directory %r does not exist', work_dir)
  os.system(
      '/bin/tar xf %s --directory %s --strip-components=%d'
      % (archive, work_dir, strip_components))


# ------------------------------------------------------------------------------


class Tutorial(object):
  """Runs the KijiMusic tutorial."""

  def __init__(
      self, work_dir, version,
      maven_local_repo=None,
      maven_remote_repo=None):
    """Initializes the tutorial runner.

    Args:
      work_dir: Working directory where to operate.
      version: Version of KijiBento to test, eg. '1.0.0-rc5-SNAPSHOT'.
      maven_local_repo: Optional local Maven repository.
      maven_remote_repo: Optional remote Maven repository.
    """
    self._work_dir = work_dir
    self._run_id = NowMS()
    self._kiji_version = version
    self._maven_local_repo = maven_local_repo
    self._maven_remote_repo = maven_remote_repo

  def Setup(self):
    """Initializes the tutorial runner.

    Fetches the KijiBento Maven artifact, unzip it, starts a Bento cluster,
    and prepares a working environment.
    """

    archive_name = 'kiji-bento-%s-release.tar.gz' % self._kiji_version
    archive = os.path.join(self._work_dir, archive_name)

    if not os.path.exists(archive):
      maven_fetcher.FetchMavenArtifact(
        group_id='org.kiji.kiji-bento',
        artifact_id='kiji-bento',
        version=self._kiji_version,
        type='tar.gz',
        classifier='release',
        transitive=False,
        output_dir=self._work_dir,
        local_repo=self._maven_local_repo,
        remote_repo=self._maven_remote_repo,
      )

    self._kiji_bento_dir = (
        os.path.join(self._work_dir, 'kiji-bento-%s' % self._kiji_version))
    if not os.path.exists(self._kiji_bento_dir):
      # Strip the first path component from the kiji-bento release archive:
      # The top-level directory is "kiji-bento-<code-name>/",
      # but we don't know the code-name at this point.
      os.makedirs(self._kiji_bento_dir)
      ExtractArchive(archive, work_dir=self._kiji_bento_dir, strip_components=1)
    assert os.path.exists(self._kiji_bento_dir), (
        'KijiBento root directory not found: %r' % self._kiji_bento_dir)

    self._bento_cluster_dir = os.path.join(self._kiji_bento_dir, 'cluster')
    assert os.path.exists(self._bento_cluster_dir), (
        'BentoCluster root directory not found: %r' % self._bento_cluster_dir)

    self._kiji_music_dir = (
        os.path.join(self._kiji_bento_dir, 'examples', 'music'))
    assert os.path.exists(self._kiji_music_dir), (
        'KijiMusic root directory not found: %r' % self._kiji_music_dir)

    self._bento = bento_cluster.BentoCluster(home=self._bento_cluster_dir)
    self._bento.Start()

    self._hdfs_base = 'kiji-music-%d' % self._run_id
    self._kiji_instance_uri = 'kiji://.env/kiji_music_%d' % self._run_id

    kiji_music_lib_dir = os.path.join(self._kiji_music_dir, 'lib')

    # Builds a working environment for KijiMusic tutorial commands:
    self._env = dict(os.environ)
    self._env.update({
        'MUSIC_HOME': self._kiji_music_dir,
        'LIBS_DIR': kiji_music_lib_dir,
        'KIJI': self._kiji_instance_uri,
        'KIJI_CLASSPATH':
            ':'.join(glob.glob(os.path.join(kiji_music_lib_dir, '*'))),
        'HDFS_BASE': self._hdfs_base,
    })

  def Command(self, command):
    """Runs a Kiji command-line.

    Args:
      command: Kiji command-line to run as a single string.
    """
    cmd = KijiCommand(
        command=command,
        work_dir=self._kiji_bento_dir,
        env=self._env,
    )
    logging.debug('Exit code: %d', cmd.exit_code)
    if logging.getLogger().level <= DEBUG_VERBOSE:
      logging.debug('Output:\n%s\n%s%s', LINE, cmd.output_text, LINE)
      logging.debug('Error:\n%s\n%s%s', LINE, cmd.error_text, LINE)
    else:
      logging.debug('Output: %r', cmd.output_text)
      logging.debug('Error: %r', cmd.error_text)
    return cmd

  # ----------------------------------------------------------------------------
  # KijiMusic setup:

  def Part1(self):
    """Runs the setup part of the KijiMusic tutorial.

    http://docs.kiji.org/tutorials/music-recommendation/1.0.0-rc5/music-setup/
    """

    # --------------------------------------------------------------------------

    install = self.Command('kiji install --kiji=${KIJI}')
    assert (install.exit_code == 0)
    assert ('Successfully created kiji instance: ' in install.output_text)

    # --------------------------------------------------------------------------

    create_table = self.Command("""
        kiji-schema-shell \
            --kiji=${KIJI} \
            --file=${MUSIC_HOME}/music_schema.ddl
    """)
    assert (create_table.exit_code == 0)

    # --------------------------------------------------------------------------

    generate_data = self.Command("""
        rm -f $MUSIC_HOME/example_data/*
        ${MUSIC_HOME}/bin/data_generator.py \
            --output-dir=${MUSIC_HOME}/example_data/
    """)
    assert (generate_data.exit_code == 0)

    # --------------------------------------------------------------------------

    mkdir = self.Command('hadoop fs -mkdir ${HDFS_BASE}/kiji-mr-tutorial/')
    assert (mkdir.exit_code == 0)

    copy = self.Command("""
    hadoop fs -copyFromLocal \
        ${MUSIC_HOME}/example_data/*.json \
        ${HDFS_BASE}/kiji-mr-tutorial/
    """)
    assert (copy.exit_code == 0)

    # --------------------------------------------------------------------------

    list_tables = self.Command('kiji ls ${KIJI}')
    assert (list_tables.exit_code == 0)
    assert ('songs' in list_tables.output_text), (
        'Missing table "songs": %s' % list_tables.output_lines)
    assert ('users' in list_tables.output_text), (
        'Missing table "users": %s' % list_tables.output_lines)


  # ----------------------------------------------------------------------------
  # KijiMusic bulk-importing:

  def Part2(self):
    """Runs the bulk-importing part of the KijiMusic tutorial.

    http://docs.kiji.org/tutorials/music-recommendation/1.0.0-rc5/bulk-importing/
    """

    # --------------------------------------------------------------------------

    bulk_import = self.Command("""
    kiji bulk-import \
        --importer=org.kiji.examples.music.bulkimport.SongMetadataBulkImporter \
        --lib=${LIBS_DIR} \
        --input="format=text \
                 file=${HDFS_BASE}/kiji-mr-tutorial/song-metadata.json" \
        --output="format=kiji \
                  table=${KIJI}/songs \
                  nsplits=1"
    """)
    assert (bulk_import.exit_code == 0)
    # Surprisingly, the bulk-import CLI tools writes nothing to stdout!
    assert ('Total input paths to process : 1' in bulk_import.error_text)
    assert ('BULKIMPORTER_RECORDS_PROCESSED=50' in bulk_import.error_text)

    # --------------------------------------------------------------------------

    list_rows = self.Command('kiji scan ${KIJI}/songs --max-rows=3')
    assert (list_rows.exit_code == 0)

    # --------------------------------------------------------------------------
    # Using table import descriptors:

    copy = self.Command("""
    hadoop fs -copyFromLocal \
      ${MUSIC_HOME}/import/song-plays-import-descriptor.json \
      ${HDFS_BASE}/kiji-mr-tutorial/
    """)
    assert (copy.exit_code == 0)

    # --------------------------------------------------------------------------

    bulk_import = self.Command("""
    kiji bulk-import \
      -Dkiji.import.text.input.descriptor.path=\
${HDFS_BASE}/kiji-mr-tutorial/song-plays-import-descriptor.json \
      --importer=org.kiji.mapreduce.lib.bulkimport.JSONBulkImporter \
      --input="format=text \
               file=${HDFS_BASE}/kiji-mr-tutorial/song-plays.json" \
      --output="format=kiji \
                table=${KIJI}/users \
                nsplits=1" \
      --lib=${LIBS_DIR}
    """)
    assert (bulk_import.exit_code == 0)
    assert ('Total input paths to process : 1' in bulk_import.error_text)
    # Number of records changes from time to time:
    assert('BULKIMPORTER_RECORDS_PROCESSED=' in bulk_import.error_text)

    # --------------------------------------------------------------------------

    list_rows = self.Command('kiji scan ${KIJI}/users --max-rows=3')
    assert (list_rows.exit_code == 0)
    assert (list_rows.output_lines[0].startswith('Scanning kiji table: kiji://'))
    assert (len(list_rows.output_lines) >= 3 * 3 + 1), len(list_rows.output_lines)
    for row in range(0, 3):
      ExpectRegexMatch(
          expect=r"^entity-id=\['user-\d+'\] \[\d+\] info:track_plays$",
          actual=list_rows.output_lines[1 + row * 3])
      ExpectRegexMatch(
          expect=r"^\s*song-\d+$",
          actual=list_rows.output_lines[2 + row * 3])
      ExpectRegexMatch(
          expect=r"^$",
          actual=list_rows.output_lines[3 + row * 3])

  # ----------------------------------------------------------------------------
  # KijiMusic play-count:

  def Part3(self):
    """Runs the play-count part of the KijiMusic tutorial.

    http://docs.kiji.org/tutorials/music-recommendation/1.0.0-rc5/play-count/
    """
    gather = self.Command("""
    kiji gather \
        --gatherer=org.kiji.examples.music.gather.SongPlayCounter \
        --reducer=org.kiji.mapreduce.lib.reduce.LongSumReducer \
        --input="format=kiji table=${KIJI}/users" \
        --output="format=text \
                  file=${HDFS_BASE}/output.txt_file \
                  nsplits=2" \
        --lib=${LIBS_DIR}
    """)
    assert (gather.exit_code == 0)

    # --------------------------------------------------------------------------

    fs_text = self.Command("""
        hadoop fs -text ${HDFS_BASE}/output.txt_file/part-r-00000 | head -3
    """)
    Expect(expect=0, actual=fs_text.exit_code)
    lines = list(filter(None, fs_text.output_lines))  # filter empty lines
    Expect(expect=3, actual=len(lines))
    for line in lines:
      ExpectRegexMatch(expect=r'^song-\d+\t\d+$', actual=line)

  # ----------------------------------------------------------------------------
  # KijiMusic sequential play-count:

  def Part4(self):
    """Runs the sequential play-count part of the KijiMusic tutorial.

    http://docs.kiji.org/tutorials/music-recommendation/1.0.0-rc5/sequential-play-count/
    """
    gather = self.Command("""
    kiji gather \
        --gatherer=org.kiji.examples.music.gather.SequentialPlayCounter \
        --reducer=org.kiji.examples.music.reduce.SequentialPlayCountReducer \
        --input="format=kiji table=${KIJI}/users" \
        --output="format=avrokv \
                  file=${HDFS_BASE}/output.sequentialPlayCount \
                  nsplits=2" \
        --lib=${LIBS_DIR}
    """)
    assert (gather.exit_code == 0)

    # --------------------------------------------------------------------------

    fs_text = self.Command("""
        hadoop fs -text ${HDFS_BASE}/output.txt_file/part-r-00000 | head -3
    """)
    Expect(expect=0, actual=fs_text.exit_code)
    lines = list(filter(None, fs_text.output_lines))  # filter empty lines
    Expect(expect=3, actual=len(lines))
    for line in lines:
      ExpectRegexMatch(expect=r'^song-\d+\t\d+$', actual=line)

  # ----------------------------------------------------------------------------
  # Cleanup:

  def Cleanup(self):
    self._bento.Stop()
    shutil.rmtree(self._work_dir)


# ------------------------------------------------------------------------------


def Expect(expect, actual):
  """Assertion.

  Args:
    expect: Expected value.
    actual: Actual value.
  Raises:
    AssertionError if the actual value does not match the expected value.
  """
  if expect != actual:
    logging.error('Expected %r, got %r', expect, actual)
    raise AssertionError('Expected %r, got %r' % (expect, actual))


# ------------------------------------------------------------------------------

def ExpectRegexMatch(expect, actual):
  """Asserts that a text strings matches a given regular expression.

  Args:
    expect: Regular expression to match.
    actual: Text string to assert the content of.
  Raises:
    AssertionError if the text does not match the regular expression.
  """
  if re.match(expect, actual):
    return True
  else:
    logging.error('%r does not match regex %r.', actual, expect)
    raise AssertionError('%r does not match regex %r.' % (actual, expect))

# ------------------------------------------------------------------------------


def Main(args):
  """Program entry point."""

  # Parse the command-line flags:
  parser = BuildFlagParser()
  (parsed_flags, unparsed_flags) = parser.parse_known_args(args[1:])
  if len(unparsed_flags) > 0:
    print('Unknown flag: %s' % unparsed_flags)
    return os.EX_USAGE
  global FLAGS
  FLAGS = parsed_flags

  FLAGS.disable_cleanup_after_test = Truth(FLAGS.disable_cleanup_after_test)

  # Initialize the logging system:
  log_level = None
  if len(FLAGS.log_level) > 0:
    if (FLAGS.log_level in logging.__dict__):
      log_level = logging.__dict__[FLAGS.log_level]
      if type(log_level) != int:
        log_level = None
    if log_level is None:
      log_level = int(FLAGS.log_level)
  logging.getLogger().setLevel(log_level)
  log_handler = logging.StreamHandler()
  log_handler.setFormatter(logging.Formatter(
      '%(asctime)s %(levelname)s %(filename)s:%(lineno)s : %(message)s'))
  logging.root.addHandler(log_handler)

  # Create a temporary working directory:
  cwd = os.getcwd()
  work_dir = FLAGS.work_dir
  if work_dir is None:
    work_dir = tempfile.mkdtemp(prefix='work_dir.', dir=os.getcwd())
  work_dir = os.path.abspath(work_dir)
  if not os.path.exists(work_dir):
    os.makedirs(work_dir)

  logging.debug('Working directory: %r', work_dir)

  if FLAGS.log_dir == None:
    FLAGS.log_dir = work_dir
  if not os.path.exists(FLAGS.log_dir):
    os.makedirs(FLAGS.log_dir)

  logging.debug('Log directory: %r', FLAGS.log_dir)

  if not FLAGS.kiji_bento_version:
    print('Specify the version of KijiBento to test '
          + 'with --kiji_bento_version=...')
    return os.EX_USAGE
  logging.info('Testing tutorial of KijiBento %s', FLAGS.kiji_bento_version)

  # Runs the tutorial:
  tutorial = Tutorial(
      work_dir=work_dir,
      version=FLAGS.kiji_bento_version,
      maven_local_repo=FLAGS.maven_local_repo,
      maven_remote_repo=FLAGS.maven_remote_repo,
  )
  tutorial.Setup()
  tutorial.Part1()
  tutorial.Part2()
  tutorial.Part3()
  tutorial.Part4()
  if not FLAGS.disable_cleanup_after_test:
    tutorial.Cleanup()


if __name__ == '__main__':
  Main(sys.argv)