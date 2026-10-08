"""macOS menu-bar item (NSStatusItem) living inside Tk's Cocoa event loop.

Tk already runs NSApplication on the main thread, so the status item is created there
and its menu actions arrive on the main thread too. ``items`` is a list of
(title, callback) pairs, (title, None) for a disabled line, None for a separator, or
(title, [subitems]) for a submenu; ``set_items`` rebuilds the menu."""
import logging

import objc
from AppKit import NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength
from Foundation import NSObject

log = logging.getLogger(__name__)


class _Target(NSObject):
    def initWithCallbacks_(self, callbacks):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self.callbacks = callbacks
        return self

    def fire_(self, sender):
        cb = self.callbacks.get(sender.tag())
        if cb:
            try:
                cb()
            except Exception:
                log.exception("menu action failed")


class MenuBar:
    def __init__(self, title="AT"):
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item.button().setTitle_(title)
        self.callbacks = {}
        self.target = _Target.alloc().initWithCallbacks_(self.callbacks)

    def set_title(self, title):
        self.item.button().setTitle_(title)

    def _build(self, items):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for entry in items:
            if entry is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            title, action = entry
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            if isinstance(action, list):
                mi.setSubmenu_(self._build(action))
            elif action is None:
                mi.setEnabled_(False)
            else:
                tag = len(self.callbacks) + 1
                self.callbacks[tag] = action
                mi.setTag_(tag)
                mi.setTarget_(self.target)
                mi.setAction_("fire:")
            menu.addItem_(mi)
        return menu

    def set_items(self, items):
        self.callbacks.clear()
        self.item.setMenu_(self._build(items))

    def remove(self):
        NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
