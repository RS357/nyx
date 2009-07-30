#!/usr/bin/env python
# util.py -- support functions common for arm user interface.
# Released under the GPL v3 (http://www.gnu.org/licenses/gpl.html)

import curses
from sys import maxint

LABEL_ATTR = curses.A_STANDOUT          # default formatting constant

# colors curses can handle
COLOR_LIST = (("red", curses.COLOR_RED),
             ("green", curses.COLOR_GREEN),
             ("yellow", curses.COLOR_YELLOW),
             ("blue", curses.COLOR_BLUE),
             ("cyan", curses.COLOR_CYAN),
             ("magenta", curses.COLOR_MAGENTA),
             ("black", curses.COLOR_BLACK),
             ("white", curses.COLOR_WHITE))

FORMAT_TAGS = {"<b>": curses.A_BOLD,
               "<u>": curses.A_UNDERLINE,
               "<h>": curses.A_STANDOUT}
for (colorLabel, cursesAttr) in COLOR_LIST: FORMAT_TAGS["<%s>" % colorLabel] = curses.A_NORMAL

# foreground color mappings (starts uninitialized - all colors associated with default white fg / black bg)
COLOR_ATTR_INITIALIZED = False
COLOR_ATTR = dict([(color[0], 0) for color in COLOR_LIST])

def initColors():
  """
  Initializes color mappings for the current curses. This needs to be called
  after curses.initscr().
  """
  
  global COLOR_ATTR_INITIALIZED
  if not COLOR_ATTR_INITIALIZED:
    COLOR_ATTR_INITIALIZED = True
    
    # if color support is available initializes color mappings
    if curses.has_colors():
      colorpair = 0
      
      for name, fgColor in COLOR_LIST:
        colorpair += 1
        curses.init_pair(colorpair, fgColor, -1) # -1 allows for default (possibly transparent) background
        COLOR_ATTR[name] = curses.color_pair(colorpair)
      
      # maps color tags to initialized attributes
      for colorLabel in COLOR_ATTR.keys(): FORMAT_TAGS["<%s>" % colorLabel] = COLOR_ATTR[colorLabel]

def getColor(color):
  """
  Provides attribute corresponding to a given text color. Supported colors
  include:
  red, green, yellow, blue, cyan, magenta, black, and white
  
  If color support isn't available then this uses the default terminal coloring
  scheme.
  """
  
  return COLOR_ATTR[color]

def getSizeLabel(bytes):
  """
  Converts byte count into label in its most significant units, for instance
  7500 bytes would return "7 KB".
  """
  
  if bytes >= 1073741824: return "%i GB" % (bytes / 1073741824)
  elif bytes >= 1048576: return "%i MB" % (bytes / 1048576)
  elif bytes >= 1024: return "%i KB" % (bytes / 1024)
  else: return "%i bytes" % bytes

class Panel():
  """
  Wrapper for curses subwindows. This provides safe proxies to common methods
  and is extended by panels.
  """
  
  def __init__(self, lock, height):
    self.win = None           # associated curses subwindow
    self.lock = lock          # global curses lock
    self.startY = -1          # top in parent window when created
    self.height = height      # preferred (max) height of panel, -1 if infinite
    self.isDisplaced = False  # window isn't in the right location - don't redraw
    self.maxY, self.maxX = -1, -1
    self._resetBounds()       # sets last known dimensions of win (maxX and maxY)
  
  def redraw(self):
    pass # overwritten by implementations
  
  def recreate(self, stdscr, startY, maxX=-1):
    """
    Creates a new subwindow for the panel if:
    - panel currently doesn't have a subwindow
    - the panel is being moved (startY is different)
    - there's room for the panel to grow
    
    Returns True if subwindow's created, False otherwise.
    """
    
    # I'm not sure if recreating subwindows is some sort of memory leak but the
    # Python curses bindings seem to lack all of the following:
    # - subwindow deletion (to tell curses to free the memory)
    # - subwindow moving/resizing (to restore the displaced windows)
    # so this is the only option (besides removing subwindows entirly which 
    # would mean more complicated code and no more selective refreshing)
    
    y, x = stdscr.getmaxyx()
    self._resetBounds()
    
    if self.win and startY > y:
      return False # trying to make panel out of bounds
    
    newHeight = max(0, y - startY)
    if self.height != -1: newHeight = min(newHeight, self.height)
    
    if self.startY != startY or newHeight > self.maxY or self.isDisplaced or (self.maxX != maxX and maxX != -1):
      # window growing or moving - recreate
      self.startY = startY
      startY = min(startY, y - 1) # better create a displaced window than leave it as None
      if maxX != -1: x = min(x, maxX)
      
      self.win = stdscr.subwin(newHeight, x, startY, 0)
      return True
    else: return False
  
  def clear(self):
    """
    Erases window and resets bounds used in writting to it.
    """
    
    if self.win:
      self.isDisplaced = self.startY > self.win.getparyx()[0]
      if not self.isDisplaced: self.win.erase()
      self._resetBounds()
  
  def refresh(self):
    """
    Proxy for window refresh.
    """
    
    if self.win and not self.isDisplaced: self.win.refresh()
  
  def addstr(self, y, x, msg, attr=curses.A_NORMAL):
    """
    Writes string to subwindow if able. This takes into account screen bounds
    to avoid making curses upset.
    """
    
    # subwindows need a character buffer (either in the x or y direction) from
    # actual content to prevent crash when shrank
    if self.win and self.maxX > x and self.maxY > y and not self.isDisplaced:
      self.win.addstr(y, x, msg[:self.maxX - x - 1], attr)
  
  def addfstr(self, y, x, msg):
    """
    Writes string to subwindow. The message can contain xhtml-style tags for
    formatting, including:
    <b>text</b>               bold
    <u>text</u>               underline
    <h>text</h>               highlight
    <[color]>text</[color]>   use color (see COLOR_LIST for constants)
    
    Tag nexting is supported and tag closing is not strictly enforced. This 
    does not valididate input and unrecognized tags are treated as normal text.
    Currently this funtion has the following restrictions:
    - Duplicate tags nested (such as "<b><b>foo</b></b>") is invalid and may
    throw an error.
    - Color tags shouldn't be nested in each other (results are undefined).
    """
    
    if self.win and self.maxY > y and not self.isDisplaced:
      formatting = [curses.A_NORMAL]
      expectedCloseTags = []
      
      while self.maxX > x and len(msg) > 0:
        # finds next consumeable tag
        nextTag, nextTagIndex = None, maxint
        
        for tag in FORMAT_TAGS.keys() + expectedCloseTags:
          tagLoc = msg.find(tag)
          if tagLoc != -1 and tagLoc < nextTagIndex:
            nextTag, nextTagIndex = tag, tagLoc
        
        # splits into text before and after tag
        if nextTag:
          msgSegment = msg[:nextTagIndex]
          msg = msg[nextTagIndex + len(nextTag):]
        else:
          msgSegment = msg
          msg = ""
        
        # adds text before tag with current formatting
        attr = 0
        for format in formatting: attr |= format
        self.win.addstr(y, x, msgSegment[:self.maxX - x - 1], attr)
        
        # applies tag attributes for future text
        if nextTag:
          if not nextTag.startswith("</"):
            # open tag - add formatting
            expectedCloseTags.append("</" + nextTag[1:])
            formatting.append(FORMAT_TAGS[nextTag])
          else:
            # close tag - remove formatting
            expectedCloseTags.remove(nextTag)
            formatting.remove(FORMAT_TAGS["<" + nextTag[2:]])
        
        x += len(msgSegment)
  
  def _resetBounds(self):
    if self.win: self.maxY, self.maxX = self.win.getmaxyx()
    else: self.maxY, self.maxX = -1, -1

