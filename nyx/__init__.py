# Copyright 2009-2017, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Tor curses monitoring application.

::

  nyx_interface - nyx interface singleton
  tor_controller - tor connection singleton
  cache - provides our application cache

  show_message - shows a message to the user
  input_prompt - prompts the user for text input
  init_controller - initializes our connection to tor
  expand_path - expands path with respect to our chroot
  join - joins a series of strings up to a set length

  Cache - application cache
    |- write - provides a content where we can write to the cache
    |
    |- relay_nickname - provides the nickname of a relay
    +- relay_address - provides the address and orport of a relay

  CacheWriter - context in which we can write to the cache
    +- record_relay - caches information about a relay

  Interface - overall nyx interface
    |- get_page - page we're showing
    |- set_page - sets the page we're showing
    |- page_count - pages within our interface
    |
    |- header_panel - provides the header panel
    |- page_panels - provides panels on a page
    |
    |- is_paused - checks if the interface is paused
    |- set_paused - sets paused state
    |
    |- redraw - renders our content
    |- quit - quits our application
    +- halt - stops daemon panels
"""

import contextlib
import distutils.spawn
import os
import sqlite3
import sys
import threading
import time

import stem
import stem.connection
import stem.control
import stem.util.conf
import stem.util.connection
import stem.util.log
import stem.util.system
import stem.util.tor_tools

__version__ = '1.4.6-dev'
__release_date__ = 'April 28, 2011'
__author__ = 'Damian Johnson'
__contact__ = 'atagar@torproject.org'
__url__ = 'http://www.atagar.com/arm/'
__license__ = 'GPLv3'

__all__ = [
  'arguments',
  'cache',
  'controller',
  'curses',
  'log',
  'menu',
  'panel',
  'popups',
  'starter',
  'tracker',
]


def conf_handler(key, value):
  if key == 'redraw_rate':
    return max(1, value)


CONFIG = stem.util.conf.config_dict('nyx', {
  'confirm_quit': True,
  'redraw_rate': 5,
  'show_graph': True,
  'show_log': True,
  'show_connections': True,
  'show_config': True,
  'show_torrc': True,
  'show_interpreter': True,
  'start_time': 0,
}, conf_handler)

NYX_INTERFACE = None
TOR_CONTROLLER = None
CACHE = None
BASE_DIR = os.path.sep.join(__file__.split(os.path.sep)[:-1])

# technically can change but we use this query a *lot* so needs to be cached

stem.control.CACHEABLE_GETINFO_PARAMS = list(stem.control.CACHEABLE_GETINFO_PARAMS) + ['address']

# disable trace level messages about cache hits

stem.control.LOG_CACHE_FETCHES = False

SCHEMA_VERSION = 1  # version of our scheme, bump this if you change the following
SCHEMA = (
  'CREATE TABLE schema(version NUMBER)',
  'INSERT INTO schema(version) VALUES (%i)' % SCHEMA_VERSION,

  'CREATE TABLE relays(fingerprint TEXT PRIMARY KEY, address TEXT, or_port NUMBER, nickname TEXT)',
)


try:
  uses_settings = stem.util.conf.uses_settings('nyx', os.path.join(BASE_DIR, 'settings'), lazy_load = False)
except IOError as exc:
  print("Unable to load nyx's internal configurations: %s" % exc)
  sys.exit(1)


def main():
  try:
    nyx.starter.main()
  except ImportError as exc:
    if exc.message == 'No module named stem':
      if distutils.spawn.find_executable('pip') is not None:
        advice = ", try running 'sudo pip install stem'"
      elif distutils.spawn.find_executable('apt-get') is not None:
        advice = ", try running 'sudo apt-get install python-stem'"
      else:
        advice = ', you can find it at https://stem.torproject.org/download.html'

      print('nyx requires stem' + advice)
    else:
      print('Unable to start nyx: %s' % exc)

    sys.exit(1)


def draw_loop():
  interface = nyx_interface()
  next_key = None  # use this as the next user input

  stem.util.log.info('nyx started (initialization took %0.1f seconds)' % (time.time() - CONFIG['start_time']))

  while not interface._quit:
    interface.redraw()

    if next_key:
      key, next_key = next_key, None
    else:
      key = nyx.curses.key_input(CONFIG['redraw_rate'])

    if key.match('right'):
      interface.set_page((interface.get_page() + 1) % interface.page_count())
    elif key.match('left'):
      interface.set_page((interface.get_page() - 1) % interface.page_count())
    elif key.match('p'):
      interface.set_paused(not interface.is_paused())
    elif key.match('m'):
      nyx.menu.show_menu()
    elif key.match('q'):
      if CONFIG['confirm_quit']:
        confirmation_key = show_message('Are you sure (q again to confirm)?', nyx.curses.BOLD, max_wait = 30)

        if not confirmation_key.match('q'):
          continue

      break
    elif key.match('x'):
      confirmation_key = show_message("This will reset Tor's internal state. Are you sure (x again to confirm)?", nyx.curses.BOLD, max_wait = 30)

      if confirmation_key.match('x'):
        try:
          tor_controller().signal(stem.Signal.RELOAD)
        except stem.ControllerError as exc:
          stem.util.log.error('Error detected when reloading tor: %s' % exc.strerror)
    elif key.match('h'):
      next_key = nyx.popups.show_help()
    else:
      for panel in interface.page_panels():
        for keybinding in panel.key_handlers():
          keybinding.handle(key)


def nyx_interface():
  """
  Singleton controller for our interface.

  :returns: :class:`~nyx.Interface` controller
  """

  if NYX_INTERFACE is None:
    Interface()  # constructor sets NYX_INTERFACE

  return NYX_INTERFACE


def tor_controller():
  """
  Singleton for getting our tor controller connection.

  :returns: :class:`~stem.control.Controller` nyx is using
  """

  return TOR_CONTROLLER


def cache():
  """
  Provides the sqlite cache for application data.

  :returns: :class:`~nyx.cache.Cache` for our applicaion
  """

  global CACHE

  if CACHE is None:
    CACHE = Cache()

  return CACHE


def show_message(message = None, *attr, **kwargs):
  """
  Shows a message in our header.

  :param str message: message to be shown
  """

  return nyx_interface().header_panel().show_message(message, *attr, **kwargs)


def input_prompt(msg, initial_value = ''):
  """
  Prompts the user for input.

  :param str message: prompt for user input
  :param str initial_value: initial value of the prompt

  :returns: **str** with the user input, this is **None** if the prompt is
    canceled
  """

  header_panel = nyx_interface().header_panel()

  header_panel.show_message(msg)
  user_input = nyx.curses.str_input(len(msg), header_panel.get_height() - 1, initial_value)
  header_panel.show_message()

  return user_input


def init_controller(*args, **kwargs):
  """
  Sets the Controller used by nyx. This is a passthrough for Stem's
  :func:`~stem.connection.connect` function.

  :returns: :class:`~stem.control.Controller` nyx is using
  """

  global TOR_CONTROLLER
  TOR_CONTROLLER = stem.connection.connect(*args, **kwargs)
  return TOR_CONTROLLER


@uses_settings
def data_directory(filename, config):
  path = config.get('data_directory', '~/.nyx')

  if path == 'disabled':
    return None

  data_dir = os.path.expanduser(path)

  if not os.path.exists(data_dir):
    try:
      os.mkdir(data_dir)
    except OSError as exc:
      stem.util.log.log_once('nyx.data_directory_unavailable', stem.util.log.NOTICE, 'Unable to create a data directory at %s (%s). This is fine, but caching is disabled meaning performance will be diminished a bit.' % (data_dir, exc))
      return None

  return os.path.join(data_dir, filename)


@uses_settings
def expand_path(path, config):
  """
  Expands relative paths and include our chroot if one was set.

  :param str path: path to be expanded

  :returns: **str** with the expanded path
  """

  if path is None:
    return None

  try:
    chroot = config.get('tor_chroot', '')
    tor_cwd = stem.util.system.cwd(tor_controller().get_pid(None))
    return chroot + stem.util.system.expand_path(path, tor_cwd)
  except IOError as exc:
    stem.util.log.info('Unable to expand a relative path (%s): %s' % (path, exc))
    return path


def join(entries, joiner = ' ', size = None):
  """
  Joins a series of strings similar to str.join(), but only up to a given size.
  This returns an empty string if none of the entries will fit. For example...

    >>> join(['This', 'is', 'a', 'looooong', 'message'], size = 18)
    'This is a looooong'

    >>> join(['This', 'is', 'a', 'looooong', 'message'], size = 17)
    'This is a'

    >>> join(['This', 'is', 'a', 'looooong', 'message'], size = 2)
    ''

  :param list entries: strings to be joined
  :param str joiner: strings to join the entries with
  :param int size: maximum length the result can be, there's no length
    limitation if **None**

  :returns: **str** of the joined entries up to the given length
  """

  if size is None:
    return joiner.join(entries)

  result = ''

  for entry in entries:
    new_result = joiner.join((result, entry)) if result else entry

    if len(new_result) > size:
      break
    else:
      result = new_result

  return result


class Cache(object):
  """
  Cache for frequently needed information. This persists to disk if we can, and
  otherwise is an in-memory cache.
  """

  def __init__(self):
    self._conn_lock = threading.RLock()
    cache_path = nyx.data_directory('cache.sqlite')

    if cache_path:
      try:
        self._conn = sqlite3.connect(cache_path, check_same_thread = False)
        schema = self._conn.execute('SELECT version FROM schema').fetchone()[0]
      except:
        schema = None

      if schema == SCHEMA_VERSION:
        stem.util.log.info('Cache loaded from %s' % cache_path)
      else:
        if schema is None:
          stem.util.log.info('Cache at %s is missing a schema, clearing it.' % cache_path)
        else:
          stem.util.log.info('Cache at %s has schema version %s but the current version is %s, clearing it.' % (cache_path, schema, SCHEMA_VERSION))

        self._conn.close()
        os.remove(cache_path)
        self._conn = sqlite3.connect(cache_path, check_same_thread = False)

        for cmd in SCHEMA:
          self._conn.execute(cmd)
    else:
      stem.util.log.info('Unable to cache to disk. Using an in-memory cache instead.')
      self._conn = sqlite3.connect(':memory:', check_same_thread = False)

      for cmd in SCHEMA:
        self._conn.execute(cmd)

  @contextlib.contextmanager
  def write(self):
    """
    Provides a context in which we can modify the cache.

    :returns: :class:`~nyx.CacheWriter` that can modify the cache
    """

    with self._conn:
      yield CacheWriter(self)

  def relay_nickname(self, fingerprint, default = None):
    """
    Provides the nickname associated with the given relay.

    :param str fingerprint: relay to look up
    :param str default: response if no such relay exists

    :returns: **str** with the nickname ("Unnamed" if unset)
    """

    result = self._query('SELECT nickname FROM relays WHERE fingerprint=?', fingerprint).fetchone()
    return result[0] if result else default

  def relay_address(self, fingerprint, default = None):
    """
    Provides the (address, port) tuple where a relay is running.

    :param str fingerprint: fingerprint to be checked
    :param str default: response if no such relay exists

    :returns: **tuple** with a **str** address and **int** port
    """

    result = self._query('SELECT address, or_port FROM relays WHERE fingerprint=?', fingerprint).fetchone()
    return result if result else default

  def _query(self, query, *param):
    """
    Performs a query on our cache.
    """

    with self._conn_lock:
      return self._conn.execute(query, param)


class CacheWriter(object):
  def __init__(self, cache):
    self._cache = cache

  def record_relay(self, fingerprint, address, or_port, nickname):
    """
    Records relay metadata.

    :param str fingerprint: relay fingerprint
    :param str address: ipv4 or ipv6 address
    :param int or_port: ORPort of the relay
    :param str nickname: relay nickname

    :raises: **ValueError** if provided data is malformed
    """

    if not stem.util.tor_tools.is_valid_fingerprint(fingerprint):
      raise ValueError("'%s' isn't a valid fingerprint" % fingerprint)
    elif not stem.util.tor_tools.is_valid_nickname(nickname):
      raise ValueError("'%s' isn't a valid nickname" % nickname)
    elif not stem.util.connection.is_valid_ipv4_address(address) and not stem.util.connection.is_valid_ipv6_address(address):
      raise ValueError("'%s' isn't a valid address" % address)
    elif not stem.util.connection.is_valid_port(or_port):
      raise ValueError("'%s' isn't a valid port" % or_port)

    self._cache._query('INSERT OR REPLACE INTO relays(fingerprint, address, or_port, nickname) VALUES (?,?,?,?)', fingerprint, address, or_port, nickname)


class Interface(object):
  """
  Overall state of the nyx interface.
  """

  def __init__(self):
    global NYX_INTERFACE

    self._page = 0
    self._page_panels = []
    self._header_panel = None
    self._paused = False
    self._quit = False

    NYX_INTERFACE = self

    self._header_panel = nyx.panel.header.HeaderPanel()
    first_page_panels = []

    if CONFIG['show_graph']:
      first_page_panels.append(nyx.panel.graph.GraphPanel())

    if CONFIG['show_log']:
      first_page_panels.append(nyx.panel.log.LogPanel())

    if first_page_panels:
      self._page_panels.append(first_page_panels)

    if CONFIG['show_connections']:
      self._page_panels.append([nyx.panel.connection.ConnectionPanel()])

    if CONFIG['show_config']:
      self._page_panels.append([nyx.panel.config.ConfigPanel()])

    if CONFIG['show_torrc']:
      self._page_panels.append([nyx.panel.torrc.TorrcPanel()])

    if CONFIG['show_interpreter']:
      self._page_panels.append([nyx.panel.interpreter.InterpreterPanel()])

    visible_panels = self.page_panels()

    for panel in self:
      panel.set_visible(panel in visible_panels)

      if isinstance(panel, nyx.panel.DaemonPanel):
        panel.start()

  def get_page(self):
    """
    Provides the page we're showing.

    :return: **int** of the page we're showing
    """

    return self._page

  def set_page(self, page_number):
    """
    Sets the selected page.

    :param int page_number: page to be shown

    :raises: **ValueError** if the page_number is invalid
    """

    if page_number < 0 or page_number >= self.page_count():
      raise ValueError('Invalid page number: %i' % page_number)

    if page_number != self._page:
      self._page = page_number
      self.header_panel().redraw()

      visible_panels = self.page_panels()

      for panel in self:
        panel.set_visible(panel in visible_panels)

  def page_count(self):
    """
    Provides the number of pages the interface has.

    :returns: **int** number of pages in the interface
    """

    return len(self._page_panels)

  def header_panel(self):
    """
    Provides our interface's header.

    :returns: :class:`~nyx.panel.header.HeaderPanel` of our interface
    """

    return self._header_panel

  def page_panels(self, page_number = None):
    """
    Provides panels belonging to a page, ordered top to bottom.

    :param int page_number: page to provide the panels of, current page if
      **None**

    :returns: **list** of panels on that page
    """

    page_number = self._page if page_number is None else page_number
    return [self._header_panel] + self._page_panels[page_number]

  def is_paused(self):
    """
    Checks if the interface is configured to be paused.

    :returns: **True** if the interface is paused, **False** otherwise
    """

    return self._paused

  def set_paused(self, is_pause):
    """
    Pauses or unpauses the interface.

    :param bool is_pause: suspends the interface if **True**, resumes it
      otherwise
    """

    if is_pause != self._paused:
      self._paused = is_pause

      for panel in self:
        panel.set_paused(is_pause)

      for panel in self.page_panels():
        panel.redraw()

  def redraw(self):
    """
    Renders our displayed content.
    """

    occupied = 0

    for panel in self.page_panels():
      panel.redraw(force = False, top = occupied)
      occupied += panel.get_height()

  def quit(self):
    """
    Quits our application.
    """

    self._quit = True

  def halt(self):
    """
    Stops curses panels in our interface.

    :returns: **threading.Thread** terminating daemons
    """

    def halt_panels():
      daemons = [panel for panel in self if isinstance(panel, nyx.panel.DaemonPanel)]

      for panel in daemons:
        panel.stop()

      for panel in daemons:
        panel.join()

    halt_thread = threading.Thread(target = halt_panels)
    halt_thread.start()
    return halt_thread

  def __iter__(self):
    yield self._header_panel

    for page in self._page_panels:
      for panel in page:
        yield panel


import nyx.curses
import nyx.menu
import nyx.panel
import nyx.panel.config
import nyx.panel.connection
import nyx.panel.graph
import nyx.panel.header
import nyx.panel.interpreter
import nyx.panel.log
import nyx.panel.torrc
import nyx.popups
import nyx.starter
